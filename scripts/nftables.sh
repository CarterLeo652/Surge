#!/usr/bin/env bash
#
# nftables 端口转发管理工具 v1.6
# 交互式管理 DNAT 端口转发规则（支持 TCP / UDP / TCP+UDP 选择）
#

# ============== 常量定义 ==============
CONF_DIR="/etc/nftables.d"
CONF_FILE="${CONF_DIR}/port-forward.conf"
BACKUP_DIR="${CONF_DIR}/backups"
MAIN_CONF="/etc/nftables.conf"
SYSCTL_CONF="/etc/sysctl.d/99-nft-forward.conf"
LOG_FILE="/var/log/nft-forward.log"
LOGROTATE_CONF="/etc/logrotate.d/nft-forward"
TABLE_NAME="port_forward"

# ============== 日志函数 ==============
log_action() {
    local msg="$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${msg}" >> "${LOG_FILE}" 2>/dev/null || true
}

# ============== 输出辅助（用 printf 避免 echo -e 转义副作用） ==============
info()    { printf '\033[32m[信息]\033[0m %s\n' "$1"; }
warn()    { printf '\033[33m[警告]\033[0m %s\n' "$1"; }
err()     { printf '\033[31m[错误]\033[0m %s\n' "$1"; }

clear_screen() {
    if command -v clear &>/dev/null; then
        clear 2>/dev/null || printf '\033[2J\033[H'
    else
        printf '\033[2J\033[H'
    fi
}

pause_screen() {
    echo ""
    read -rp "按 Enter 返回..." _
}

# ============== root 权限检查 ==============
check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "此脚本需要 root 权限运行，请使用 sudo 或 root 用户执行。"
        exit 1
    fi
}

# ============== 输入验证 ==============
validate_port() {
    local port="$1"
    # 拒绝非纯数字、前导零（避免 bash 八进制歧义）、空串
    if [[ ! "$port" =~ ^[0-9]+$ ]] || [[ "$port" =~ ^0[0-9] ]]; then
        return 1
    fi
    if (( port < 1 || port > 65535 )); then
        return 1
    fi
    return 0
}

validate_ipv4() {
    local ip="$1"
    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    [[ "$ip" =~ (^|\.)0[0-9] ]] && return 1
    local IFS='.' octet
    read -ra octets <<< "$ip"
    for octet in "${octets[@]}"; do (( octet <= 255 )) || return 1; done
    return 0
}

validate_ipv6() {
    local ip="$1" work part count=0 compressed=false
    [[ "$ip" == *:* && "$ip" =~ ^[0-9A-Fa-f:]+$ && "$ip" != *":::"* ]] || return 1
    if [[ "$ip" == *"::"* ]]; then
        [[ "${ip/::/}" != *"::"* ]] || return 1
        compressed=true
        work="${ip/::/:x:}"
    else
        work="$ip"
    fi
    local IFS=':'
    read -ra parts <<< "$work"
    for part in "${parts[@]}"; do
        [[ -z "$part" ]] && continue
        if [[ "$part" == "x" ]]; then
            ((count++))
            continue
        fi
        [[ "$part" =~ ^[0-9A-Fa-f]{1,4}$ ]] || return 1
        ((count++))
    done
    if $compressed; then
        (( count < 8 ))
    else
        (( count == 8 ))
    fi
}

ip_family() {
    [[ "$1" == *:* ]] && echo "ipv6" || echo "ipv4"
}

family_display() {
    [[ "$1" == "ipv6" ]] && echo "IPv6" || echo "IPv4"
}

get_local_ip() {
    local ip
    ip=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1) || true
    [[ -n "$ip" ]] && { echo "$ip"; return; }
    ip=$(ip -4 addr show scope global 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1) || true
    [[ -n "$ip" ]] && { echo "$ip"; return; }
    hostname -I 2>/dev/null | awk '{print $1}' || true
}

get_local_ipv6() {
    local ip
    ip=$(ip -6 route get 2606:4700:4700::1111 2>/dev/null | awk '/ src / {for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}') || true
    validate_ipv6 "$ip" && echo "$ip"
}

has_usable_ipv6() {
    [[ -n "$(get_local_ipv6)" ]]
}

SELECTED_FAMILY=""

choose_ip_family() {
    local default="${1:-}" choice
    while true; do
        echo "请选择 IP 类型:"
        echo "  1) IPv4"
        if has_usable_ipv6; then
            echo "  2) IPv6"
        else
            echo "  2) IPv6（本机未检测到 IPv6）"
        fi
        read -rp "请选择 [1-2${default:+，默认 ${default}}]: " choice
        choice="${choice:-$default}"
        case "$choice" in
            1|ipv4|IPv4) SELECTED_FAMILY="ipv4"; return ;;
            2|ipv6|IPv6)
                if has_usable_ipv6; then
                    SELECTED_FAMILY="ipv6"
                    return
                fi
                err "本机未检测到可用 IPv6，无法创建 IPv6 转发规则。"
                ;;
            *) err "无效选择，请输入 1 或 2。" ;;
        esac
    done
}

# ============== 发行版检测 ==============
detect_pkg_manager() {
    if command -v apt-get &>/dev/null; then
        echo "apt"
    elif command -v dnf &>/dev/null; then
        echo "dnf"
    elif command -v yum &>/dev/null; then
        echo "yum"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    else
        echo "unknown"
    fi
}

# ============== iptables 可用性检测 ==============
# 不依赖 systemd 服务，而是检测命令是否存在且能读取规则
has_iptables() {
    command -v iptables &>/dev/null && iptables -S &>/dev/null
}

has_ip6tables() {
    command -v ip6tables &>/dev/null && ip6tables -S &>/dev/null
}

# ============== iptables 规则持久化尝试 ==============
try_persist_iptables() {
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save >/dev/null 2>&1 && return 0
    fi
    if command -v iptables-save &>/dev/null; then
        if [[ -d /etc/iptables ]]; then
            iptables-save > /etc/iptables/rules.v4 2>/dev/null && return 0
        elif [[ -d /etc/sysconfig ]]; then
            iptables-save > /etc/sysconfig/iptables 2>/dev/null && return 0
        fi
    fi
    if command -v service &>/dev/null; then
        service iptables save >/dev/null 2>&1 && return 0
    fi
    return 1
}

# ============== 检查目标是否仍被其他规则使用 ==============
# 参数: $1=目标IP  $2=目标端口  $3=要排除的本机端口(即正在删除的那条)
#       $4=要排除的协议(可选，用于协议级共享判定)
dest_still_used() {
    local check_ip="$1" check_dport="$2" exclude_lport="$3" exclude_proto="${4:-}"
    local rule lport dip dport proto note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r lport dip dport proto note <<< "$rule"
        proto="${proto:-both}"
        [[ "$lport" == "$exclude_lport" ]] && continue
        [[ "$dip" == "$check_ip" && "$dport" == "$check_dport" ]] || continue
        if [[ -z "$exclude_proto" ]] ||            { proto_has_tcp "$exclude_proto" && proto_has_tcp "$proto"; } ||            { proto_has_udp "$exclude_proto" && proto_has_udp "$proto"; }; then
            return 0
        fi
    done
    return 1
}

# ============== 协议字段标准化 ==============
# 把用户输入归一为 tcp|udp|both；非法输入返回空
normalize_proto() {
    case "$1" in
        1|tcp|TCP)   echo "tcp"  ;;
        2|udp|UDP)   echo "udp"  ;;
        3|both|BOTH) echo "both" ;;
        *)           echo ""     ;;
    esac
}

# 判断某协议是否应包含 tcp
proto_has_tcp() { [[ "$1" == "tcp" || "$1" == "both" ]]; }
# 判断某协议是否应包含 udp
proto_has_udp() { [[ "$1" == "udp" || "$1" == "both" ]]; }

# 显示用：tcp / udp / tcp+udp
proto_display() {
    case "$1" in
        tcp)  echo "tcp"     ;;
        udp)  echo "udp"     ;;
        both) echo "tcp+udp" ;;
        *)    echo "?"       ;;
    esac
}

# ============== UDP 可达性弱探测 ==============
# 参数: $1=目标IP  $2=目标端口
# 返回 0=可达（未收到 ICMP port-unreachable）
# 返回 1=不可达（收到 ICMP 或无可用工具）
# 注意: UDP 无连接，"可达"只代表端口未被显式关闭，不代表服务正常响应
test_udp_reachable() {
    local dip="$1" dport="$2"

    # 方案 A: netcat (openbsd 版支持 -uz)
    if command -v nc &>/dev/null; then
        # -u UDP, -z 零 IO, -w1 等待 1 秒看是否回 ICMP
        # 返回 0 = 没收到拒绝(视为可达); 非 0 = 收到 ICMP port-unreachable(不可达)
        if nc -u -z -w1 "$dip" "$dport" 2>/dev/null; then
            return 0
        fi
        return 1
    fi

    # 方案 B: bash 原生 /dev/udp (bash 4+ 且内核支持)
    # 发个空包，能写出去且 1 秒内没被 ICMP 拒绝即视为可达
    if [[ -w /dev/null ]] && (echo -n "" >"/dev/udp/${dip}/${dport}") 2>/dev/null; then
        sleep 0.2 2>/dev/null || true
        # /dev/udp 无法可靠捕获 ICMP，只能确认"能发出去"
        return 0
    fi

    # 两种工具都没有
    return 1
}

# ============== firewalld / iptables 端口放行 ==============
# 参数: $1=本机监听端口  $2=目标IP  $3=目标端口  $4=协议(tcp|udp|both，默认 both)
firewall_open_port() {
    local lport="$1" dest_ip="$2" dport="$3" proto="${4:-both}"
    local family cmd disp
    family=$(ip_family "$dest_ip")
    disp=$(proto_display "$proto")

    if systemctl is-active --quiet firewalld 2>/dev/null; then
        if proto_has_tcp "$proto"; then
            firewall-cmd --permanent --add-rich-rule="rule family=\"$family\" port port=\"$lport\" protocol=\"tcp\" accept" >/dev/null 2>&1 || true
        fi
        if proto_has_udp "$proto"; then
            firewall-cmd --permanent --add-rich-rule="rule family=\"$family\" port port=\"$lport\" protocol=\"udp\" accept" >/dev/null 2>&1 || true
        fi
        firewall-cmd --reload >/dev/null 2>&1 || true
        info "已在 firewalld 中放行 IPv${family#ipv} 端口 ${lport} (${disp})。"
        return
    fi

    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw "active"; then
        if proto_has_tcp "$proto"; then
            ufw route allow proto tcp to "$dest_ip" port "$dport" >/dev/null 2>&1 || true
        fi
        if proto_has_udp "$proto"; then
            ufw route allow proto udp to "$dest_ip" port "$dport" >/dev/null 2>&1 || true
        fi
        info "已在 UFW 中放行 IPv${family#ipv} 转发到 ${dest_ip}:${dport} (${disp})。"
        return
    fi

    if [[ "$family" == "ipv6" ]]; then
        cmd="ip6tables"
        has_ip6tables || return
    else
        cmd="iptables"
        has_iptables || return
    fi

    if proto_has_tcp "$proto"; then
        $cmd -C INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || $cmd -I INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || true
        $cmd -C FORWARD -d "$dest_ip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || $cmd -I FORWARD -d "$dest_ip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || true
    fi
    if proto_has_udp "$proto"; then
        $cmd -C INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || $cmd -I INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || true
        $cmd -C FORWARD -d "$dest_ip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || $cmd -I FORWARD -d "$dest_ip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || true
    fi
    $cmd -C FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || $cmd -I FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    info "已在 ${cmd} 中放行 IPv${family#ipv} 转发端口 ${lport} (${disp})。"
}

firewall_close_port() {
    local lport="$1" dest_ip="$2" dport="$3" proto="${4:-both}" force="${5:-}"
    local family cmd
    family=$(ip_family "$dest_ip")

    if systemctl is-active --quiet firewalld 2>/dev/null; then
        proto_has_tcp "$proto" && firewall-cmd --permanent --remove-rich-rule="rule family=\"$family\" port port=\"$lport\" protocol=\"tcp\" accept" >/dev/null 2>&1 || true
        proto_has_udp "$proto" && firewall-cmd --permanent --remove-rich-rule="rule family=\"$family\" port port=\"$lport\" protocol=\"udp\" accept" >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
        return
    fi

    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw "active"; then
        if proto_has_tcp "$proto" && { [[ "$force" == "force" ]] || ! dest_still_used "$dest_ip" "$dport" "$lport" tcp; }; then
            yes | ufw route delete allow proto tcp to "$dest_ip" port "$dport" >/dev/null 2>&1 || true
        fi
        if proto_has_udp "$proto" && { [[ "$force" == "force" ]] || ! dest_still_used "$dest_ip" "$dport" "$lport" udp; }; then
            yes | ufw route delete allow proto udp to "$dest_ip" port "$dport" >/dev/null 2>&1 || true
        fi
        return
    fi

    if [[ "$family" == "ipv6" ]]; then cmd="ip6tables"; else cmd="iptables"; fi
    command -v "$cmd" &>/dev/null || return
    proto_has_tcp "$proto" && $cmd -D INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || true
    proto_has_udp "$proto" && $cmd -D INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || true
    if proto_has_tcp "$proto" && { [[ "$force" == "force" ]] || ! dest_still_used "$dest_ip" "$dport" "$lport" tcp; }; then
        $cmd -D FORWARD -d "$dest_ip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || true
    fi
    if proto_has_udp "$proto" && { [[ "$force" == "force" ]] || ! dest_still_used "$dest_ip" "$dport" "$lport" udp; }; then
        $cmd -D FORWARD -d "$dest_ip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || true
    fi
}

# ============== 端口占用检测（TCP + UDP） ==============
check_port_conflict() {
    local port="$1"
    local conflict=""
    if ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; then
        conflict="TCP"
    fi
    if ss -ulnp 2>/dev/null | grep -qE ":${port}\b"; then
        if [[ -n "$conflict" ]]; then
            conflict="TCP+UDP"
        else
            conflict="UDP"
        fi
    fi
    if [[ -n "$conflict" ]]; then
        warn "本机端口 ${port} 已被其他服务占用（${conflict}）。"
        warn "添加转发后，该端口的外部流量将被转发，本地服务可能无法从外部访问。"
        read -rp "是否仍要继续添加转发规则？[y/N]: " ans
        if [[ ! "$ans" =~ ^[Yy]$ ]]; then
            return 1
        fi
    fi
    return 0
}

# ============== 初始化配置文件结构 ==============
init_conf() {
    mkdir -p "${CONF_DIR}" "${BACKUP_DIR}" 2>/dev/null || {
        err "无法创建配置目录 ${CONF_DIR}，请检查权限。"
        return 1
    }

    # 确保日志文件存在
    touch "${LOG_FILE}" 2>/dev/null || true

    # 创建 logrotate 配置
    if [[ ! -f "${LOGROTATE_CONF}" ]]; then
        cat > "${LOGROTATE_CONF}" <<'LOGROTATE'
/var/log/nft-forward.log {
    monthly
    rotate 6
    compress
    missingok
    notifempty
}
LOGROTATE
    fi

    # 确保主配置存在且包含 include
    if [[ ! -f "${MAIN_CONF}" ]]; then
        # 极简系统可能没有 nftables.conf，创建最小文件确保重启后规则自动加载
        cat > "${MAIN_CONF}" <<'NFTCONF'
#!/usr/sbin/nft -f
flush ruleset
include "/etc/nftables.d/*.conf"
NFTCONF
        info "已创建 ${MAIN_CONF}（系统中不存在该文件）。"
        log_action "创建 ${MAIN_CONF}"
    elif ! grep -qF 'include "/etc/nftables.d/*.conf"' "${MAIN_CONF}" 2>/dev/null; then
        echo 'include "/etc/nftables.d/*.conf"' >> "${MAIN_CONF}"
        info "已在 ${MAIN_CONF} 中添加 include 指令。"
        log_action "在 ${MAIN_CONF} 中添加 include 指令"
    fi

    # 如果转发配置文件不存在，创建初始结构
    if [[ ! -f "${CONF_FILE}" ]]; then
        write_conf_file || return 1
    fi
}

# ============== 写出配置文件（IPv4 / IPv6 规则按目标地址自动分表） ==============
# RULES 数组格式: "本机端口|目标IP|目标端口|协议(tcp|udp|both)|备注"
declare -a RULES=()

sanitize_note() {
    local note="${1:-}"
    note="${note//$'\r'/ }"
    note="${note//$'\n'/ }"
    note="${note//|/ }"
    printf "%s" "$note"
}

load_rules() {
    RULES=()
    [[ -f "${CONF_FILE}" ]] || return

    local line p lport dip dport pending_note="" current_family="ipv4"
    while IFS= read -r line; do
        [[ "$line" =~ ^table[[:space:]]+ip6[[:space:]] ]] && { current_family="ipv6"; continue; }
        [[ "$line" =~ ^table[[:space:]]+ip[[:space:]] ]] && { current_family="ipv4"; continue; }
        if [[ "$line" =~ ^[[:space:]]*#[[:space:]]*备注:[[:space:]]*(.*)$ ]]; then
            pending_note=$(sanitize_note "${BASH_REMATCH[1]}")
            continue
        fi
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$line" =~ (tcp|udp)[[:space:]]+dport[[:space:]]+([0-9]+)[[:space:]]+dnat[[:space:]]+to[[:space:]]+\[?([0-9A-Fa-f:.]+)\]?:([0-9]+) ]]; then
            p="${BASH_REMATCH[1]}"; lport="${BASH_REMATCH[2]}"; dip="${BASH_REMATCH[3]}"; dport="${BASH_REMATCH[4]}"
            [[ "$(ip_family "$dip")" == "$current_family" ]] || continue

            local idx=-1 i old_lport old_dip old_dport old_proto old_note
            for ((i=0; i<${#RULES[@]}; i++)); do
                IFS='|' read -r old_lport old_dip old_dport old_proto old_note <<< "${RULES[$i]}"
                if [[ "$old_lport" == "$lport" && "$(ip_family "$old_dip")" == "$current_family" ]]; then idx=$i; break; fi
            done
            if (( idx < 0 )); then
                RULES+=("${lport}|${dip}|${dport}|${p}|${pending_note}")
            else
                old_proto="${old_proto:-both}"
                [[ "$old_proto" != "both" && "$old_proto" != "$p" ]] && old_proto="both"
                [[ -z "$old_note" ]] && old_note="$pending_note"
                RULES[$idx]="${old_lport}|${old_dip}|${old_dport}|${old_proto}|${old_note}"
            fi
            pending_note=""
        fi
    done < "${CONF_FILE}"
}

write_conf_file() {
    local local_ip="" local_ip6="" has_v4=false has_v6=false rule lport dip dport proto note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r lport dip dport proto note <<< "$rule"
        [[ "$(ip_family "$dip")" == "ipv6" ]] && has_v6=true || has_v4=true
    done
    if $has_v4; then
        local_ip=$(get_local_ip)
        [[ -n "$local_ip" ]] || { err "无法获取本机 IPv4 地址。"; return 1; }
    fi
    if $has_v6; then
        local_ip6=$(get_local_ipv6)
        [[ -n "$local_ip6" ]] || { err "本机未检测到可用 IPv6，无法写入 IPv6 转发规则。"; return 1; }
    fi

    local tmp_file="${CONF_FILE}.tmp.$$"
    cat > "${tmp_file}" <<EOF
#!/usr/sbin/nft -f
# 由 nftables 端口转发管理工具 v1.6 自动生成。
EOF

    if $has_v4; then
        cat >> "${tmp_file}" <<EOF
define LOCAL_IP = ${local_ip}

table ip ${TABLE_NAME} {
    chain prerouting {
        type nat hook prerouting priority -100; policy accept;
EOF
        for rule in "${RULES[@]}"; do
            IFS='|' read -r lport dip dport proto note <<< "$rule"
            [[ "$(ip_family "$dip")" == "ipv4" ]] || continue
            {
                echo ""
                echo "        # 转发: IPv4 本机:${lport} ($(proto_display "${proto:-both}")) -> ${dip}:${dport}"
                [[ -n "$note" ]] && echo "        # 备注: ${note}"
                proto_has_tcp "${proto:-both}" && echo "        tcp dport ${lport} dnat to ${dip}:${dport}"
                proto_has_udp "${proto:-both}" && echo "        udp dport ${lport} dnat to ${dip}:${dport}"
            } >> "${tmp_file}"
        done
        cat >> "${tmp_file}" <<EOF
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
EOF
        for rule in "${RULES[@]}"; do
            IFS='|' read -r lport dip dport proto note <<< "$rule"
            [[ "$(ip_family "$dip")" == "ipv4" ]] || continue
            {
                proto_has_tcp "${proto:-both}" && echo "        ip daddr ${dip} tcp dport ${dport} ct status dnat snat to \$LOCAL_IP"
                proto_has_udp "${proto:-both}" && echo "        ip daddr ${dip} udp dport ${dport} ct status dnat snat to \$LOCAL_IP"
            } >> "${tmp_file}"
        done
        echo "    }" >> "${tmp_file}"
        echo "}" >> "${tmp_file}"
    fi

    if $has_v6; then
        cat >> "${tmp_file}" <<EOF
define LOCAL_IP6 = ${local_ip6}

table ip6 ${TABLE_NAME} {
    chain prerouting {
        type nat hook prerouting priority -100; policy accept;
EOF
        for rule in "${RULES[@]}"; do
            IFS='|' read -r lport dip dport proto note <<< "$rule"
            [[ "$(ip_family "$dip")" == "ipv6" ]] || continue
            {
                echo ""
                echo "        # 转发: IPv6 本机:${lport} ($(proto_display "${proto:-both}")) -> [${dip}]:${dport}"
                [[ -n "$note" ]] && echo "        # 备注: ${note}"
                proto_has_tcp "${proto:-both}" && echo "        tcp dport ${lport} dnat to [${dip}]:${dport}"
                proto_has_udp "${proto:-both}" && echo "        udp dport ${lport} dnat to [${dip}]:${dport}"
            } >> "${tmp_file}"
        done
        cat >> "${tmp_file}" <<EOF
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
EOF
        for rule in "${RULES[@]}"; do
            IFS='|' read -r lport dip dport proto note <<< "$rule"
            [[ "$(ip_family "$dip")" == "ipv6" ]] || continue
            {
                proto_has_tcp "${proto:-both}" && echo "        ip6 daddr ${dip} tcp dport ${dport} ct status dnat snat to \$LOCAL_IP6"
                proto_has_udp "${proto:-both}" && echo "        ip6 daddr ${dip} udp dport ${dport} ct status dnat snat to \$LOCAL_IP6"
            } >> "${tmp_file}"
        done
        echo "    }" >> "${tmp_file}"
        echo "}" >> "${tmp_file}"
    fi

    mv -f "${tmp_file}" "${CONF_FILE}" 2>/dev/null || { err "无法写入配置文件 ${CONF_FILE}"; rm -f "${tmp_file}" 2>/dev/null || true; return 1; }
}

reload_rules() {
    nft delete table ip "${TABLE_NAME}" 2>/dev/null || true
    nft delete table ip6 "${TABLE_NAME}" 2>/dev/null || true
    nft -f "${CONF_FILE}" || { err "加载配置文件失败，请检查 ${CONF_FILE}"; return 1; }
    return 0
}

# ============== 备份与恢复 ==============
LAST_BACKUP=""

backup_conf() {
    LAST_BACKUP=""
    [[ -f "${CONF_FILE}" ]] || return 1

    mkdir -p "${BACKUP_DIR}" 2>/dev/null || return 1
    local ts backup_file
    ts=$(date '+%Y%m%d_%H%M%S')
    backup_file="${BACKUP_DIR}/port-forward.conf.${ts}"
    while [[ -e "${backup_file}" ]]; do
        backup_file="${BACKUP_DIR}/port-forward.conf.${ts}.${RANDOM}"
    done

    cp "${CONF_FILE}" "${backup_file}" 2>/dev/null || return 1
    LAST_BACKUP="${backup_file}"
    return 0
}

load_backup_files() {
    BACKUP_FILES=()
    local file
    mkdir -p "${BACKUP_DIR}" 2>/dev/null || return 1
    for file in "${BACKUP_DIR}"/port-forward.conf.*; do
        [[ -f "${file}" ]] && BACKUP_FILES+=("${file}")
    done
    return 0
}

list_backups() {
    load_backup_files || { err "无法读取备份目录。"; return 1; }
    if [[ ${#BACKUP_FILES[@]} -eq 0 ]]; then
        info "暂无备份。"
        return 1
    fi

    printf "\n%-4s %s\n" "序号" "备份文件"
    echo "────────────────────────────────────────"
    local i
    for ((i=0; i<${#BACKUP_FILES[@]}; i++)); do
        printf "%-4s %s\n" "$((i + 1))" "$(basename "${BACKUP_FILES[$i]}")"
    done
}

create_manual_backup() {
    if backup_conf; then
        info "已创建备份: $(basename "${LAST_BACKUP}")"
        log_action "手动备份配置: $(basename "${LAST_BACKUP}")"
    else
        err "创建备份失败：当前转发配置不存在。"
    fi
}

restore_backup() {
    list_backups || return

    local choice selected rollback_backup rule lport dip dport proto note
    read -rp "请输入要恢复的序号 (0 取消): " choice
    [[ "$choice" == "0" || -z "$choice" ]] && return
    if [[ ! "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#BACKUP_FILES[@]} )); then
        err "无效的序号。"
        return
    fi
    selected="${BACKUP_FILES[$((choice - 1))]}"

    warn "将恢复: $(basename "${selected}")"
    read -rp "输入 RESTORE 确认恢复: " confirm
    [[ "$confirm" == "RESTORE" ]] || { info "已取消。"; return; }

    backup_conf || { err "无法备份当前配置，已取消恢复。"; return; }
    rollback_backup="${LAST_BACKUP}"
    cp "${selected}" "${CONF_FILE}" 2>/dev/null || { err "写入备份失败。"; return; }

    load_rules
    if reload_rules; then
        for rule in "${RULES[@]}"; do
            IFS='|' read -r lport dip dport proto note <<< "${rule}"
            firewall_open_port "${lport}" "${dip}" "${dport}" "${proto:-both}"
        done
        info "已恢复备份: $(basename "${selected}")"
        log_action "恢复备份: $(basename "${selected}")"
    else
        err "恢复加载失败，正在回滚到恢复前的配置。"
        cp "${rollback_backup}" "${CONF_FILE}" 2>/dev/null || true
        load_rules
        reload_rules || err "自动回滚加载失败，请手动检查 ${rollback_backup}。"
    fi
}

do_backup_restore() {
    while true; do
        clear_screen
        echo "========================================"
        echo "             备份与恢复"
        echo "========================================"
        echo "  1) 查看备份"
        echo "  2) 立即创建备份"
        echo "  3) 恢复备份"
        echo "  0) 返回主菜单"
        echo "========================================"
        read -rp "请选择操作 [0-3]: " choice

        case "$choice" in
            1) list_backups; pause_screen ;;
            2) create_manual_backup; pause_screen ;;
            3) restore_backup; pause_screen ;;
            0) return ;;
            *) err "无效选择，请输入 0-3。"; pause_screen ;;
        esac
    done
}

# ============== 开启内核参数：IP 转发 + BBR/fq ==============
enable_ip_forward() {
    local current
    current=$(sysctl -n net.ipv4.ip_forward 2>/dev/null) || current="0"
    if [[ "$current" != "1" ]]; then
        if sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; then
            info "已开启 IPv4 转发。"
        else
            warn "无法开启 IPv4 转发，请手动执行: sysctl -w net.ipv4.ip_forward=1"
        fi
    fi

    # 持久化：统一替换所有匹配行为 =1，没有则追加（避免重复项导致后值覆盖前值的误判）
    mkdir -p "$(dirname "${SYSCTL_CONF}")" 2>/dev/null || true
    touch "${SYSCTL_CONF}" 2>/dev/null || true

    if grep -qE '^[[:space:]]*net\.ipv4\.ip_forward[[:space:]]*=' "${SYSCTL_CONF}" 2>/dev/null; then
        sed -i -E 's|^[[:space:]]*net\.ipv4\.ip_forward[[:space:]]*=.*|net.ipv4.ip_forward=1|' "${SYSCTL_CONF}" 2>/dev/null || true
    else
        echo "net.ipv4.ip_forward=1" >> "${SYSCTL_CONF}" 2>/dev/null || true
    fi

    sysctl -p "${SYSCTL_CONF}" >/dev/null 2>&1 || true
}

enable_ip6_forward() {
    local current
    current=$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null) || current="0"
    if [[ "$current" != "1" ]]; then
        sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1 || {
            err "无法开启 IPv6 转发，请检查内核是否支持 IPv6。"
            return 1
        }
        info "已开启 IPv6 转发。"
    fi
    mkdir -p "$(dirname "${SYSCTL_CONF}")" 2>/dev/null || true
    touch "${SYSCTL_CONF}" 2>/dev/null || true
    if grep -qE '^[[:space:]]*net\.ipv6\.conf\.all\.forwarding[[:space:]]*=' "${SYSCTL_CONF}" 2>/dev/null; then
        sed -i -E 's|^[[:space:]]*net\.ipv6\.conf\.all\.forwarding[[:space:]]*=.*|net.ipv6.conf.all.forwarding=1|' "${SYSCTL_CONF}" 2>/dev/null || true
    else
        echo "net.ipv6.conf.all.forwarding=1" >> "${SYSCTL_CONF}" 2>/dev/null || true
    fi
}

enable_bbr_fq() {
    # 1) 内核是否支持 bbr
    modprobe tcp_bbr 2>/dev/null || true
    if ! grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null; then
        warn "内核不支持 BBR（tcp_available_congestion_control 中未找到 bbr），已跳过。"
        return 0
    fi

    # 2) 读取当前配置
    local cur_cc cur_qd
    cur_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) || cur_cc=""
    cur_qd=$(sysctl -n net.core.default_qdisc 2>/dev/null) || cur_qd=""

    # 3) 判断是否已经开启
    if [[ "$cur_cc" == "bbr" && "$cur_qd" == "fq" ]]; then
        info "BBR + fq 已启用（无需修改）。"
        return 0
    fi

    # 4) 没开则开启（立即生效）
    sysctl -w net.core.default_qdisc=fq >/dev/null 2>&1 || true
    sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null 2>&1 || true

    # 再读一次确认
    cur_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) || cur_cc=""
    cur_qd=$(sysctl -n net.core.default_qdisc 2>/dev/null) || cur_qd=""

    if [[ "$cur_cc" == "bbr" && "$cur_qd" == "fq" ]]; then
        info "已开启 BBR + fq。"
        log_action "开启 BBR+fq"
    else
        warn "尝试开启 BBR+fq 后未确认生效（当前: cc=${cur_cc:-?}, qdisc=${cur_qd:-?}），可能被系统配置覆盖。"
    fi

    # 5) 持久化：写入 SYSCTL_CONF（用“替换/追加”避免覆盖别的项）
    mkdir -p "$(dirname "${SYSCTL_CONF}")" 2>/dev/null || true
    touch "${SYSCTL_CONF}" 2>/dev/null || true

    if grep -qE '^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=' "${SYSCTL_CONF}"; then
        sed -i -E 's|^[[:space:]]*net\.core\.default_qdisc[[:space:]]*=.*|net.core.default_qdisc=fq|' "${SYSCTL_CONF}" 2>/dev/null || true
    else
        echo "net.core.default_qdisc=fq" >> "${SYSCTL_CONF}" 2>/dev/null || true
    fi

    if grep -qE '^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=' "${SYSCTL_CONF}"; then
        sed -i -E 's/^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=.*/net.ipv4.tcp_congestion_control=bbr/' "${SYSCTL_CONF}" 2>/dev/null || true
    else
        echo "net.ipv4.tcp_congestion_control=bbr" >> "${SYSCTL_CONF}" 2>/dev/null || true
    fi

    sysctl -p "${SYSCTL_CONF}" >/dev/null 2>&1 || true
    info "已持久化 BBR + fq 到 ${SYSCTL_CONF}。"
    log_action "持久化 BBR+fq 到 ${SYSCTL_CONF}"
}

# ============== 检测防火墙状态（仅提示） ==============
check_firewall_status() {
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        info "检测到 firewalld 正在运行，添加转发规则时将自动放行对应端口。"
    elif command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw "active"; then
        info "检测到 UFW 正在运行，添加转发规则时将自动放行对应端口。"
    elif has_iptables; then
        info "检测到 iptables 规则集存在，添加转发规则时将自动放行对应端口。"
    fi
}

# ============== 服务与持久化管理 ==============
show_service_persistence_status() {
    echo ""
    echo "--- 服务与持久化状态 ---"

    if command -v systemctl &>/dev/null; then
        local enabled active
        enabled=$(systemctl is-enabled nftables 2>/dev/null) || enabled="未启用"
        active=$(systemctl is-active nftables 2>/dev/null) || active="未运行"
        info "nftables 开机启动: ${enabled}"
        info "nftables 服务状态: ${active}"
    else
        warn "未检测到 systemctl，无法管理 nftables 服务。"
    fi

    if [[ -f "${MAIN_CONF}" ]] && grep -qF 'include "/etc/nftables.d/*.conf"' "${MAIN_CONF}" 2>/dev/null; then
        info "主配置 include: 已配置"
    else
        warn "主配置 include: 未配置"
    fi

    if [[ -f "${CONF_FILE}" ]]; then
        info "转发配置文件: ${CONF_FILE}"
    else
        warn "转发配置文件: 不存在"
    fi

    if command -v nft &>/dev/null && { nft list table ip "${TABLE_NAME}" &>/dev/null || nft list table ip6 "${TABLE_NAME}" &>/dev/null; }; then
        info "运行中转发表: 已加载"
    else
        warn "运行中转发表: 未加载"
    fi
}

repair_main_conf_include() {
    local include_line='include "/etc/nftables.d/*.conf"'
    if [[ -f "${MAIN_CONF}" ]] && grep -qF "$include_line" "${MAIN_CONF}" 2>/dev/null; then
        info "主配置已包含 include 指令，无需修复。"
        return
    fi

    warn "此操作只会创建或补充 ${MAIN_CONF} 的 include 指令，不会清空 nftables 规则。"
    local confirm
    read -rp "确认修复？[y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        info "已取消。"
        return
    fi

    if [[ -f "${MAIN_CONF}" ]]; then
        local ts backup
        ts=$(date '+%Y%m%d_%H%M%S')
        backup="${MAIN_CONF}.bak.${ts}"
        if ! cp "${MAIN_CONF}" "$backup" 2>/dev/null; then
            err "无法备份 ${MAIN_CONF}，已取消修复。"
            return
        fi
        printf '\n%s\n' "$include_line" >> "${MAIN_CONF}" || {
            err "无法写入 ${MAIN_CONF}"
            return
        }
        info "已添加 include 指令；原文件备份: ${backup}"
    else
        mkdir -p "$(dirname "${MAIN_CONF}")" 2>/dev/null || true
        cat > "${MAIN_CONF}" <<EOF
#!/usr/sbin/nft -f
${include_line}
EOF
        info "已创建最小主配置: ${MAIN_CONF}"
    fi
    log_action "修复 nftables 主配置 include"
}

do_service_management() {
    while true; do
        clear_screen
        echo "========================================"
        echo "         服务与持久化管理"
        echo "========================================"
        echo "  1) 查看服务与持久化状态"
        echo "  2) 启用并立即启动 nftables"
        echo "  3) 重载本脚本转发规则"
        echo "  4) 修复主配置 include（不清空规则）"
        echo "  5) 返回主菜单"
        echo "========================================"
        read -rp "请选择操作 [1-5]: " choice

        case "$choice" in
            1) show_service_persistence_status; pause_screen ;;
            2)
                if ! command -v systemctl &>/dev/null; then
                    err "未检测到 systemctl，请手动启动 nftables 服务。"
                elif systemctl enable --now nftables 2>/dev/null; then
                    info "nftables 已启用开机自启并正在运行。"
                    log_action "启用并启动 nftables 服务"
                else
                    err "nftables 服务启用失败，请检查 systemctl status nftables。"
                fi
                pause_screen
                ;;
            3)
                if [[ ! -f "${CONF_FILE}" ]]; then
                    err "未找到 ${CONF_FILE}，请先新增一条转发规则。"
                elif ! command -v nft &>/dev/null; then
                    err "nftables 未安装。"
                elif reload_rules; then
                    info "本脚本转发规则已重载。"
                    log_action "重载端口转发规则"
                fi
                pause_screen
                ;;
            4) repair_main_conf_include; pause_screen ;;
            5) return ;;
            *) err "无效选择，请输入 1-5。"; pause_screen ;;
        esac
    done
}

# ============== 诊断/自检 ==============
do_diagnose() {
    echo ""
    echo "========================================"
    echo "           诊断 / 自检"
    echo "========================================"

    # 1. IP 转发
    local ip_fwd
    ip_fwd=$(sysctl -n net.ipv4.ip_forward 2>/dev/null) || ip_fwd="未知"
    if [[ "$ip_fwd" == "1" ]]; then
        info "IPv4 转发: 已开启"
    else
        err  "IPv4 转发: 未开启 (当前值: ${ip_fwd})"
        echo "  → 修复: sysctl -w net.ipv4.ip_forward=1"
    fi

    if has_usable_ipv6; then
        local ip6_fwd
        ip6_fwd=$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null) || ip6_fwd="未知"
        [[ "$ip6_fwd" == "1" ]] && info "IPv6 转发: 已开启" || warn "IPv6 转发: 未开启（创建 IPv6 规则时会自动开启）"
    else
        info "IPv6: 本机未检测到可用 IPv6"
    fi

    # 2. nftables 状态
    if command -v nft &>/dev/null; then
        info "nftables: 已安装 ($(nft --version 2>/dev/null || echo '未知版本'))"
    else
        err  "nftables: 未安装"
        echo "  → 修复: 选择菜单【安装 nftables】"
    fi

    local svc_enabled svc_active
    svc_enabled=$(systemctl is-enabled nftables 2>/dev/null) || svc_enabled="unknown"
    svc_active=$(systemctl is-active nftables 2>/dev/null) || svc_active="unknown"

    if [[ "$svc_enabled" == "enabled" ]]; then
        info "nftables 开机启动: 是"
    else
        warn "nftables 开机启动: 否（重启后规则可能丢失）"
        echo "  → 修复: 选择菜单【服务与持久化管理】→【启用并立即启动 nftables】"
    fi

    if [[ "$svc_active" == "active" ]]; then
        info "nftables 服务状态: 运行中"
    else
        warn "nftables 服务状态: 未运行"
        echo "  → 修复: 选择菜单【服务与持久化管理】→【启用并立即启动 nftables】"
    fi

    # 3. 转发规则是否加载
    if nft list table ip "${TABLE_NAME}" &>/dev/null || nft list table ip6 "${TABLE_NAME}" &>/dev/null; then
        load_rules
        info "转发规则表: 已加载（${#RULES[@]} 条转发规则）"
    else
        warn "转发规则表: 未加载（可能无规则或服务未启动）"
    fi

    # 4. 防火墙检测
    echo ""
    echo "--- 防火墙状态 ---"
    local fw_found=false

    if systemctl is-active --quiet firewalld 2>/dev/null; then
        fw_found=true
        info "firewalld: 活跃"
    fi

    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw "active"; then
        fw_found=true
        warn "UFW: 活跃（默认会阻止入站连接，可能影响转发）"
    fi

    if ! $fw_found && has_iptables; then
        fw_found=true
        local fwd_policy
        fwd_policy=$(iptables -S FORWARD 2>/dev/null | grep -- '^-P FORWARD' | awk '{print $3}') || fwd_policy=""
        if [[ "$fwd_policy" == "DROP" || "$fwd_policy" == "REJECT" ]]; then
            warn "iptables FORWARD 默认策略: ${fwd_policy}（可能阻止转发流量）"
        else
            info "iptables FORWARD 默认策略: ${fwd_policy:-ACCEPT}"
        fi
    fi

    if ! $fw_found; then
        info "未检测到活跃的防火墙 (firewalld / UFW / iptables)"
    fi

    # 5. nftables forward 链检测
    echo ""
    echo "--- nftables forward 链 ---"
    local fwd_chains
    fwd_chains=$(nft list chains 2>/dev/null | grep -B1 "hook forward" || true)
    if [[ -n "$fwd_chains" ]]; then
        if echo "$fwd_chains" | grep -qi "drop"; then
            warn "检测到 nftables 存在 forward 链默认策略为 drop"
            echo "  这会阻止所有转发流量，需手动添加放行规则。"
            echo "  查看详情: nft list ruleset | grep -A5 'hook forward'"
        else
            info "nftables forward 链: 未发现 drop 策略"
        fi
    else
        info "未检测到 nftables forward 链（正常，不影响转发）"
    fi

    # 6. 配置持久化
    echo ""
    echo "--- 配置持久化 ---"
    if [[ -f "${MAIN_CONF}" ]]; then
        if grep -qF 'include "/etc/nftables.d/*.conf"' "${MAIN_CONF}" 2>/dev/null; then
            info "主配置 ${MAIN_CONF}: 已包含 include 指令"
        else
            warn "主配置 ${MAIN_CONF}: 缺少 include 指令（重启后规则可能丢失）"
            echo "  → 修复: 选择菜单【服务与持久化管理】→【修复主配置 include】"
        fi
    else
        warn "主配置 ${MAIN_CONF}: 不存在（重启后规则可能丢失）"
        echo "  → 修复: 选择菜单【服务与持久化管理】→【修复主配置 include】"
    fi

    if [[ -f "${CONF_FILE}" ]]; then
        info "转发配置文件: ${CONF_FILE} 存在"
    else
        info "转发配置文件: 尚未创建（添加首条规则时自动生成）"
    fi

    # 7. 目标连通性测试（可选）
    echo ""
    load_rules
    if [[ ${#RULES[@]} -gt 0 ]]; then
        read -rp "是否测试目标连通性？[y/N]: " test_conn
        if [[ "$test_conn" =~ ^[Yy]$ ]]; then
            local rule lport dip dport proto note
            for rule in "${RULES[@]}"; do
                IFS='|' read -r lport dip dport proto note <<< "$rule"
                proto="${proto:-both}"

                # TCP 测试：建立连接即视为通
                if proto_has_tcp "$proto"; then
                    printf "  测试 %s:%s (TCP) ... " "$dip" "$dport"
                    if timeout 3 bash -c ">/dev/tcp/${dip}/${dport}" 2>/dev/null; then
                        printf "\033[32m通\033[0m\n"
                    else
                        printf "\033[31m不通或超时\033[0m\n"
                    fi
                fi

                # UDP 测试：UDP 无连接，只能做弱判定
                if proto_has_udp "$proto"; then
                    printf "  测试 %s:%s (UDP) ... " "$dip" "$dport"
                    if test_udp_reachable "$dip" "$dport"; then
                        printf "\033[32m可达\033[0m\n"
                    else
                        printf "\033[31m不可达\033[0m\n"
                    fi
                fi
            done
        fi
    fi
    echo ""
}

# ====================================================
# 功能 1：安装 nftables
# ====================================================
do_install() {
    echo ""
    if command -v nft &>/dev/null; then
        info "nftables 已安装。"
        nft --version 2>/dev/null || true
        echo ""
        warn "安装将清空所有已有 nftables 配置，由本脚本统一接管。"
        warn "已有的配置文件将被备份（重命名为 .bak）。"
        read -rp "是否继续？[y/N]: " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            info "已取消，退出脚本。"
            exit 0
        fi

        # 备份已有配置文件（重命名，不删除）
        local ts
        ts=$(date '+%Y%m%d_%H%M%S')
        if [[ -f "${MAIN_CONF}" ]]; then
            mv "${MAIN_CONF}" "${MAIN_CONF}.bak.${ts}" 2>/dev/null || true
            info "已备份 ${MAIN_CONF} → ${MAIN_CONF}.bak.${ts}"
        fi
        if [[ -d "${CONF_DIR}" ]]; then
            local f
            for f in "${CONF_DIR}"/*.conf; do
                [[ -f "$f" ]] || continue
                mv "$f" "${f}.bak.${ts}" 2>/dev/null || true
                info "已备份 ${f} → ${f}.bak.${ts}"
            done
        fi

        # 清空当前运行中的规则
        nft flush ruleset 2>/dev/null || true
        info "已清空当前 nftables 规则集。"
        log_action "清空已有配置并由脚本接管 (备份时间戳: ${ts})"

        enable_ip_forward
        enable_bbr_fq
        check_firewall_status
        init_conf

        # 加载主配置（flush + include），验证整条配置链路
        if ! nft -f "${MAIN_CONF}"; then
            err "加载 ${MAIN_CONF} 失败，请检查配置。"
            return
        fi

        # 确保服务开机启动且当前正在运行
        if systemctl enable --now nftables 2>/dev/null; then
            info "已启用 nftables 服务。"
        else
            warn "nftables 服务启用失败，重启后规则可能丢失。"
            warn "请手动执行: systemctl enable --now nftables"
        fi

        info "初始化完成，所有配置已由本脚本接管。"
        return
    fi

    info "未检测到 nftables，准备安装..."
    local pkg_mgr
    pkg_mgr=$(detect_pkg_manager)

    case "$pkg_mgr" in
        apt)
            apt-get update -y && apt-get install -y nftables
            ;;
        dnf)
            dnf install -y nftables
            ;;
        yum)
            yum install -y nftables
            ;;
        pacman)
            pacman -Sy --noconfirm nftables
            ;;
        *)
            err "无法识别包管理器，请手动安装 nftables。"
            return
            ;;
    esac

    if ! command -v nft &>/dev/null; then
        err "安装失败，请手动安装 nftables。"
        return
    fi

    info "nftables 安装成功。"
    nft --version 2>/dev/null || true
    log_action "安装 nftables"

    enable_ip_forward
    enable_bbr_fq
    check_firewall_status
    init_conf
    # 先写好配置，再启用服务，确保服务启动时直接加载我们的配置
    if systemctl enable --now nftables 2>/dev/null; then
        info "已启用 nftables 服务。"
    else
        warn "nftables 服务启用失败，重启后规则可能丢失。"
        warn "请手动执行: systemctl enable --now nftables"
    fi

    info "安装与初始化完成。"
}

# ====================================================
# 功能 2：查看现有端口转发
# ====================================================
print_rules() {
    printf "\n\033[1m%-6s %-8s %-10s %-10s    %-28s %s\033[0m\n" "序号" "IP类型" "协议" "本机端口" "目标地址" "备注"
    echo "────────────────────────────────────────────────────────────────────────────────"
    local idx=1 rule lport dip dport proto note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r lport dip dport proto note <<< "$rule"
        local target="${dip}:${dport}"
        [[ "$(ip_family "$dip")" == "ipv6" ]] && target="[${dip}]:${dport}"
        printf "%-6s %-8s %-10s %-10s -> %-28s %s\n" "$idx" "$(family_display "$(ip_family "$dip")")" "$(proto_display "${proto:-both}")" "$lport" "$target" "${note:--}"
        ((idx++))
    done
    echo ""
}

do_list() {
    echo ""
    load_rules
    [[ ${#RULES[@]} -gt 0 ]] && print_rules || info "当前没有端口转发规则。"
}

# ====================================================
# 功能 3：新增端口转发
# ====================================================
do_add() {
    echo ""
    command -v nft &>/dev/null || { err "nftables 未安装，请先选择 [1] 安装。"; return; }
    init_conf || return
    load_rules

    local family lport proto proto_choice dip dport note rule rp existing_dip
    choose_ip_family
    family="$SELECTED_FAMILY"
    if [[ "$family" == "ipv6" ]]; then enable_ip6_forward || return; else enable_ip_forward; fi

    while true; do
        read -rp "请输入本机监听端口 (1-65535): " lport
        validate_port "$lport" && break
        err "端口无效，请输入 1-65535 之间的数字。"
    done
    while true; do
        echo "请选择转发协议:"
        echo "  1) TCP"
        echo "  2) UDP"
        echo "  3) TCP + UDP"
        read -rp "请选择 [1-3，默认 3]: " proto_choice
        proto=$(normalize_proto "${proto_choice:-3}")
        [[ -n "$proto" ]] && break
        err "无效选择，请输入 1、2 或 3。"
    done
    for rule in "${RULES[@]}"; do
        IFS='|' read -r rp existing_dip _ <<< "$rule"
        [[ "$rp" == "$lport" && "$(ip_family "$existing_dip")" == "$family" ]] && { err "该 $(family_display "$family") 监听端口已存在转发规则。"; return; }
    done
    check_port_conflict "$lport" || { info "已取消。"; return; }

    while true; do
        read -rp "请输入目标 $(family_display "$family") 地址: " dip
        if [[ "$family" == "ipv6" ]]; then validate_ipv6 "$dip"; else validate_ipv4 "$dip"; fi && break
        err "IP 地址格式无效，请重新输入。"
    done
    while true; do
        read -rp "请输入目标端口 (1-65535) [默认: ${lport}]: " dport
        dport="${dport:-$lport}"
        validate_port "$dport" && break
        err "端口无效，请输入 1-65535 之间的数字。"
    done
    read -rp "请输入备注（可留空）: " note
    note=$(sanitize_note "$note")

    echo ""
    echo "即将添加 $(family_display "$family") 转发规则:"
    [[ "$family" == "ipv6" ]] && echo "  本机端口 ${lport} ($(proto_display "$proto")) → [${dip}]:${dport}" || echo "  本机端口 ${lport} ($(proto_display "$proto")) → ${dip}:${dport}"
    [[ -n "$note" ]] && echo "  备注: ${note}"
    read -rp "确认添加？[Y/n]: " confirm
    [[ "$confirm" =~ ^[Nn]$ ]] && { info "已取消。"; return; }

    backup_conf
    RULES+=("${lport}|${dip}|${dport}|${proto}|${note}")
    write_conf_file || return
    if reload_rules; then
        firewall_open_port "$lport" "$dip" "$dport" "$proto"
        info "转发规则添加成功。"
        log_action "新增 $(family_display "$family") 转发: ${lport} -> ${dip}:${dport}"
    else
        err "规则加载失败，请检查配置。"
    fi
}

# ====================================================
# 功能 4：修改端口转发
# ====================================================
do_edit() {
    echo ""
    command -v nft &>/dev/null || { err "nftables 未安装，请先选择 [1] 安装。"; return; }
    load_rules
    [[ ${#RULES[@]} -gt 0 ]] || { info "当前没有端口转发规则，无需修改。"; return; }
    print_rules

    local choice idx target old_lport old_dip old_dport old_proto old_note old_family family lport dip dport proto proto_choice note_input note rule rp existing_dip
    read -rp "请输入要修改的序号 (0 取消): " choice
    [[ "$choice" == "0" || -z "$choice" ]] && { info "已取消。"; return; }
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#RULES[@]} )) || { err "无效的序号。"; return; }

    idx=$((choice - 1)); target="${RULES[$idx]}"
    IFS='|' read -r old_lport old_dip old_dport old_proto old_note <<< "$target"
    old_proto="${old_proto:-both}"; old_family=$(ip_family "$old_dip")

    echo "留空保留当前值。"
    choose_ip_family "$([[ "$old_family" == "ipv6" ]] && echo 2 || echo 1)"
    family="$SELECTED_FAMILY"
    if [[ "$family" == "ipv6" ]]; then enable_ip6_forward || return; else enable_ip_forward; fi

    read -rp "本机监听端口 [${old_lport}]: " lport
    lport="${lport:-$old_lport}"
    validate_port "$lport" || { err "端口无效。"; return; }
    for ((i=0; i<${#RULES[@]}; i++)); do
        (( i == idx )) && continue
        IFS='|' read -r rp existing_dip _ <<< "${RULES[$i]}"
        [[ "$rp" == "$lport" && "$(ip_family "$existing_dip")" == "$family" ]] && { err "该 $(family_display "$family") 监听端口已存在规则。"; return; }
    done
    [[ "$lport" == "$old_lport" ]] || check_port_conflict "$lport" || { info "已取消。"; return; }

    while true; do
        read -rp "协议 [1 TCP/2 UDP/3 TCP+UDP，当前 $(proto_display "$old_proto")]: " proto_choice
        [[ -z "$proto_choice" ]] && { proto="$old_proto"; break; }
        proto=$(normalize_proto "$proto_choice")
        [[ -n "$proto" ]] && break
        err "无效选择，请输入 1、2 或 3。"
    done

    if [[ "$family" == "$old_family" ]]; then
        read -rp "目标 $(family_display "$family") 地址 [${old_dip}]: " dip
        dip="${dip:-$old_dip}"
    else
        read -rp "请输入目标 $(family_display "$family") 地址: " dip
    fi
    if [[ "$family" == "ipv6" ]]; then validate_ipv6 "$dip"; else validate_ipv4 "$dip"; fi || { err "IP 地址格式无效。"; return; }

    read -rp "目标端口 [${old_dport}]: " dport
    dport="${dport:-$old_dport}"
    validate_port "$dport" || { err "端口无效。"; return; }

    read -rp "备注 [${old_note:--}；留空保留，输入 - 清空]: " note_input
    if [[ "$note_input" == "-" ]]; then note=""; elif [[ -z "$note_input" ]]; then note="$old_note"; else note=$(sanitize_note "$note_input"); fi

    echo ""
    echo "即将修改为 $(family_display "$family") 转发:"
    [[ "$family" == "ipv6" ]] && echo "  本机端口 ${lport} ($(proto_display "$proto")) → [${dip}]:${dport}" || echo "  本机端口 ${lport} ($(proto_display "$proto")) → ${dip}:${dport}"
    [[ -n "$note" ]] && echo "  备注: ${note}"
    read -rp "确认修改？[Y/n]: " confirm
    [[ "$confirm" =~ ^[Nn]$ ]] && { info "已取消。"; return; }

    local network_changed=false
    [[ "$old_lport" != "$lport" || "$old_dip" != "$dip" || "$old_dport" != "$dport" || "$old_proto" != "$proto" ]] && network_changed=true
    backup_conf
    RULES[$idx]="${lport}|${dip}|${dport}|${proto}|${note}"
    write_conf_file || return
    if ! $network_changed; then info "备注已更新。"; return; fi
    if reload_rules; then
        firewall_close_port "$old_lport" "$old_dip" "$old_dport" "$old_proto"
        firewall_open_port "$lport" "$dip" "$dport" "$proto"
        info "转发规则修改成功。"
    else
        err "规则加载失败，请检查配置。"
    fi
}

# ====================================================
# 功能 5：删除端口转发
# ====================================================
do_delete() {
    echo ""
    if ! command -v nft &>/dev/null; then
        err "nftables 未安装，请先选择 [1] 安装。"
        return
    fi

    load_rules

    if [[ ${#RULES[@]} -eq 0 ]]; then
        info "当前没有端口转发规则，无需删除。"
        return
    fi

    print_rules

    # 选择删除
    local choice
    read -rp "请输入要删除的序号 (0 取消): " choice

    if [[ "$choice" == "0" ]] || [[ -z "$choice" ]]; then
        info "已取消。"
        return
    fi

    if [[ ! "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#RULES[@]} )); then
        err "无效的序号。"
        return
    fi

    local target="${RULES[$((choice-1))]}"
    local note
    IFS='|' read -r lport dip dport proto note <<< "$target"
    proto="${proto:-both}"

    echo "即将删除转发规则:"
    echo "  本机端口 ${lport} ($(proto_display "$proto")) → ${dip}:${dport}"
    [[ -n "$note" ]] && echo "  备注: ${note}"
    read -rp "确认删除？[Y/n]: " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
        info "已取消。"
        return
    fi

    # 备份并移除
    backup_conf
    unset 'RULES[$((choice-1))]'
    RULES=("${RULES[@]}")

    if ! write_conf_file; then
        return
    fi

    if reload_rules; then
        # nft 规则已成功更新后，再清理防火墙放行（RULES 已移除该条，dest_still_used 能正确判断）
        firewall_close_port "$lport" "$dip" "$dport" "$proto"
        info "转发规则已删除: ${lport} ($(proto_display "$proto")) → ${dip}:${dport}"
        log_action "删除转发: ${lport} ($(proto_display "$proto")) -> ${dip}:${dport}"
    else
        err "规则加载失败，请检查配置。"
    fi
}

# ====================================================
# 功能 6：一键清空所有转发
# ====================================================
do_clear_all() {
    echo ""
    if ! command -v nft &>/dev/null; then
        err "nftables 未安装，请先选择 [1] 安装。"
        return
    fi

    load_rules

    if [[ ${#RULES[@]} -eq 0 ]]; then
        info "当前没有端口转发规则，无需清空。"
        return
    fi

    warn "即将清空全部 ${#RULES[@]} 条转发规则！"
    read -rp "确认清空？[y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        info "已取消。"
        return
    fi

    backup_conf

    # 先清理所有防火墙规则（清空场景用 force，无需检查共享）
    local rule lport dip dport proto note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r lport dip dport proto note <<< "$rule"
        firewall_close_port "$lport" "$dip" "$dport" "${proto:-both}" "force"
    done

    RULES=()
    if ! write_conf_file; then
        return
    fi

    if reload_rules; then
        info "所有转发规则已清空。"
        log_action "清空所有转发规则"
    else
        err "规则加载失败，请检查配置。"
    fi
}

# ====================================================
# 主菜单
# ====================================================
main_menu() {
    while true; do
        clear_screen
        echo "========================================"
        echo "   nftables 端口转发管理工具 v1.6"
        echo "========================================"
        echo "  1) 安装 nftables"
        echo "  2) 查看现有端口转发"
        echo "  3) 新增端口转发"
        echo "  4) 修改端口转发"
        echo "  5) 删除端口转发"
        echo "  6) 清空本脚本管理的全部转发"
        echo "  7) 诊断/自检"
        echo "  8) 服务与持久化管理"
        echo "  9) 备份与恢复"
        echo "  0) 退出"
        echo "========================================"
        read -rp "请选择操作 [0-9]: " choice

        case "$choice" in
            1) do_install; pause_screen ;;
            2) do_list; pause_screen ;;
            3) do_add; pause_screen ;;
            4) do_edit; pause_screen ;;
            5) do_delete; pause_screen ;;
            6) do_clear_all; pause_screen ;;
            7) do_diagnose; pause_screen ;;
            8) do_service_management ;;
            9) do_backup_restore ;;
            0) info "再见！"; exit 0 ;;
            *) err "无效选择，请输入 0-9。"; pause_screen ;;
        esac
    done
}

# ============== 入口 ==============
check_root
main_menu

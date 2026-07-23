#!/usr/bin/env bash
#
# NFT Port Forward v2.0
# 新配置格式：不兼容 v1.x。请在升级前自行删除旧配置。
#

set -o pipefail

STATE_DIR="/etc/nft-port-forward"
RULES_FILE="${STATE_DIR}/rules.db"
SETTINGS_FILE="${STATE_DIR}/settings.conf"
BACKUP_DIR="${STATE_DIR}/backups"
CONF_DIR="/etc/nftables.d"
CONF_FILE="${CONF_DIR}/nft-port-forward.conf"
MAIN_CONF="/etc/nftables.conf"
TABLE_NAME="nft_port_forward"
LOG_FILE="/var/log/nft-port-forward.log"

declare -a RULES=()
SNAT_MODE="fixed"
FIXED_SNAT_IP=""

info() { printf '\033[32m[信息]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[警告]\033[0m %s\n' "$1"; }
err() { printf '\033[31m[错误]\033[0m %s\n' "$1"; }

log_action() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1" >> "${LOG_FILE}" 2>/dev/null || true
}

pause() { read -rp "按 Enter 返回..." _; }

clear_screen() { command -v clear &>/dev/null && clear || true; }

draw_header() {
    clear_screen
    local count="0" service="未知"
    load_rules
    count="${#RULES[@]}"
    if command -v systemctl &>/dev/null; then
        service=$(systemctl is-active nftables 2>/dev/null || echo "未运行")
    fi
    echo "╭────────────────────────────────────────────────────╮"
    echo "│              NFT Port Forward v2.0                 │"
    printf "│ 服务：%-9s  规则：%-4s 条  回源：%-8s │\n" "$service" "$count" "$(snat_mode_display "$SNAT_MODE")"
    echo "╰────────────────────────────────────────────────────╯"
}

check_root() {
    if [[ ${EUID} -ne 0 ]]; then
        err "此脚本需要 root 权限运行。"
        exit 1
    fi
}

validate_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && [[ ! "$1" =~ ^0[0-9] ]] && (( $1 >= 1 && $1 <= 65535 ))
}

validate_ip() {
    local ip="$1" octet
    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    [[ "$ip" =~ (^|\.)0[0-9] ]] && return 1
    local IFS='.'
    read -r -a octets <<< "$ip"
    for octet in "${octets[@]}"; do (( octet <= 255 )) || return 1; done
}

sanitize_note() {
    local note="${1:-}"
    note="${note//$'\r'/ }"
    note="${note//$'\n'/ }"
    note="${note//|/ }"
    printf '%s' "$note"
}

normalize_protocol() {
    case "$1" in
        1|tcp|TCP) echo "tcp" ;;
        2|udp|UDP) echo "udp" ;;
        3|tcp_udp|TCP_UDP|tcp+udp|TCP+UDP) echo "tcp_udp" ;;
        *) echo "" ;;
    esac
}

protocol_display() {
    case "$1" in
        tcp) echo "TCP" ;;
        udp) echo "UDP" ;;
        *) echo "TCP+UDP" ;;
    esac
}

has_tcp() { [[ "$1" == "tcp" || "$1" == "tcp_udp" ]]; }
has_udp() { [[ "$1" == "udp" || "$1" == "tcp_udp" ]]; }

normalize_snat_mode() {
    case "$1" in
        fixed|FIXED|1|固定) echo "fixed" ;;
        auto|AUTO|2|自动) echo "auto" ;;
        *) echo "" ;;
    esac
}

snat_mode_display() {
    [[ "$1" == "auto" ]] && echo "自动" || echo "固定"
}

get_local_ip() {
    local ip
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/ src / {for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}') || true
    [[ -n "$ip" ]] && { printf '%s' "$ip"; return; }
    ip=$(ip -4 addr show scope global 2>/dev/null | awk '/inet / {sub(/\/.*/, "", $2); print $2; exit}') || true
    [[ -n "$ip" ]] && { printf '%s' "$ip"; return; }
    hostname -I 2>/dev/null | awk '{print $1}' || true
}

ensure_state() {
    mkdir -p "${STATE_DIR}" "${BACKUP_DIR}" "${CONF_DIR}" || return 1
    touch "${RULES_FILE}" "${LOG_FILE}" || return 1
    if [[ ! -f "${SETTINGS_FILE}" ]]; then
        local detected
        detected=$(get_local_ip)
        cat > "${SETTINGS_FILE}" <<EOF
SNAT_MODE=fixed
FIXED_SNAT_IP=${detected}
EOF
    fi
    load_settings
}

load_settings() {
    SNAT_MODE="fixed"
    FIXED_SNAT_IP=""
    [[ -f "${SETTINGS_FILE}" ]] || return
    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            SNAT_MODE) SNAT_MODE=$(normalize_snat_mode "$value") ;;
            FIXED_SNAT_IP) validate_ip "$value" && FIXED_SNAT_IP="$value" ;;
        esac
    done < "${SETTINGS_FILE}"
    SNAT_MODE="${SNAT_MODE:-fixed}"
}

save_settings() {
    cat > "${SETTINGS_FILE}" <<EOF
SNAT_MODE=${SNAT_MODE}
FIXED_SNAT_IP=${FIXED_SNAT_IP}
EOF
}

load_rules() {
    RULES=()
    [[ -f "${RULES_FILE}" ]] || return
    local line id lport proto dip dport note
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        IFS='|' read -r id lport proto dip dport note <<< "$line"
        validate_port "$lport" && validate_port "$dport" && validate_ip "$dip" || continue
        proto=$(normalize_protocol "$proto")
        [[ -n "$proto" ]] || continue
        RULES+=("${id}|${lport}|${proto}|${dip}|${dport}|${note}")
    done < "${RULES_FILE}"
}

save_rules() {
    local tmp="${RULES_FILE}.tmp.$$" rule
    : > "$tmp" || return 1
    for rule in "${RULES[@]}"; do printf '%s\n' "$rule" >> "$tmp" || return 1; done
    mv -f "$tmp" "${RULES_FILE}"
}

next_rule_id() {
    local max=0 rule id
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id _ <<< "$rule"
        [[ "$id" =~ ^[0-9]+$ ]] && (( id > max )) && max=$id
    done
    echo $((max + 1))
}

find_rule_index() {
    local wanted="$1" rule id i
    for ((i=0; i<${#RULES[@]}; i++)); do
        IFS='|' read -r id _ <<< "${RULES[$i]}"
        [[ "$id" == "$wanted" ]] && { echo "$i"; return 0; }
    done
    return 1
}

port_in_use_by_rule() {
    local port="$1" except_id="${2:-}" rule id lport
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id lport _ <<< "$rule"
        [[ "$id" != "$except_id" && "$lport" == "$port" ]] && return 0
    done
    return 1
}

create_backup() {
    ensure_state || return 1
    local stamp stage archive
    stamp=$(date '+%Y%m%d_%H%M%S')
    stage=$(mktemp -d) || return 1
    archive="${BACKUP_DIR}/backup_${stamp}.tar.gz"
    mkdir -p "${stage}/state" || { rm -rf "$stage"; return 1; }
    cp "${RULES_FILE}" "${stage}/state/rules.db" 2>/dev/null || true
    cp "${SETTINGS_FILE}" "${stage}/state/settings.conf" 2>/dev/null || true
    cp "${CONF_FILE}" "${stage}/forward.conf" 2>/dev/null || true
    cp "${MAIN_CONF}" "${stage}/main.conf" 2>/dev/null || true
    printf 'created_at=%s\nrule_count=%s\nsnat_mode=%s\n' "$(date '+%F %T')" "${#RULES[@]}" "$SNAT_MODE" > "${stage}/metadata"
    tar -C "$stage" -czf "$archive" . && rm -rf "$stage" || { rm -rf "$stage"; return 1; }
    printf '%s' "$archive"
}

list_backups() {
    ensure_state || return 1
    local files=("${BACKUP_DIR}"/*.tar.gz)
    if [[ ! -e "${files[0]}" ]]; then info "暂无备份。"; return 1; fi
    local i=1 file meta
    printf '\n%-4s %-28s %s\n' "序号" "备份文件" "摘要"
    for file in "${files[@]}"; do
        meta=$(tar -xOzf "$file" ./metadata 2>/dev/null | tr '\n' ' ' || true)
        printf '%-4s %-28s %s\n' "$i" "$(basename "$file")" "${meta:-无摘要}"
        ((i++))
    done
}

restore_backup() {
    ensure_state || return 1
    local files=("${BACKUP_DIR}"/*.tar.gz)
    [[ -e "${files[0]}" ]] || { info "暂无备份。"; return; }
    list_backups || return
    local choice file stage confirm
    read -rp "请输入要恢复的序号 (0 取消): " choice
    [[ "$choice" == "0" || -z "$choice" ]] && return
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#files[@]} )) || { err "无效序号。"; return; }
    file="${files[$((choice-1))]}"
    warn "恢复会覆盖当前 v2.0 规则与全局回源设置。"
    read -rp "输入 RESTORE 确认恢复: " confirm
    [[ "$confirm" == "RESTORE" ]] || { info "已取消。"; return; }
    stage=$(mktemp -d) || return
    tar -xzf "$file" -C "$stage" || { rm -rf "$stage"; err "备份文件损坏。"; return; }
    [[ -f "${stage}/state/rules.db" && -f "${stage}/state/settings.conf" ]] || { rm -rf "$stage"; err "备份格式无效。"; return; }
    cp "${stage}/state/rules.db" "$RULES_FILE" && cp "${stage}/state/settings.conf" "$SETTINGS_FILE" || { rm -rf "$stage"; err "无法恢复状态文件。"; return; }
    [[ -f "${stage}/main.conf" ]] && cp "${stage}/main.conf" "$MAIN_CONF" 2>/dev/null || true
    rm -rf "$stage"
    load_settings; load_rules
    apply_config || { err "规则已恢复，但加载 nftables 失败。"; return; }
    info "已恢复备份: $(basename "$file")"
    log_action "恢复备份 $(basename "$file")"
}

ensure_main_include() {
    local include='include "/etc/nftables.d/*.conf"'
    if [[ -f "$MAIN_CONF" ]] && grep -qF "$include" "$MAIN_CONF"; then return 0; fi
    if [[ -f "$MAIN_CONF" ]]; then
        cp "$MAIN_CONF" "${MAIN_CONF}.bak.$(date '+%Y%m%d_%H%M%S')" || return 1
        printf '\n%s\n' "$include" >> "$MAIN_CONF"
    else
        cat > "$MAIN_CONF" <<EOF
#!/usr/sbin/nft -f
${include}
EOF
    fi
}

enable_ip_forward() {
    sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || { warn "无法立即开启 IPv4 转发。"; return 1; }
    mkdir -p /etc/sysctl.d
    printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/99-nft-port-forward.conf
}

install_nftables() {
    command -v nft &>/dev/null && return 0
    local manager
    if command -v apt-get &>/dev/null; then manager="apt";
    elif command -v dnf &>/dev/null; then manager="dnf";
    elif command -v yum &>/dev/null; then manager="yum";
    elif command -v pacman &>/dev/null; then manager="pacman";
    else err "无法识别包管理器，请手动安装 nftables。"; return 1; fi
    case "$manager" in
        apt) apt-get update -y && apt-get install -y nftables ;;
        dnf) dnf install -y nftables ;;
        yum) yum install -y nftables ;;
        pacman) pacman -Sy --noconfirm nftables ;;
    esac
}

build_config() {
    local output="$1" rule id lport proto dip dport note
    if [[ "$SNAT_MODE" == "fixed" ]] && ! validate_ip "$FIXED_SNAT_IP"; then
        err "固定回源 IP 无效，请先在【全局回源模式】中设置。"
        return 1
    fi
    cat > "$output" <<EOF
#!/usr/sbin/nft -f
# 由 NFT Port Forward v2.0 自动生成，请勿手工修改。
EOF
    [[ "$SNAT_MODE" == "fixed" ]] && echo "define FORWARD_SNAT_IP = ${FIXED_SNAT_IP}" >> "$output"
    cat >> "$output" <<EOF
table ip ${TABLE_NAME} {
    chain prerouting {
        type nat hook prerouting priority -100; policy accept;
EOF
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id lport proto dip dport note <<< "$rule"
        echo "        # 规则 ${id}: $(sanitize_note "$note")" >> "$output"
        has_tcp "$proto" && echo "        tcp dport ${lport} dnat to ${dip}:${dport}" >> "$output"
        has_udp "$proto" && echo "        udp dport ${lport} dnat to ${dip}:${dport}" >> "$output"
    done
    cat >> "$output" <<EOF
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
EOF
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id lport proto dip dport note <<< "$rule"
        if has_tcp "$proto"; then
            [[ "$SNAT_MODE" == "auto" ]] && echo "        ip daddr ${dip} tcp dport ${dport} ct status dnat masquerade" >> "$output" || echo "        ip daddr ${dip} tcp dport ${dport} ct status dnat snat to \$FORWARD_SNAT_IP" >> "$output"
        fi
        if has_udp "$proto"; then
            [[ "$SNAT_MODE" == "auto" ]] && echo "        ip daddr ${dip} udp dport ${dport} ct status dnat masquerade" >> "$output" || echo "        ip daddr ${dip} udp dport ${dport} ct status dnat snat to \$FORWARD_SNAT_IP" >> "$output"
        fi
    done
    cat >> "$output" <<EOF
    }
}
EOF
}

apply_config() {
    command -v nft &>/dev/null || { err "nftables 未安装。"; return 1; }
    ensure_state || return 1
    local tmp="${CONF_FILE}.tmp.$$"
    build_config "$tmp" || { rm -f "$tmp"; return 1; }
    nft -c -f "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; err "新配置语法校验失败，现有规则未改动。"; return 1; }
    mv -f "$tmp" "$CONF_FILE" || return 1
    nft delete table ip "$TABLE_NAME" 2>/dev/null || true
    nft -f "$CONF_FILE" || { err "加载转发规则失败。"; return 1; }
}

has_iptables() { command -v iptables &>/dev/null && iptables -S &>/dev/null; }

dest_still_used() {
    local ip="$1" port="$2" proto="$3" rule id lport rproto dip dport note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id lport rproto dip dport note <<< "$rule"
        [[ "$dip" == "$ip" && "$dport" == "$port" ]] || continue
        { has_tcp "$proto" && has_tcp "$rproto"; } || { has_udp "$proto" && has_udp "$rproto"; } && return 0
    done
    return 1
}

firewall_open() {
    local lport="$1" dip="$2" dport="$3" proto="$4"
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        has_tcp "$proto" && firewall-cmd --add-port="${lport}/tcp" --permanent >/dev/null 2>&1 || true
        has_udp "$proto" && firewall-cmd --add-port="${lport}/udp" --permanent >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
    elif command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw active; then
        if has_tcp "$proto"; then ufw allow "${lport}/tcp" >/dev/null 2>&1 || true; ufw route allow proto tcp to "$dip" port "$dport" >/dev/null 2>&1 || true; fi
        if has_udp "$proto"; then ufw allow "${lport}/udp" >/dev/null 2>&1 || true; ufw route allow proto udp to "$dip" port "$dport" >/dev/null 2>&1 || true; fi
    elif has_iptables; then
        if has_tcp "$proto"; then
            iptables -C INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || true
            iptables -C FORWARD -d "$dip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || iptables -I FORWARD -d "$dip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || true
        fi
        if has_udp "$proto"; then
            iptables -C INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || true
            iptables -C FORWARD -d "$dip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || iptables -I FORWARD -d "$dip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || true
        fi
        iptables -C FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    fi
}

firewall_close() {
    local lport="$1" dip="$2" dport="$3" proto="$4"
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        has_tcp "$proto" && firewall-cmd --remove-port="${lport}/tcp" --permanent >/dev/null 2>&1 || true
        has_udp "$proto" && firewall-cmd --remove-port="${lport}/udp" --permanent >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
    elif command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw active; then
        if has_tcp "$proto"; then yes | ufw delete allow "${lport}/tcp" >/dev/null 2>&1 || true; ! dest_still_used "$dip" "$dport" tcp && yes | ufw route delete allow proto tcp to "$dip" port "$dport" >/dev/null 2>&1 || true; fi
        if has_udp "$proto"; then yes | ufw delete allow "${lport}/udp" >/dev/null 2>&1 || true; ! dest_still_used "$dip" "$dport" udp && yes | ufw route delete allow proto udp to "$dip" port "$dport" >/dev/null 2>&1 || true; fi
    elif has_iptables; then
        has_tcp "$proto" && iptables -D INPUT -p tcp --dport "$lport" -j ACCEPT 2>/dev/null || true
        has_udp "$proto" && iptables -D INPUT -p udp --dport "$lport" -j ACCEPT 2>/dev/null || true
        has_tcp "$proto" && ! dest_still_used "$dip" "$dport" tcp && iptables -D FORWARD -d "$dip" -p tcp --dport "$dport" -j ACCEPT 2>/dev/null || true
        has_udp "$proto" && ! dest_still_used "$dip" "$dport" udp && iptables -D FORWARD -d "$dip" -p udp --dport "$dport" -j ACCEPT 2>/dev/null || true
    fi
}

print_rules() {
    load_rules
    if [[ ${#RULES[@]} -eq 0 ]]; then info "当前没有转发规则。"; return 1; fi
    printf '\n%-4s %-10s %-8s %-22s %-8s %s\n' "ID" "协议" "监听" "目标" "回源" "备注"
    echo "────────────────────────────────────────────────────────────────────"
    local rule id lport proto dip dport note
    for rule in "${RULES[@]}"; do
        IFS='|' read -r id lport proto dip dport note <<< "$rule"
        printf '%-4s %-10s %-8s %-22s %-8s %s\n' "$id" "$(protocol_display "$proto")" "$lport" "${dip}:${dport}" "$(snat_mode_display "$SNAT_MODE")" "${note:--}"
    done
}

choose_protocol() {
    local choice proto
    while true; do
        echo "协议：1) TCP  2) UDP  3) TCP+UDP"
        read -rp "请选择 [1-3，默认 3]: " choice
        choice="${choice:-3}"; proto=$(normalize_protocol "$choice")
        [[ -n "$proto" ]] && { echo "$proto"; return; }
        err "无效选择。"
    done
}

ask_port() {
    local prompt="$1" default="${2:-}" value
    while true; do
        read -rp "${prompt}${default:+ [${default}]}: " value
        value="${value:-$default}"
        [[ "$value" == "0" ]] && return 1
        validate_port "$value" && { echo "$value"; return; }
        err "请输入 1-65535，输入 0 取消。"
    done
}

ask_ip() {
    local prompt="$1" default="${2:-}" value
    while true; do
        read -rp "${prompt}${default:+ [${default}]}: " value
        value="${value:-$default}"
        [[ "$value" == "0" ]] && return 1
        validate_ip "$value" && { echo "$value"; return; }
        err "请输入有效 IPv4 地址，输入 0 取消。"
    done
}

add_rule() {
    draw_header; echo "新增转发 · 输入 0 可取消"
    local lport proto dip dport note id backup
    lport=$(ask_port "监听端口") || return
    port_in_use_by_rule "$lport" && { err "该监听端口已存在规则。"; pause; return; }
    proto=$(choose_protocol)
    dip=$(ask_ip "目标 IP") || return
    dport=$(ask_port "目标端口" "$lport") || return
    read -rp "备注（可留空）: " note; note=$(sanitize_note "$note")
    echo "\n将新增：$(protocol_display "$proto") ${lport} → ${dip}:${dport}，回源：$(snat_mode_display "$SNAT_MODE")"
    [[ -n "$note" ]] && echo "备注：${note}"
    read -rp "输入 y 确认: " confirm; [[ "$confirm" == "y" || "$confirm" == "Y" ]] || return
    id=$(next_rule_id); backup=$(create_backup) || { err "创建备份失败。"; pause; return; }
    RULES+=("${id}|${lport}|${proto}|${dip}|${dport}|${note}")
    save_rules && apply_config || { err "新增失败，备份在 ${backup}"; pause; return; }
    firewall_open "$lport" "$dip" "$dport" "$proto"
    info "规则 ${id} 已添加。备份：${backup}"; log_action "新增规则 ${id}"
    pause
}

edit_rule() {
    draw_header; print_rules || { pause; return; }
    local id idx rule old_lport old_proto old_dip old_dport old_note lport proto dip dport note input backup
    read -rp "请输入要修改的规则 ID (0 取消): " id; [[ "$id" == "0" || -z "$id" ]] && return
    idx=$(find_rule_index "$id") || { err "未找到规则。"; pause; return; }
    rule="${RULES[$idx]}"; IFS='|' read -r _ old_lport old_proto old_dip old_dport old_note <<< "$rule"
    echo "留空保留当前值，端口/IP 输入 0 取消。"
    lport=$(ask_port "监听端口" "$old_lport") || return
    [[ "$lport" == "$old_lport" ]] || port_in_use_by_rule "$lport" "$id" && { err "监听端口已被其他规则使用。"; pause; return; }
    read -rp "协议 [1 TCP/2 UDP/3 TCP+UDP，当前 $(protocol_display "$old_proto")，留空保留]: " input
    proto="${old_proto}"; [[ -n "$input" ]] && proto=$(normalize_protocol "$input")
    [[ -n "$proto" ]] || { err "协议无效。"; pause; return; }
    dip=$(ask_ip "目标 IP" "$old_dip") || return
    dport=$(ask_port "目标端口" "$old_dport") || return
    read -rp "备注 [${old_note:--}；留空保留，- 清空]: " input
    if [[ "$input" == "-" ]]; then note=""; elif [[ -z "$input" ]]; then note="$old_note"; else note=$(sanitize_note "$input"); fi
    echo "\n将修改为：$(protocol_display "$proto") ${lport} → ${dip}:${dport}"
    read -rp "输入 y 确认: " confirm; [[ "$confirm" == "y" || "$confirm" == "Y" ]] || return
    backup=$(create_backup) || { err "创建备份失败。"; pause; return; }
    RULES[$idx]="${id}|${lport}|${proto}|${dip}|${dport}|${note}"
    save_rules && apply_config || { err "修改失败，备份在 ${backup}"; pause; return; }
    firewall_close "$old_lport" "$old_dip" "$old_dport" "$old_proto"
    firewall_open "$lport" "$dip" "$dport" "$proto"
    info "规则 ${id} 已修改。备份：${backup}"; log_action "修改规则 ${id}"
    pause
}

delete_rule() {
    draw_header; print_rules || { pause; return; }
    local id idx rule lport proto dip dport note backup confirm
    read -rp "请输入要删除的规则 ID (0 取消): " id; [[ "$id" == "0" || -z "$id" ]] && return
    idx=$(find_rule_index "$id") || { err "未找到规则。"; pause; return; }
    rule="${RULES[$idx]}"; IFS='|' read -r _ lport proto dip dport note <<< "$rule"
    warn "将删除：$(protocol_display "$proto") ${lport} → ${dip}:${dport}"
    read -rp "输入 DELETE 确认: " confirm; [[ "$confirm" == "DELETE" ]] || return
    backup=$(create_backup) || { err "创建备份失败。"; pause; return; }
    unset 'RULES[idx]'; RULES=("${RULES[@]}")
    save_rules && apply_config || { err "删除失败，备份在 ${backup}"; pause; return; }
    firewall_close "$lport" "$dip" "$dport" "$proto"
    info "规则已删除。备份：${backup}"; log_action "删除规则 ${id}"
    pause
}

snat_menu() {
    while true; do
        draw_header
        echo "全局回源模式：$(snat_mode_display "$SNAT_MODE")"
        [[ "$SNAT_MODE" == "fixed" ]] && echo "固定 SNAT IP：${FIXED_SNAT_IP:-未设置}"
        echo "\n  1) 固定 SNAT IP"
        echo "  2) 自动 MASQUERADE"
        echo "  0) 返回"
        read -rp "请选择: " choice
        case "$choice" in
            1)
                local ip backup
                ip=$(ask_ip "固定回源 IP" "${FIXED_SNAT_IP:-$(get_local_ip)}") || continue
                backup=$(create_backup) || { err "创建备份失败。"; pause; continue; }
                SNAT_MODE="fixed"; FIXED_SNAT_IP="$ip"; save_settings && apply_config || { err "切换失败，备份在 ${backup}"; pause; continue; }
                info "已切换为固定 SNAT：${ip}"; pause
                ;;
            2)
                read -rp "自动模式会按实际出口选源地址，输入 y 确认: " confirm
                [[ "$confirm" == "y" || "$confirm" == "Y" ]] || continue
                local backup
                backup=$(create_backup) || { err "创建备份失败。"; pause; continue; }
                SNAT_MODE="auto"; save_settings && apply_config || { err "切换失败，备份在 ${backup}"; pause; continue; }
                info "已切换为自动 MASQUERADE。"; pause
                ;;
            0) return ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

show_service_status() {
    draw_header; echo "服务与持久化状态\n"
    if command -v systemctl &>/dev/null; then
        info "开机自启：$(systemctl is-enabled nftables 2>/dev/null || echo 未启用)"
        info "服务状态：$(systemctl is-active nftables 2>/dev/null || echo 未运行)"
    else warn "未检测到 systemctl。"; fi
    [[ -f "$MAIN_CONF" ]] && grep -qF 'include "/etc/nftables.d/*.conf"' "$MAIN_CONF" && info "主配置 include：已配置" || warn "主配置 include：缺失"
    nft list table ip "$TABLE_NAME" &>/dev/null && info "运行中转发表：已加载" || warn "运行中转发表：未加载"
    echo "回源模式：$(snat_mode_display "$SNAT_MODE") ${FIXED_SNAT_IP:+(${FIXED_SNAT_IP})}"
    pause
}

service_menu() {
    while true; do
        draw_header
        echo "服务与持久化管理\n"
        echo "  1) 查看状态"
        echo "  2) 启用并立即启动 nftables"
        echo "  3) 重载本脚本规则"
        echo "  4) 修复主配置 include（不清空规则）"
        echo "  0) 返回"
        read -rp "请选择: " choice
        case "$choice" in
            1) show_service_status ;;
            2) systemctl enable --now nftables && info "服务已启用。" || err "服务启用失败。"; pause ;;
            3) apply_config && info "规则已重载。"; pause ;;
            4) ensure_main_include && info "主配置已修复。" || err "修复失败。"; pause ;;
            0) return ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

test_rule() {
    local rule="$1" id lport proto dip dport note
    IFS='|' read -r id lport proto dip dport note <<< "$rule"
    has_tcp "$proto" && { printf '规则 %s TCP %s:%s ... ' "$id" "$dip" "$dport"; timeout 3 bash -c ">/dev/tcp/${dip}/${dport}" 2>/dev/null && echo "通" || echo "不通或超时"; }
    has_udp "$proto" && printf '规则 %s UDP %s:%s ... 已发送探测包\n' "$id" "$dip" "$dport"
}

diagnostic_menu() {
    while true; do
        draw_header
        echo "诊断与测试\n"
        echo "  1) 基础状态检查"
        echo "  2) 测试指定规则"
        echo "  3) 测试全部规则"
        echo "  4) 查看当前生成的 DNAT/SNAT"
        echo "  0) 返回"
        read -rp "请选择: " choice
        case "$choice" in
            1) show_service_status ;;
            2) print_rules || { pause; continue; }; read -rp "规则 ID: " id; idx=$(find_rule_index "$id") && test_rule "${RULES[$idx]}" || err "未找到规则。"; pause ;;
            3) load_rules; for rule in "${RULES[@]}"; do test_rule "$rule"; done; pause ;;
            4) [[ -f "$CONF_FILE" ]] && sed -n '/chain prerouting/,/}/p;/chain postrouting/,/}/p' "$CONF_FILE" || err "配置不存在。"; pause ;;
            0) return ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

backup_menu() {
    while true; do
        draw_header
        echo "备份与恢复\n"
        echo "  1) 查看备份"
        echo "  2) 恢复备份"
        echo "  3) 立即创建备份"
        echo "  0) 返回"
        read -rp "请选择: " choice
        case "$choice" in
            1) list_backups; pause ;;
            2) restore_backup; pause ;;
            3) b=$(create_backup) && info "已创建备份：${b}" || err "备份失败。"; pause ;;
            0) return ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

safe_initialize() {
    draw_header
    echo "安全初始化不会清空其他 nftables 表。"
    read -rp "输入 y 确认: " confirm; [[ "$confirm" == "y" || "$confirm" == "Y" ]] || return
    install_nftables || { pause; return; }
    ensure_state && ensure_main_include && enable_ip_forward && apply_config || { err "初始化失败。"; pause; return; }
    systemctl enable --now nftables 2>/dev/null || warn "请手动启用 nftables 服务。"
    info "安全初始化完成。"; log_action "安全初始化"; pause
}

dangerous_takeover() {
    draw_header
    warn "危险操作会执行 nft flush ruleset，清空机器当前全部 nftables 规则。"
    read -rp "输入 TAKEOVER 确认: " confirm; [[ "$confirm" == "TAKEOVER" ]] || return
    create_backup >/dev/null || { err "创建备份失败。"; pause; return; }
    nft flush ruleset || { err "清空规则失败。"; pause; return; }
    cat > "$MAIN_CONF" <<'EOF'
#!/usr/sbin/nft -f
include "/etc/nftables.d/*.conf"
EOF
    ensure_state && enable_ip_forward && apply_config || { err "接管失败。"; pause; return; }
    systemctl enable --now nftables 2>/dev/null || true
    info "已完成接管。"; log_action "危险接管 nftables"; pause
}

initialization_menu() {
    while true; do
        draw_header
        echo "安装与配置\n"
        echo "  1) 安装依赖并安全初始化"
        echo "  2) 清空并接管 nftables 规则"
        echo "  0) 返回"
        read -rp "请选择: " choice
        case "$choice" in
            1) safe_initialize ;;
            2) dangerous_takeover ;;
            0) return ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

main_menu() {
    ensure_state || { err "无法初始化状态目录。"; exit 1; }
    while true; do
        draw_header
        echo "\n  转发规则"
        echo "  [1] 查看规则        [2] 新增规则"
        echo "  [3] 修改规则        [4] 删除规则"
        echo "\n  网络与服务"
        echo "  [5] 全局回源模式    [6] 服务与开机自启"
        echo "\n  维护工具"
        echo "  [7] 诊断与测试      [8] 备份与恢复"
        echo "  [9] 安装与配置"
        echo "\n  [0] 退出"
        read -rp "请选择操作: " choice
        case "$choice" in
            1) draw_header; print_rules; pause ;;
            2) add_rule ;;
            3) edit_rule ;;
            4) delete_rule ;;
            5) snat_menu ;;
            6) service_menu ;;
            7) diagnostic_menu ;;
            8) backup_menu ;;
            9) initialization_menu ;;
            0) info "再见！"; exit 0 ;;
            *) err "无效选择。"; pause ;;
        esac
    done
}

check_root
main_menu

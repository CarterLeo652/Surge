#!/usr/bin/env bash
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin:~/bin
export PATH

#=================================================
#   System  : CentOS 6/7, Debian 8+, Ubuntu 16+
#   Desc    : BBR 配置管理与按机器/线路自适应 TCP 调优
#   Version : 2.1.0
#   Scope   : BBR 作用于本机发送的 TCP；纯 nft 转发连接由两端控制拥塞。
#=================================================

# ────────────────────────── 颜色定义 ──────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;36m'
CYAN='\033[0;96m'
BOLD='\033[1m'
PLAIN='\033[0m'

sh_ver="2.1.0"

# ────────────────────────── 通用工具函数 ──────────────────────────
info()    { echo -e "${BLUE}[INFO]${PLAIN}  $*"; }
ok()      { echo -e "${GREEN}[  OK]${PLAIN}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${PLAIN}  $*"; }
error()   { echo -e "${RED}[FAIL]${PLAIN}  $*"; }
die()     { error "$*"; exit 1; }

hr() {
    echo -e "${BLUE}──────────────────────────────────────────────────────${PLAIN}"
}

banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║                                                      ║"
    printf '  ║     BBR 配置管理               v%s%*s║\n' "$sh_ver" "$((19 - ${#sh_ver}))" ''
    echo "  ║     适用于 CentOS 6/7 · Debian 8+ · Ubuntu 16+       ║"
    echo "  ║                                                      ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${PLAIN}"
}

# ────────────────────────── Root 权限检查 ──────────────────────────
[[ $EUID -ne 0 ]] && die "请使用 root 用户运行此脚本"

# ────────────────────────── 系统信息检测 ──────────────────────────
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
    VER=$VERSION_ID
elif [[ -f /etc/redhat-release ]]; then
    OS="centos"
    VER=$(grep -oE '[0-9]+' /etc/redhat-release | head -1)
else
    die "不支持的操作系统"
fi

ARCH=$(uname -m)
[[ $ARCH == "x86_64" ]] && ARCH_NAME="amd64" || ARCH_NAME=$ARCH

# ────────────────────────── /boot 空间检查 ──────────────────────────
check_boot_space() {
    hr
    info "检查 /boot 分区空间..."
    local boot_available
    boot_available=$(df -m /boot 2>/dev/null | tail -1 | awk '{print $4}')

    if [[ -n "$boot_available" ]]; then
        if [[ $boot_available -lt 100 ]]; then
            warn "/boot 空间不足（可用: ${boot_available}MB），建议清理旧内核后再继续"
            read -rp "  是否继续? [y/N]: " continue_boot
            [[ ! "$continue_boot" =~ ^[Yy]$ ]] && exit 1
        else
            ok "/boot 空间充足（可用: ${boot_available}MB）"
        fi
    fi
}

# ────────────────────────── 系统默认拥塞控制检测 ──────────────────────────
# 此处判断新建 TCP 连接的默认算法，不代表每一条现有连接都在使用 BBR。
check_bbr_status() {
    local cc
    cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
    [[ "$cc" == "bbr" ]]
}

# ────────────────────────── 检查当前内核是否实际提供 BBR ──────────────────────────
check_bbr_available() {
    local available
    available=$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null) || available=""
    if [[ "$available" =~ (^|[[:space:]])bbr($|[[:space:]]) ]]; then
        ok "当前内核提供 BBR"
        return 0
    fi
    error "当前内核或容器未提供 BBR（可用算法: ${available:-未知}）"
    return 1
}

# ────────────────────────── sysctl 兼容写入（兼容 LXC/容器）──────────────────────────
# 优先用 sysctl -w 逐条写入，避免 sysctl -p 在容器内遇到不支持的参数时中途退出
sysctl_apply_file() {
    local conf_file="$1"
    local skipped=0 actual

    while IFS= read -r line; do
        # 跳过注释行和空行
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue

        # 提取 key=value（key 去除所有空格，value 仅去除首尾空白，保留中间空格）
        local key value
        key=$(echo "$line" | cut -d= -f1 | tr -d ' ')
        value=$(echo "$line" | cut -d= -f2- | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
        [[ -z "$key" || -z "$value" ]] && continue

        # 尝试写入，捕获错误
        local err
        err=$(sysctl -w "${key}=${value}" 2>&1)
        if [[ $? -ne 0 ]]; then
            # 某些内核或容器不允许写入该参数，继续处理其余配置。
            warn "参数写入失败: ${key} (${err})"
            (( skipped++ ))
        else
            actual=$(sysctl -n "$key" 2>/dev/null) || actual=""
            if [[ "${actual//[[:space:]]/}" != "${value//[[:space:]]/}" ]]; then
                warn "参数写入后未达到目标值: ${key}（实际: ${actual:-未知}）"
                (( skipped++ ))
            fi
        fi
    done < "$conf_file"

    [[ $skipped -eq 0 ]]
}

cleanup_temp_files() {
    local file
    for file in "$@"; do
        [[ -n "$file" ]] && rm -f -- "$file"
    done
}

# 只在当前运行值确实来自本脚本配置时，允许按新线路测量值调低旧上限。
managed_setting_matches() {
    local key="$1" current="$2" file configured
    for file in /etc/sysctl.d/99-tcp-tune.conf /etc/sysctl.d/99-bbr.conf; do
        [[ "$file" == *99-bbr.conf ]] && {
            is_managed_conf "$file" bbr || continue
        }
        [[ "$file" == *99-tcp-tune.conf ]] && {
            is_managed_conf "$file" tcp || continue
        }
        configured=$(awk -F= -v wanted="$key" '
            /^[[:space:]]*#/ {next}
            NF >= 2 {
                name=$1; gsub(/[[:space:]]/, "", name)
                if (name == wanted) {
                    value=$2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                    print value
                }
            }
        ' "$file" | tail -n 1)
        [[ -n "$configured" && "${configured//[[:space:]]/}" == "${current//[[:space:]]/}" ]] && return 0
    done
    return 1
}

# ────────────────────────── 优化配置归属与备份 ──────────────────────────
is_managed_conf() {
    local conf_file="$1" kind="$2"
    [[ -f "$conf_file" && ! -L "$conf_file" ]] || return 1

    if grep -Fxq '# bbr-tcp-clean: managed configuration' "$conf_file"; then
        return 0
    fi

    # 兼容旧版脚本生成的配置文件。
    case "$kind" in
        bbr)
            grep -Fxq '# ── BBR 拥塞控制 ──' "$conf_file" &&
            grep -Eq '^[[:space:]]*net\.ipv4\.tcp_congestion_control[[:space:]]*=[[:space:]]*bbr[[:space:]]*$' "$conf_file"
            ;;
        tcp)
            grep -Fxq '# ── TIME_WAIT ──────────────────────────────' "$conf_file" &&
            grep -Eq '^[[:space:]]*net\.ipv4\.tcp_tw_reuse[[:space:]]*=[[:space:]]*1[[:space:]]*$' "$conf_file"
            ;;
        *) return 1 ;;
    esac
}

prepare_managed_conf() {
    local conf_file="$1" kind="$2" backup="${1}.bbr-tcp-original"
    [[ -e "$conf_file" || -L "$conf_file" ]] || return 0
    [[ ! -L "$conf_file" ]] || { error "配置文件是符号链接，拒绝覆盖: $conf_file"; return 1; }
    is_managed_conf "$conf_file" "$kind" && return 0
    [[ ! -e "$backup" ]] || { error "备份文件已存在，拒绝覆盖: $backup"; return 1; }
    cp -p -- "$conf_file" "$backup" || { error "备份失败: $conf_file"; return 1; }
    ok "已备份原有配置: $backup"
}

remove_managed_conf() {
    local conf_file="$1" kind="$2" backup="${1}.bbr-tcp-original"
    if ! is_managed_conf "$conf_file" "$kind"; then
        warn "文件内容与本脚本生成的配置不匹配，已保留: $conf_file"
        return 1
    fi

    # 保留脚本生成文件的原样副本，便于核对或手工恢复；扩展名不是 .conf，不会被 sysctl 加载。
    local removed_archive
    removed_archive=$(mktemp "${conf_file}.removed.XXXXXX") || return 1
    cp -p -- "$conf_file" "$removed_archive" || { rm -f -- "$removed_archive"; error "无法归档配置: $conf_file"; return 1; }

    if [[ -e "$backup" ]]; then
        [[ -f "$backup" && ! -L "$backup" ]] || { error "原配置备份异常: $backup"; return 1; }
        mv -f -- "$backup" "$conf_file" || { error "恢复原配置失败: $conf_file"; return 1; }
        ok "已恢复原有配置: ${conf_file}（脚本配置归档: ${removed_archive}）"
    else
        rm -f -- "$conf_file" || { error "删除失败: $conf_file"; return 1; }
        ok "已删除: ${conf_file}（归档: ${removed_archive}）"
    fi
}

remove_limits_block() {
    local limits_file=/etc/security/limits.conf tmp_file
    tmp_file=$(mktemp /etc/security/.limits.conf.XXXXXX) || { error "无法创建临时文件"; return 1; }

    # 只删除本脚本添加的完整五行，保留用户自己的 nofile 设置。
    if ! awk '
        { lines[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) {
                if (lines[i] == "# tcp-tune: 提升文件描述符限制" &&
                    lines[i+1] == "* soft nofile 1000000" &&
                    lines[i+2] == "* hard nofile 1000000" &&
                    lines[i+3] == "root soft nofile 1000000" &&
                    lines[i+4] == "root hard nofile 1000000") {
                    i += 4
                    continue
                }
                print lines[i]
            }
        }
    ' "$limits_file" > "$tmp_file"; then
        rm -f -- "$tmp_file"
        error "读取 limits.conf 失败"
        return 1
    fi

    if cmp -s "$limits_file" "$tmp_file"; then
        rm -f -- "$tmp_file"
        warn "limits.conf 中的优化条目已变动，未自动删除"
        return 1
    fi

    if ! cat "$tmp_file" > "$limits_file"; then
        rm -f -- "$tmp_file"
        error "写入 limits.conf 失败"
        return 1
    fi
    rm -f -- "$tmp_file"
    ok "已删除 limits.conf 中的文件描述符优化条目"
}

# ────────────────────────── 一键卸载优化配置 ──────────────────────────
uninstall_optimizations() {
    hr
    echo "  将移除本脚本写入的 BBR、TCP 和文件描述符优化配置。"
    echo "  内核升级、软件源更改和已安装软件不会自动回退。"
    echo ""
    read -rp "  确认卸载? [y/N]: " confirm_uninstall
    [[ "$confirm_uninstall" =~ ^[Yy]$ ]] || { warn "已取消"; return 0; }

    local removed=0 failed=0 conf_file kind
    for kind in bbr tcp; do
        if [[ "$kind" == bbr ]]; then
            conf_file=/etc/sysctl.d/99-bbr.conf
        else
            conf_file=/etc/sysctl.d/99-tcp-tune.conf
        fi
        if [[ -e "$conf_file" || -L "$conf_file" ]]; then
            if remove_managed_conf "$conf_file" "$kind"; then
                ((removed+=1))
            else
                failed=1
            fi
        fi
    done

    if [[ -f /etc/security/limits.conf ]] &&
       grep -Fxq '# tcp-tune: 提升文件描述符限制' /etc/security/limits.conf; then
        if remove_limits_block; then
            ((removed+=1))
        else
            failed=1
        fi
    fi

    if [[ $failed -ne 0 ]]; then
        error "部分配置未能卸载，请检查上方提示"
        return 1
    fi
    if [[ $removed -eq 0 ]]; then
        ok "未发现本脚本写入的优化配置"
        return 0
    fi

    ok "优化配置已卸载（共处理 ${removed} 处）"
    warn "运行中的内核参数需重启后才会按系统及其他配置恢复"
    read -rp "  是否现在重启? [y/N]: " reboot_now
    [[ "$reboot_now" =~ ^[Yy]$ ]] && reboot
    return 0
}

# ────────────────────────── 仅启用 BBR / fq ──────────────────────────
enable_bbr() {
    hr
    modprobe tcp_bbr 2>/dev/null || true
    check_bbr_available || return 1

    local conf=/etc/sysctl.d/99-bbr.conf
    local previous_file="" tmp_file="" old_cc old_qdisc current_cc current_qdisc
    local legacy_archive=""
    old_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) || old_cc=""
    old_qdisc=$(sysctl -n net.core.default_qdisc 2>/dev/null) || old_qdisc=""

    # 老版本的同名文件还包含全局缓冲区和连接队列设置，先归档再收敛为两项。
    if is_managed_conf "$conf" bbr && grep -q '^net.core.rmem_max' "$conf"; then
        legacy_archive=$(mktemp "${conf}.legacy.XXXXXX") || return 1
        cp -p -- "$conf" "$legacy_archive" || { rm -f -- "$legacy_archive"; error "无法归档旧版 BBR 配置"; return 1; }
        ok "旧版完整配置已归档: $legacy_archive"
    fi
    prepare_managed_conf "$conf" bbr || return 1

    if [[ -f "$conf" ]]; then
        previous_file=$(mktemp /etc/sysctl.d/.99-bbr-previous.XXXXXX) || return 1
        cp -p -- "$conf" "$previous_file" || { rm -f -- "$previous_file"; return 1; }
    fi
    tmp_file=$(mktemp /etc/sysctl.d/.99-bbr-new.XXXXXX) || {
        [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
        return 1
    }
    if ! cat > "$tmp_file" <<'EOF'
# bbr-tcp-clean: managed configuration
# 仅设置本机新建 TCP 连接的拥塞控制和默认队列。
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
    then
        rm -f -- "$tmp_file"
        [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
        error "写入 BBR 配置失败"
        return 1
    fi
    chmod 644 "$tmp_file" || { rm -f -- "$tmp_file"; return 1; }
    if ! mv -f -- "$tmp_file" "$conf"; then
        rm -f -- "$tmp_file"
        [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
        error "保存 BBR 配置失败"
        return 1
    fi

    if sysctl_apply_file "$conf"; then
        current_cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)
        current_qdisc=$(sysctl -n net.core.default_qdisc 2>/dev/null)
    else
        current_cc=""
        current_qdisc=""
    fi
    if [[ "$current_cc" != bbr || "$current_qdisc" != fq ]]; then
        if [[ -n "$previous_file" ]]; then
            mv -f -- "$previous_file" "$conf"
        else
            rm -f -- "$conf"
        fi
        [[ -n "$old_qdisc" ]] && sysctl -w "net.core.default_qdisc=$old_qdisc" >/dev/null 2>&1 || true
        [[ -n "$old_cc" ]] && sysctl -w "net.ipv4.tcp_congestion_control=$old_cc" >/dev/null 2>&1 || true
        error "BBR/fq 未完整生效，已尝试恢复原配置和运行值"
        return 1
    fi
    [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
    ok "BBR + fq 已启用；只写入两项内核参数"
    info "BBR 作用于本机 TCP 发送；纯 nft 转发流量不会因此改变拥塞控制。"
    if [[ -n "$legacy_archive" || -f /etc/sysctl.d/99-tcp-tune.conf ]]; then
        warn "检测到旧版调优：已运行的其他参数不会因重写 BBR 文件立即恢复。"
        warn "请用菜单 2 检查旧配置，并在合适时间重启以恢复未单独配置的参数。"
    fi
    info "default_qdisc 是默认值；当前网卡队列请用 tc qdisc show 检查。"
}

# ────────────────────────── 根据带宽、RTT 和内存计算 TCP 缓冲区 ──────────────────────────
# BDP = Mbps × 125 × RTT(ms) 字节；给双向在途数据与协议开销留余量。
# 建议值受物理内存 1/32 和 64 MiB 双重限制；保留非本脚本的更高上限。
calc_buffer_target() {
    local bw="$1" rtt="$2" ram_mib="$3"
    awk -v bw="$bw" -v rtt="$rtt" -v ram="$ram_mib" 'BEGIN {
        target = bw * 125 * rtt * 2 + 2097152
        cap = ram * 32768
        if (cap > 67108864) cap = 67108864
        if (cap < 1048576) cap = 1048576
        if (target < 4194304) target = 4194304
        if (target > cap) target = cap
        printf "%.0f", target
    }'
}

buffer_ram_cap() {
    local ram_mib="$1"
    awk -v ram="$ram_mib" 'BEGIN {
        cap = ram * 32768
        if (cap > 67108864) cap = 67108864
        if (cap < 1048576) cap = 1048576
        printf "%.0f", cap
    }'
}

# 连接追踪只在实时占用超过 75% 时建议调整，不凭内存档位直接写死 100 万。
conntrack_proposal() {
    local count="$1" current="$2" ram_mib="$3" cap proposal
    [[ "$count" =~ ^[0-9]+$ && "$current" =~ ^[0-9]+$ && "$ram_mib" =~ ^[0-9]+$ ]] || return 1
    (( current > 0 && count * 4 >= current * 3 )) || return 1
    cap=$(( ram_mib * 64 ))
    (( cap > 262144 )) && cap=262144
    proposal=$(( current * 2 ))
    (( proposal > cap )) && proposal=$cap
    (( proposal > current )) || return 1
    printf '%s' "$proposal"
}

# ────────────────────────── 自适应 TCP 调优 ──────────────────────────
adaptive_tcp_tune() {
    local conf=/etc/sysctl.d/99-tcp-tune.conf
    local ram_kib ram_mib ip_forward role_choice role_name bw rtt input
    local current_rmax current_wmax current_rrange current_wrange
    local rmin rmid rmax wmin wmid wmax target cap new_rmax new_wmax
    local ct_count ct_max ct_target="" existing_ct key value old_value
    local previous_file="" runtime_backup="" tmp_file="" legacy_archive=""
    local apply_failed=0

    ram_kib=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
    [[ "$ram_kib" =~ ^[0-9]+$ ]] || { error "无法读取物理内存"; return 1; }
    ram_mib=$(( ram_kib / 1024 ))
    ip_forward=$(sysctl -n net.ipv4.ip_forward 2>/dev/null) || ip_forward=0
    ct_count=$(sysctl -n net.netfilter.nf_conntrack_count 2>/dev/null) || ct_count=""
    ct_max=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null) || ct_max=""

    hr
    info "物理内存 ${ram_mib} MiB；IPv4 转发 ${ip_forward}；连接追踪 ${ct_count:-未知}/${ct_max:-未知}"
    if is_managed_conf /etc/sysctl.d/99-bbr.conf bbr &&
       grep -q '^net.core.rmem_max' /etc/sysctl.d/99-bbr.conf; then
        warn "旧版 BBR 配置仍含固定 TCP 参数，建议先运行菜单 1 收敛该文件。"
    fi
    echo "  请选择机器用途：1) 本机代理  2) 转发 + 本机代理  3) 纯转发"
    if [[ "$ip_forward" == 1 ]]; then input=2; else input=1; fi
    read -rp "  用途 [默认 ${input}]: " role_choice
    role_choice=${role_choice:-$input}
    case "$role_choice" in
        1) role_name=proxy ;;
        2) role_name=mixed ;;
        3) role_name=forward ;;
        *) error "用途只能选 1、2 或 3"; return 1 ;;
    esac

    # 纯转发不终止 TCP 连接，不用 BDP 公式改本机 socket 缓冲区。
    if [[ "$role_name" != forward ]]; then
        echo "  请填实际线路能力。虚拟网卡显示的速率通常不是套餐带宽。"
        read -rp "  本机代理线路带宽 Mbps（回车取消）: " bw
        [[ "$bw" =~ ^[0-9]+$ ]] && (( bw >= 1 && bw <= 100000 )) || {
            info "未提供有效带宽，未修改任何参数"
            return 0
        }
        read -rp "  该线路典型 RTT 毫秒（1-2000，回车取消）: " rtt
        [[ "$rtt" =~ ^[0-9]+$ ]] && (( rtt >= 1 && rtt <= 2000 )) || {
            info "未提供有效 RTT，未修改任何参数"
            return 0
        }
        current_rmax=$(sysctl -n net.core.rmem_max 2>/dev/null)
        current_wmax=$(sysctl -n net.core.wmem_max 2>/dev/null)
        current_rrange=$(sysctl -n net.ipv4.tcp_rmem 2>/dev/null)
        current_wrange=$(sysctl -n net.ipv4.tcp_wmem 2>/dev/null)
        read -r rmin rmid rmax <<< "$current_rrange"
        read -r wmin wmid wmax <<< "$current_wrange"
        for value in "$current_rmax" "$current_wmax" "$rmin" "$rmid" "$rmax" "$wmin" "$wmid" "$wmax"; do
            [[ "$value" =~ ^[0-9]+$ ]] || { error "现有 TCP 缓冲区参数无法解析"; return 1; }
        done
        target=$(calc_buffer_target "$bw" "$rtt" "$ram_mib")
        cap=$(buffer_ram_cap "$ram_mib")
        new_rmax=$target
        new_wmax=$target
        if ! managed_setting_matches net.core.rmem_max "$current_rmax"; then
            (( current_rmax > new_rmax )) && new_rmax=$current_rmax
        fi
        if ! managed_setting_matches net.core.wmem_max "$current_wmax"; then
            (( current_wmax > new_wmax )) && new_wmax=$current_wmax
        fi
        if ! managed_setting_matches net.ipv4.tcp_rmem "$current_rrange"; then
            (( rmax > new_rmax )) && new_rmax=$rmax
        fi
        if ! managed_setting_matches net.ipv4.tcp_wmem "$current_wrange"; then
            (( wmax > new_wmax )) && new_wmax=$wmax
        fi
        echo ""
        info "按 ${bw} Mbps × ${rtt} ms 估算 BDP；建议不超过内存 1/32（上限 64 MiB），保留非本脚本的更高值"
        printf '  接收上限: %s → %s 字节；发送上限: %s → %s 字节（建议上限 %s 字节）\n' \
            "$current_rmax" "$new_rmax" "$current_wmax" "$new_wmax" "$cap"
    fi

    if [[ ! "$ct_count" =~ ^[0-9]+$ || ! "$ct_max" =~ ^[0-9]+$ ]]; then
        warn "无法读取连接追踪占用，跳过该项"
    elif ct_target=$(conntrack_proposal "$ct_count" "$ct_max" "$ram_mib"); then
        warn "连接追踪已用 ${ct_count}/${ct_max}；按内存上限可考虑提高到 ${ct_target}"
        read -rp "  同时调整连接追踪上限？[y/N]: " input
        [[ "$input" =~ ^[Yy]$ ]] || ct_target=""
    else
        info "连接追踪未达到可安全建议的扩容条件，保持现有上限"
    fi
    if [[ "$role_name" != forward && -z "$ct_target" && -f "$conf" ]] &&
       grep -q '^# adaptive-tcp-tune:' "$conf"; then
        existing_ct=$(awk -F= '$1 ~ /^[[:space:]]*net.netfilter.nf_conntrack_max[[:space:]]*$/ {
            value=$2; gsub(/[[:space:]]/, "", value); print value
        }' "$conf" | tail -n 1)
        if [[ "$existing_ct" =~ ^[0-9]+$ && "$existing_ct" == "$ct_max" ]]; then
            ct_target=$existing_ct
            info "保留现有自适应配置中的连接追踪上限 ${ct_target}"
        fi
    fi
    if [[ "$role_name" == forward && -z "$ct_target" ]]; then
        info "纯转发不调整 TCP socket 参数，当前也不需要扩大连接追踪表"
        if is_managed_conf "$conf" tcp; then
            warn "现有本脚本 TCP 调优配置仍会在重启时加载，可按提示清理。"
            remove_tcp_tune || return 1
        fi
        return 0
    fi
    warn "不会改 tcp_mem、TIME_WAIT、keepalive、SYN 重试、文件句柄等缺乏负载依据的参数。"
    if [[ -f "$conf" ]] && grep -q '^net.ipv4.tcp_mem' "$conf"; then
        warn "检测到旧版固定配置；替换后旧运行值仍会保留到重启。"
    fi
    read -rp "  确认应用以上参数？[y/N]: " input
    [[ "$input" =~ ^[Yy]$ ]] || { info "已取消"; return 0; }

    # 保留旧版脚本生成的文件，另存为非 .conf 扩展名以免再次加载。
    if is_managed_conf "$conf" tcp; then
        legacy_archive=$(mktemp "${conf}.before-adaptive.XXXXXX") || return 1
        cp -p -- "$conf" "$legacy_archive" || { rm -f -- "$legacy_archive"; error "无法归档现有调优配置"; return 1; }
    fi
    prepare_managed_conf "$conf" tcp || return 1
    if [[ -f "$conf" ]]; then
        previous_file=$(mktemp /etc/sysctl.d/.99-tcp-previous.XXXXXX) || return 1
        cp -p -- "$conf" "$previous_file" || { rm -f -- "$previous_file"; return 1; }
    fi
    runtime_backup=$(mktemp /etc/sysctl.d/.99-tcp-runtime.XXXXXX) || {
        [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
        return 1
    }
    if [[ "$role_name" != forward ]]; then
        for key in net.core.rmem_max net.core.wmem_max net.ipv4.tcp_rmem net.ipv4.tcp_wmem; do
            old_value=$(sysctl -n "$key" 2>/dev/null) || { error "无法读取 $key"; cleanup_temp_files "$runtime_backup" "$previous_file"; return 1; }
            printf '%s=%s\n' "$key" "$old_value" >> "$runtime_backup" || { cleanup_temp_files "$runtime_backup" "$previous_file"; return 1; }
        done
    fi
    if [[ -n "$ct_target" ]]; then
        old_value=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null) || {
            error "无法读取连接追踪上限"; cleanup_temp_files "$runtime_backup" "$previous_file"; return 1;
        }
        printf 'net.netfilter.nf_conntrack_max=%s\n' "$old_value" >> "$runtime_backup" || { cleanup_temp_files "$runtime_backup" "$previous_file"; return 1; }
    fi
    tmp_file=$(mktemp /etc/sysctl.d/.99-tcp-new.XXXXXX) || {
        rm -f -- "$runtime_backup"
        [[ -n "$previous_file" ]] && rm -f -- "$previous_file"
        return 1
    }
    {
        printf '# bbr-tcp-clean: managed configuration\n'
        printf '# adaptive-tcp-tune: role=%s ram=%sMiB bw=%sMbps rtt=%sms\n' \
            "$role_name" "$ram_mib" "${bw:-n/a}" "${rtt:-n/a}"
        if [[ "$role_name" != forward ]]; then
            printf 'net.core.rmem_max = %s\n' "$new_rmax"
            printf 'net.core.wmem_max = %s\n' "$new_wmax"
            printf 'net.ipv4.tcp_rmem = %s %s %s\n' "$rmin" "$rmid" "$new_rmax"
            printf 'net.ipv4.tcp_wmem = %s %s %s\n' "$wmin" "$wmid" "$new_wmax"
        fi
        if [[ -n "$ct_target" ]]; then
            printf 'net.netfilter.nf_conntrack_max = %s\n' "$ct_target"
        fi
    } > "$tmp_file" || { error "生成调优配置失败"; cleanup_temp_files "$tmp_file" "$runtime_backup" "$previous_file"; return 1; }
    chmod 644 "$tmp_file" || { cleanup_temp_files "$tmp_file" "$runtime_backup" "$previous_file"; return 1; }
    mv -f -- "$tmp_file" "$conf" || { error "保存调优配置失败"; cleanup_temp_files "$tmp_file" "$runtime_backup" "$previous_file"; return 1; }

    sysctl_apply_file "$conf" || apply_failed=1
    if (( apply_failed )); then
        if [[ -n "$previous_file" ]]; then
            mv -f -- "$previous_file" "$conf" || warn "原配置恢复失败，备份仍在: $previous_file"
        else
            rm -f -- "$conf" || warn "无法删除未生效的配置: $conf"
        fi
        while IFS='=' read -r key value; do
            [[ -n "$key" ]] && sysctl -w "${key}=${value}" >/dev/null 2>&1 || true
        done < "$runtime_backup"
        cleanup_temp_files "$runtime_backup"
        error "参数未完整应用，已尝试恢复原配置及运行值"
        return 1
    fi
    cleanup_temp_files "$runtime_backup" "$previous_file"
    ok "自适应 TCP 调优已应用: $conf"
    if [[ -f /etc/security/limits.conf ]] &&
       grep -Fxq '# tcp-tune: 提升文件描述符限制' /etc/security/limits.conf; then
        remove_limits_block || warn "旧版 nofile 配置未能自动移除"
    fi
    if [[ -n "$legacy_archive" ]]; then
        info "旧配置归档: $legacy_archive"
        warn "旧版多余参数的运行值需在合适时间重启后恢复。"
    fi
}

# ────────────────────────── 清理旧版 TCP 调优 ──────────────────────────
remove_tcp_tune() {
    local conf=/etc/sysctl.d/99-tcp-tune.conf
    if [[ ! -e "$conf" && ! -L "$conf" ]]; then
        info "未发现 TCP 调优配置"
        return 0
    fi
    if ! is_managed_conf "$conf" tcp; then
        warn "配置不是本脚本生成的，已保留: $conf"
        return 1
    fi
    warn "移除配置后，已运行的参数要在重启后才会按系统默认值恢复。"
    read -rp "  确认移除本脚本的 TCP 调优？[y/N]: " input
    [[ "$input" =~ ^[Yy]$ ]] || return 0
    remove_managed_conf "$conf" tcp || return 1
    if [[ -f /etc/security/limits.conf ]] &&
       grep -Fxq '# tcp-tune: 提升文件描述符限制' /etc/security/limits.conf; then
        remove_limits_block || return 1
    fi
    ok "TCP 调优配置已移除，BBR 配置保留"
    warn "请在合适时间重启，再核对实际参数。"
}

tcp_tune() {
    hr
    echo "  1) 按机器与线路参数调优 TCP"
    echo "  2) 查看当前 TCP / 连接追踪状态"
    echo "  3) 移除本脚本的 TCP 调优配置"
    echo "  0) 返回"
    read -rp "  请选择 [0-3]: " choice
    case "$choice" in
        1) adaptive_tcp_tune ;;
        2) echo "  TCP 内存上限: $(tcp_memory_summary)";
           sysctl net.core.rmem_max net.core.wmem_max net.ipv4.tcp_rmem net.ipv4.tcp_wmem \
               net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max 2>/dev/null || true ;;
        3) remove_tcp_tune ;;
        0) return 0 ;;
        *) error "无效选项"; return 1 ;;
    esac
}

# ────────────────────────── Ubuntu/Debian 升级内核 ──────────────────────────
upgrade_kernel_debian() {
    hr
    info "为 $OS $VER 升级内核..."

    local kver major minor
    kver=$(uname -r | cut -d- -f1)
    major=$(echo "$kver" | cut -d. -f1)
    minor=$(echo "$kver" | cut -d. -f2)

    if [[ $major -gt 5 ]] || [[ $major -eq 5 && $minor -ge 4 ]]; then
        ok "当前内核 ${kver} 已满足要求，直接启用 BBR"
        enable_bbr
        return 0
    fi

    info "使用官方镜像源更新软件包列表..."

    # 尊重管理员已有的软件源，不为启用 BBR 改写系统镜像地址。
    apt-get update -qq || { error "软件源更新失败，内核未升级"; return 1; }

    local DPKG_ARCH
    DPKG_ARCH=$(dpkg --print-architecture)

    if [[ "$OS" == "ubuntu" ]]; then
        case "$VER" in
            20.*|22.*|24.*)
                info "安装 linux-generic（Ubuntu ${VER}）..."
                apt-get install -y linux-generic ;;
            18.*)
                info "安装 HWE 内核（Ubuntu 18.04）..."
                apt-get install -y --install-recommends linux-generic-hwe-18.04 ;;
            *)
                info "安装通用内核..."
                apt-get install -y --install-recommends linux-generic-hwe-16.04 2>/dev/null || \
                apt-get install -y linux-generic ;;
        esac
    elif [[ "$OS" == "debian" ]]; then
        info "安装 linux-image-${DPKG_ARCH}..."
        apt-get install -y linux-image-"$DPKG_ARCH"
    fi

    if [[ $? -eq 0 ]]; then
        echo ""
        echo -e "${GREEN}  ┌─────────────────────────────────────────────────┐${PLAIN}"
        echo -e "${GREEN}  │  ✅  内核升级完成，请重启后再启用 BBR           │${PLAIN}"
        echo -e "${GREEN}  └─────────────────────────────────────────────────┘${PLAIN}"
        echo ""
    else
        error "内核升级失败"
        return 1
    fi
}

# ────────────────────────── CentOS 升级内核 ──────────────────────────
upgrade_kernel_centos() {
    hr

    if [[ -n "$VER" && "$VER" -ge 8 ]] 2>/dev/null; then
        echo ""
        echo -e "${RED}  ┌─────────────────────────────────────────────────────┐${PLAIN}"
        echo -e "${RED}  │  ⚠  不支持 CentOS ${VER}+ 内核升级                   │${PLAIN}"
        echo -e "${RED}  │     CentOS 8+ 已停止维护，强升内核易导致系统损坏     │${PLAIN}"
        echo -e "${YELLOW}  │                                                     │${PLAIN}"
        echo -e "${YELLOW}  │  推荐迁移至：                                       │${PLAIN}"
        echo -e "${GREEN}  │    ✔  Ubuntu 20.04 / 22.04 / 24.04                │${PLAIN}"
        echo -e "${GREEN}  │    ✔  Debian 11 / 12                              │${PLAIN}"
        echo -e "${GREEN}  │    ✔  Rocky Linux 8/9 / AlmaLinux 8/9             │${PLAIN}"
        echo -e "${RED}  └─────────────────────────────────────────────────────┘${PLAIN}"
        echo ""
        read -n1 -rp "  按任意键返回..." _; return 1
    fi

    info "为 CentOS $VER 升级内核..."

    local kver major minor
    kver=$(uname -r | cut -d- -f1)
    major=$(echo "$kver" | cut -d. -f1)
    minor=$(echo "$kver" | cut -d. -f2)

    if [[ $major -gt 5 ]] || [[ $major -eq 5 && $minor -ge 4 ]]; then
        ok "内核 ${kver} 已支持 BBR，直接启用"
        enable_bbr
        return 0
    fi

    if [[ "$VER" == "7" ]]; then
        grep -q "^timeout=" /etc/yum.conf || echo "timeout=60" >> /etc/yum.conf
        grep -q "^retries=" /etc/yum.conf || echo "retries=3"  >> /etc/yum.conf

        info "导入 ELRepo GPG 密钥（官方）..."
        rpm --import https://www.elrepo.org/RPM-GPG-KEY-elrepo.org 2>/dev/null

        info "安装 ELRepo 源（官方）..."
        yum install -y https://www.elrepo.org/elrepo-release-7.el7.elrepo.noarch.rpm 2>/dev/null || \
            { error "ELRepo 安装失败"; return 1; }

        yum clean all -q

        echo ""
        echo -e "${YELLOW}  ┌─────────────────────────────────────────────────┐${PLAIN}"
        echo -e "${YELLOW}  │  ℹ  正在下载内核（约 150-200MB），请耐心等待   │${PLAIN}"
        echo -e "${YELLOW}  │     如长时间无进度，可按 Ctrl+C 中断重试        │${PLAIN}"
        echo -e "${YELLOW}  └─────────────────────────────────────────────────┘${PLAIN}"
        echo ""

        local attempt=1 success=0
        while [[ $attempt -le 3 ]]; do
            info "尝试安装内核（第 ${attempt}/3 次）..."
            yum --enablerepo=elrepo-kernel install -y kernel-ml kernel-ml-devel && success=1 && break
            error "安装失败，${attempt}/3 次"; yum clean all -q; ((attempt++)); sleep 3
        done

        if [[ $success -eq 1 ]]; then
            grub2-set-default 0
            grub2-mkconfig -o /boot/grub2/grub.cfg
            echo ""
            echo -e "${GREEN}  ✅  内核升级完成，请重启后再启用 BBR${PLAIN}"
            echo ""
        else
            error "内核升级失败（3 次均未成功），请手动操作或更换系统"
            return 1
        fi
    else
        error "CentOS $VER 不支持自动升级内核"
        return 1
    fi
}

# ────────────────────────── 清理旧内核 ──────────────────────────
remove_old_kernels() {
    hr
    info "检测已安装内核..."

    if [[ "$OS" =~ centos|rhel ]]; then
        local installed_kernels kernel_count old_kernels current_package
        installed_kernels=$(rpm -qa | grep '^kernel-[0-9]' | sort -V)
        kernel_count=$(printf '%s\n' "$installed_kernels" | grep -c .)
        current_package=$(rpm -qf "/boot/vmlinuz-$(uname -r)" 2>/dev/null) || current_package=""

        if [[ $kernel_count -gt 2 ]]; then
            old_kernels=$(printf '%s\n' "$installed_kernels" | head -n -2)
            [[ -n "$current_package" ]] && old_kernels=$(printf '%s\n' "$old_kernels" | grep -vxF "$current_package")
            if [[ -z "$old_kernels" ]]; then
                ok "已保留正在运行及最新的内核，无需清理"
                return 0
            fi
            echo ""
            warn "发现 ${kernel_count} 个内核，将保留正在运行和最新 2 个，以下内核将被删除："
            printf '%s\n' "$old_kernels" | sed 's/^/    /'
            echo ""
            read -rp "  确认删除? [y/N]: " confirm_remove
            if [[ "$confirm_remove" =~ ^[Yy]$ ]]; then
                printf '%s\n' "$old_kernels" | xargs -r yum remove -y -q || return 1
                ok "旧内核清理完成"
            else
                warn "已取消"
            fi
        else
            ok "当前内核数量 ${kernel_count} 个，无需清理"
        fi

    elif [[ "$OS" =~ debian|ubuntu ]]; then
        local current_kernel installed_kernels keep_latest old_kernels kernel
        current_kernel=$(uname -r)
        installed_kernels=$(dpkg -l | awk '$1 == "ii" && $2 ~ /^linux-image-[0-9]/{print $2}' | sort -V)
        keep_latest=$(printf '%s\n' "$installed_kernels" | grep . | tail -n 2)
        old_kernels=""
        for kernel in $installed_kernels; do
            [[ "$kernel" == "linux-image-$current_kernel" ]] && continue
            printf '%s\n' "$keep_latest" | grep -Fxq "$kernel" && continue
            old_kernels+="${kernel}"$'\n'
        done

        echo ""
        info "当前运行内核: ${BOLD}${current_kernel}${PLAIN}"
        if [[ -z "$old_kernels" ]]; then
            ok "已保留正在运行及最新 2 个内核，无需清理"
            return 0
        fi
        warn "将保留正在运行及最新 2 个内核，拟删除："
        printf '%s' "$old_kernels" | sed 's/^/    /'
        echo ""
        read -rp "  确认清理以上旧内核? [y/N]: " confirm_remove
        if [[ "$confirm_remove" =~ ^[Yy]$ ]]; then
            for kernel in $old_kernels; do
                info "移除: $kernel"
                apt-get purge -y "$kernel" -qq || return 1
            done
            ok "旧内核清理完成"
        else
            warn "已取消"
        fi
    fi
}

# ────────────────────────── 系统状态展示 ──────────────────────────
status_line() {
    local label="$1" value="$2" color="${3:-}"
    printf "  ${CYAN}%s${PLAIN}  " "$label"
    if [[ -n "$color" ]]; then
        printf "${color}%s${PLAIN}\n" "$value"
    else
        printf '%s\n' "$value"
    fi
}

tcp_memory_summary() {
    local max_pages page_size ram_kib max_mib ram_mib
    max_pages=$(sysctl -n net.ipv4.tcp_mem 2>/dev/null | awk '{print $3}')
    page_size=$(getconf PAGESIZE 2>/dev/null) || page_size=4096
    ram_kib=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)
    if [[ "$max_pages" =~ ^[0-9]+$ && "$page_size" =~ ^[0-9]+$ && "$ram_kib" =~ ^[0-9]+$ ]]; then
        max_mib=$(( max_pages * page_size / 1024 / 1024 ))
        ram_mib=$(( ram_kib / 1024 ))
        printf 'max %s MiB / 物理内存 %s MiB' "$max_mib" "$ram_mib"
    else
        printf '未知'
    fi
}

show_status() {
    hr
    local bbr_status bbr_color bbr_mod bbr_mod_color qdisc cc iface fq_count
    if check_bbr_status; then
        bbr_status="已启用"
        bbr_color="$GREEN"
    else
        bbr_status="未启用"
        bbr_color="$RED"
    fi
    if lsmod | grep -q bbr; then
        bbr_mod="已加载（模块）"
        bbr_mod_color="$GREEN"
    elif grep -q 'CONFIG_TCP_CONG_BBR=y' /boot/config-$(uname -r) 2>/dev/null \
      || zcat /proc/config.gz 2>/dev/null | grep -q 'CONFIG_TCP_CONG_BBR=y'; then
        bbr_mod="已内置（built-in）"
        bbr_mod_color="$GREEN"
    elif check_bbr_status; then
        bbr_mod="运行中（内置）"
        bbr_mod_color="$GREEN"
    else
        bbr_mod="未加载"
        bbr_mod_color="$RED"
    fi
    qdisc=$(sysctl -n net.core.default_qdisc        2>/dev/null || echo "未设置")
    cc=$(sysctl -n net.ipv4.tcp_congestion_control  2>/dev/null || echo "未设置")

    status_line "操作系统:" "$OS $VER"
    status_line "系统架构:" "$ARCH"
    status_line "内核版本:" "$(uname -r)"
    status_line "BBR 默认:" "● $bbr_status" "$bbr_color"
    status_line "BBR 模块:" "● $bbr_mod" "$bbr_mod_color"
    status_line "默认队列:" "$qdisc"
    status_line "拥塞控制:" "$cc"
    if is_managed_conf /etc/sysctl.d/99-bbr.conf bbr &&
       grep -q '^net.core.rmem_max' /etc/sysctl.d/99-bbr.conf; then
        status_line "BBR 配置:" "● 旧版文件含固定 TCP 参数（菜单 1 可更新）" "$YELLOW"
    fi
    iface=$(ip -4 route show default 2>/dev/null | awk 'NR == 1 {for (i=1; i<NF; i++) if ($i == "dev") {print $(i+1); exit}}')
    if [[ -n "$iface" ]] && command -v tc >/dev/null 2>&1; then
        fq_count=$(tc qdisc show dev "$iface" 2>/dev/null | awk '$1 == "qdisc" && $2 == "fq" {n++} END {print n+0}')
        status_line "当前网卡:" "${iface}（fq 队列 ${fq_count} 个）"
    fi

    # 仅按文件存在不能证明每个参数都已应用。
    if [[ -f /etc/sysctl.d/99-tcp-tune.conf ]]; then
        if grep -q '^# adaptive-tcp-tune:' /etc/sysctl.d/99-tcp-tune.conf; then
            status_line "TCP 调优:" "● 自适应配置已保存（菜单 2 可核对运行值）" "$GREEN"
        else
            status_line "TCP 调优:" "● 旧版配置仍在，菜单 2 可检查" "$YELLOW"
        fi
    else
        status_line "TCP 调优:" "● 未发现配置文件" "$YELLOW"
    fi
    status_line "TCP 内存:" "$(tcp_memory_summary)"
    status_line "连接追踪:" "$(sysctl -n net.netfilter.nf_conntrack_count 2>/dev/null || echo 未知) / $(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 未知)"
    hr
}

# ────────────────────────── 主菜单 ──────────────────────────
show_menu() {
    clear
    banner
    show_status

    echo -e "  ${BOLD}请选择操作：${PLAIN}"
    echo ""
    echo -e "  ${GREEN}1.${PLAIN}  安装 / 启用 BBR"
    echo -e "  ${GREEN}2.${PLAIN}  自适应 TCP 调优 / 检查 / 清理"
    echo -e "  ${GREEN}3.${PLAIN}  升级内核（Ubuntu / Debian）"
    echo -e "  ${GREEN}4.${PLAIN}  升级内核（CentOS 7）"
    echo -e "  ${GREEN}5.${PLAIN}  清理旧内核"
    echo -e "  ${GREEN}6.${PLAIN}  查看当前系统状态"
    echo -e "  ${GREEN}7.${PLAIN}  一键卸载优化配置"
    echo ""
    echo -e "  ${GREEN}0.${PLAIN}  退出"
    echo ""
    hr
    read -rp "  请输入选项 [0-7]: " choice
    echo ""

    case $choice in
        1)
            enable_bbr
            ;;
        2)
            tcp_tune
            ;;
        3)
            if [[ "$OS" =~ debian|ubuntu ]]; then
                check_boot_space
                if upgrade_kernel_debian; then
                    read -rp "  是否现在重启? [y/N]: " reboot_now
                    [[ "$reboot_now" =~ ^[Yy]$ ]] && reboot
                fi
            else
                error "此选项仅适用于 Ubuntu / Debian"
            fi
            ;;
        4)
            if [[ "$OS" =~ centos|rhel ]]; then
                check_boot_space
                if upgrade_kernel_centos; then
                    read -rp "  是否现在重启? [y/N]: " reboot_now
                    [[ "$reboot_now" =~ ^[Yy]$ ]] && reboot
                fi
            else
                error "此选项仅适用于 CentOS / RHEL"
            fi
            ;;
        5)
            remove_old_kernels
            ;;
        6)
            show_status
            ;;
        7)
            uninstall_optimizations
            ;;
        0)
            echo -e "${GREEN}  感谢使用 BBR 脚本，再见！${PLAIN}"
            echo ""
            exit 0
            ;;
        *)
            error "无效选项，请输入 0-7"
            ;;
    esac

    echo ""
    read -n1 -rp "  按任意键继续..." _
    echo ""
    show_menu
}

# ════════════════════════════════════════════
#   入口
# ════════════════════════════════════════════
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall_optimizations
    exit $?
fi

show_menu

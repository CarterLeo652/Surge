"""Telegram HTML output matching the TeleBox text presentation."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from .telegram_photo import telegram_destination
from .whois_original import _expiry_note


def mark_handled(event) -> None:
    """A direct Telegram send owns this command; suppress later pipeline replies."""
    event.clear_result()
    event.stop_event()


async def send_html(event, content: str, *, preview: bool | None = None,
                    on_sent=None) -> bool:
    destination = telegram_destination(event)
    if destination is None:
        return False
    chat_id, thread_id = destination
    options = {"chat_id": chat_id, "text": content, "parse_mode": "HTML"}
    if preview is False:
        options["disable_web_page_preview"] = True
    if thread_id is not None:
        options["message_thread_id"] = thread_id
    sent = await event.client.send_message(**options)
    if on_sent is not None:
        on_sent(event, sent)
    mark_handled(event)
    return True


async def start_progress(event, content: str):
    """Create one bot-owned status message that can be edited during a batch."""
    destination = telegram_destination(event)
    if destination is None:
        return None
    chat_id, thread_id = destination
    options = {"chat_id": chat_id, "text": content, "parse_mode": "HTML"}
    if thread_id is not None:
        options["message_thread_id"] = thread_id
    try:
        return await event.client.send_message(**options)
    except Exception:
        return None


async def finish_progress(event, progress, content: str, *, preview: bool | None = None,
                          on_sent=None) -> bool:
    if progress is not None:
        try:
            options = {"parse_mode": "HTML"}
            if preview is False:
                options["disable_web_page_preview"] = True
            await progress.edit_text(content, **options)
            if on_sent is not None:
                on_sent(event, progress)
            mark_handled(event)
            return True
        except Exception:
            if on_sent is not None:
                on_sent(event, progress)
    return await send_html(event, content, preview=preview, on_sent=on_sent)


def ip_help_html() -> str:
    return ("📍 <b>IP查询插件</b>\n\n"
            "<b>使用方法：</b>\n"
            "• <code>/vps_ip &lt;IP地址&gt;</code>\n"
            "• <code>/vps_ip &lt;域名&gt;</code>\n"
            "• 回复包含IP/域名的消息后使用 <code>/vps_ip</code>\n\n"
            "<b>示例：</b>\n"
            "• <code>/vps_ip 8.8.8.8</code>\n"
            "• <code>/vps_ip google.com</code>\n"
            "• <code>/vps_ip 2001:4860:4860::8888</code>")


def whois_help_html() -> str:
    return ("🔍 <b>WHOIS 域名查询</b>\n\n"
            "<b>📝 功能：</b>\n"
            "• 查询域名注册信息和状态\n"
            "• 显示注册/过期/更新日期\n"
            "• 查看DNS服务器和注册商\n"
            "• 批量查询多个域名\n"
            "• 查询历史记录缓存\n"
            "• 域名到期提醒\n\n"
            "<b>🔧 使用：</b>\n"
            "• <code>/vps_whois &lt;域名&gt;</code> - 查询指定域名\n"
            "• <code>/vps_whois</code> - 回复包含域名的消息\n"
            "• <code>/vps_whois batch &lt;域名1&gt; &lt;域名2&gt;...</code> - 批量查询\n"
            "• <code>/vps_whois history</code> - 查看查询历史\n"
            "• <code>/vps_whois clear</code> - 清除历史记录\n\n"
            "<b>💡 示例：</b>\n"
            "• <code>/vps_whois google.com</code>\n"
            "• <code>/vps_whois batch google.com github.com</code>\n\n"
            "<b>📌 说明：</b>\n"
            "• 支持自动提取URL中的域名\n"
            "• 支持回复消息中的域名提取\n"
            "• 查询结果自动缓存24小时\n"
            "• 支持批量查询（最多10个）\n"
            "• 自动检测即将过期的域名")
def ip_html(data: dict, query: str) -> str:
    if data.get("status") == "fail":
        reason = escape(str(data.get("message") or "未知错误"))
        return (
            "❌ <b>查询失败</b>\n\n"
            f"<b>查询目标:</b> <code>{escape(query)}</code>\n"
            f"<b>失败原因:</b> {reason}\n\n"
            "💡 <b>建议:</b>\n• 检查IP地址或域名格式\n• 稍后重试查询"
        )
    country = escape(str(data.get("country") or "N/A"))
    region = escape(str(data.get("regionName") or "N/A"))
    city = escape(str(data.get("city") or "N/A"))
    isp = escape(str(data.get("isp") or "N/A"))
    org = escape(str(data.get("org") or "N/A"))
    as_info = str(data.get("as") or "N/A")
    address = escape(str(data.get("query") or "N/A"))
    result = ""
    if data.get("proxy"):
        result += "此 IP 可能为代理 IP\n"
    if data.get("hosting"):
        result += "此 IP 可能为数据中心 IP\n"
    if result:
        result += "\n"
    result += (
        "🌍 <b>IP/域名查询结果</b>\n\n"
        f"<b>🔍 查询目标:</b> <code>{address}</code>\n"
        f"<b>📍 地理位置:</b> {country} - {region} - {city}\n"
        f"<b>🏢 ISP:</b> {isp}\n"
        f"<b>🏦 组织:</b> {org}\n"
        f"<b>🔢 AS号:</b> <code>{escape(as_info)}</code>"
    )
    if data.get("timezone"):
        result += f"\n<b>⏰ 时区:</b> {escape(str(data['timezone']))}"
    import re
    match = re.match(r"AS(\d+)", as_info)
    if match:
        result += f"\n\nhttps://bgp.he.net/AS{match.group(1)}"
    return result


def whois_html(record: dict, *, cached: bool = False) -> str:
    result = "✅ <b>WHOIS 查询结果</b>"
    if cached:
        result += f" <i>（缓存: {_from_now(record.get('queryTime'))}）</i>"
    result += f"\n\n<b>🌐 域名：</b> <code>{escape(str(record.get('domain') or ''))}</code>\n\n"
    for key, label in (("registrar", "📋 注册商"), ("createdDate", "📅 注册日期")):
        if record.get(key):
            result += f"<b>{label}：</b> {escape(str(record[key]))}\n"
    if record.get("expiryDate"):
        result += f"<b>⏰ 过期日期：</b> {escape(str(record['expiryDate']))}"
        note = _expiry_note(record["expiryDate"])
        if "已过期" in note or "⚠️" in note:
            result += f" <b>{escape(note.strip())}</b>"
        elif note:
            result += f" <i>{escape(note.strip())}</i>"
        result += "\n"
    for key, label in (("updatedDate", "🔄 更新日期"), ("status", "📊 域名状态")):
        if record.get(key):
            result += f"<b>{label}：</b> {escape(str(record[key]))}\n"
    if record.get("nameServers"):
        result += "\n<b>🖥️ DNS 服务器：</b>\n"
        for name in record["nameServers"][:5]:
            result += f"• <code>{escape(str(name))}</code>\n"
    raw = str(record.get("rawData") or "")
    if raw:
        result += "\n<b>📄 原始 WHOIS 数据：</b>\n"
        result += f"<blockquote expandable>{escape(raw[:3000])}</blockquote>"
        if len(raw) > 3000:
            result += "\n<i>（数据已截断，仅显示前 3000 字符）</i>"
    return result


def _from_now(value: str | None) -> str:
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        seconds = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    except ValueError:
        return "几秒前"
    if seconds < 45:
        amount = "几秒"
    elif seconds < 90:
        amount = "1 分钟"
    elif seconds < 45 * 60:
        amount = f"{round(seconds / 60)} 分钟"
    elif seconds < 90 * 60:
        amount = "1 小时"
    elif seconds < 22 * 3600:
        amount = f"{round(seconds / 3600)} 小时"
    elif seconds < 36 * 3600:
        amount = "1 天"
    elif seconds < 26 * 86400:
        amount = f"{round(seconds / 86400)} 天"
    elif seconds < 45 * 86400:
        amount = "1 个月"
    elif seconds < 320 * 86400:
        amount = f"{round(seconds / 2592000)} 个月"
    elif seconds < 548 * 86400:
        amount = "1 年"
    else:
        amount = f"{round(seconds / 31536000)} 年"
    return amount + "前"


def whois_history_html(records: list[dict], total: int, cache_count: int) -> str:
    if not records:
        return "📭 <b>暂无查询历史</b>\n\n💡 使用 <code>/vps_whois &lt;域名&gt;</code> 开始查询"
    rows = [f"📜 <b>查询历史</b> <i>（最近 {len(records)} 条）</i>\n"]
    for index, record in enumerate(records, 1):
        domain = escape(str(record.get("domain") or "?"))
        time = str(record.get("queryTime") or "")
        try:
            date = datetime.fromisoformat(time.replace("Z", "+00:00")).astimezone()
            displayed = f"{date:%m-%d %H:%M}"
        except ValueError:
            displayed = time[:16].replace("T", " ")
        note = _expiry_note(record.get("expiryDate"))
        rows.append(f"{index}. <code>{domain}</code>\n   <i>{escape(displayed)} ({_from_now(time)})</i>"
                    + (f"\n   ⚠️ <b>{escape(note.strip().removeprefix('⚠️').strip())}</b>" if "⚠️" in note else "") + "\n")
    rows.append(f"<b>统计信息：</b>\n• 总查询次数：{total}\n• 缓存域名数：{cache_count}"
                "\n• 缓存时长：24 小时\n\n💡 使用 <code>/vps_whois clear</code> 清除历史记录")
    return "\n".join(rows)


def whois_batch_html(results: list[str], success: int) -> str:
    return (
        "📊 <b>批量查询完成</b>\n\n"
        f"<b>成功：</b> {success}\n"
        f"<b>失败：</b> {len(results) - success}\n\n"
        "<b>查询结果：</b>\n"
        + "\n".join(results)
        + "\n\n💡 使用 <code>/vps_whois history</code> 查看详细信息"
    )


def bgp_dns_html(target: str, prefix: str, lines: list[str]) -> str:
    table = "A\tDNS\n" + "\n".join(lines)
    now = datetime.now().astimezone()
    return (
        f"<blockquote expandable>{escape(table)}</blockquote>\n\n"
        f"🌐 <b>DNS解析记录</b>\n\n"
        f"<code>{escape(target)}</code>\n"
        f"<i>使用前缀: {escape(prefix)}</i>\n\n"
        f"⏰ <i>{now.year}/{now.month}/{now.day} {now:%H:%M:%S}</i>"
    )

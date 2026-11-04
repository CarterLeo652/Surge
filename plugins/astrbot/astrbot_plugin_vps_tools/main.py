"""AstrBot handlers for the TeleBox VPS lookup feature set."""

from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape as html_escape, unescape as html_unescape
import ipaddress
from pathlib import Path
import re
from urllib.error import HTTPError, URLError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .auto_delete import AutoDelete
from .bgp_original import extract_bgp_ip, fetch_bgp_dns, fetch_bgp_image
from .dig_original import lookup_dig_native
from .group_scope import group_allowed
from .ip_original import query_ip
from .queries import QueryError
from .telegram_photo import send_bgp_photo
from .telegram_text import (bgp_dns_html, finish_progress, ip_help_html, ip_html, mark_handled, send_html,
                            start_progress, whois_batch_html, whois_help_html,
                            whois_history_html, whois_html)
from .whois_history import WhoisHistory
from .whois_original import clean_domain, find_domain, format_history, normalize_domain, query_whois, query_whois_raw


_IPV4_TEXT = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?!\d)")


def _args(event: AstrMessageEvent) -> str:
    parts = event.message_str.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _plain_html(content: str) -> str:
    return html_unescape(re.sub(r"<[^>]*>", "", content))


def _reply_text(event: AstrMessageEvent) -> str:
    for segment in event.get_messages():
        if isinstance(segment, Reply):
            if segment.message_str:
                return segment.message_str
            if segment.chain:
                return " ".join(item.text for item in segment.chain
                                if isinstance(item, Plain))
    return ""


def _ip_from_text(text: str, *, ipv4_only: bool = False) -> str | None:
    if not text:
        return None
    for match in _IPV4_TEXT.finditer(text):
        candidate = match.group(0)
        try:
            ipaddress.IPv4Address(candidate.split("/", 1)[0])
            return candidate
        except ipaddress.AddressValueError:
            continue
    if ipv4_only:
        return None
    for word in re.split(r"\s+", text):
        candidate = word.strip("<>[](),;\"'")
        try:
            ipaddress.IPv6Address(candidate)
            return candidate
        except ipaddress.AddressValueError:
            continue
    return None


class VpsTools(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.auto_delete = AutoDelete(config)
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_vps_tools"
        self.whois_history = WhoisHistory(data_dir)

    async def initialize(self):
        await self.whois_history.load()

    async def _send_html(self, event, content: str, *, preview: bool | None = None) -> bool:
        return await send_html(event, content, preview=preview,
                               on_sent=self.auto_delete.schedule)

    async def _finish_progress(self, event, progress, content: str,
                               *, preview: bool | None = None) -> bool:
        return await finish_progress(event, progress, content, preview=preview,
                                     on_sent=self.auto_delete.schedule)

    async def _send_bgp_photo(self, event, png: bytes, target: str, prefix: str) -> bool:
        return await send_bgp_photo(event, png, target, prefix,
                                    on_sent=self.auto_delete.schedule)

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        return group_allowed(event.get_group_id(),
                             self.config.get("enabled_groups", []),
                             self.config.get("disabled_groups", []),
                             enabled=self.config.get("enabled", True))

    @filter.command("vps_ip")
    async def vps_ip(self, event: AstrMessageEvent):
        """查询 IP/域名归属地、ASN、代理与机房标记"""
        if not self._group_allowed(event):
            mark_handled(event)
            return
        target = _args(event)
        if not target:
            reply = _reply_text(event)
            from_reply = _ip_from_text(reply, ipv4_only=True)
            target = ((from_reply.split("/", 1)[0] if from_reply else None)
                      or find_domain(reply) or reply.strip().split(" ", 1)[0])
        if not target:
            content = ip_help_html()
            if await self._send_html(event, content):
                return
            yield event.plain_result(_plain_html(content))
            return
        progress = await start_progress(event, f"🔍 <b>正在查询:</b> <code>{html_escape(target)}</code>")
        try:
            data = await query_ip(target)
            result = ip_html(data, target)
            if await self._finish_progress(event, progress, result):
                return
            yield event.plain_result(_plain_html(result))
        except QueryError as exc:
            content = f"IP 查询失败：{html_escape(str(exc))}"
            if await self._finish_progress(event, progress, content):
                return
            yield event.plain_result(_plain_html(content))
        except Exception:
            logger.exception("VPS IP lookup failed")
            content = "IP 查询失败：查询服务暂时不可用"
            if await self._finish_progress(event, progress, content):
                return
            yield event.plain_result(content)

    @filter.command("vps_bgp")
    async def vps_bgp(self, event: AstrMessageEvent):
        """生成 BGP 路由图，或查询 bgp.tools 的 DNS 记录"""
        if not self._group_allowed(event):
            mark_handled(event)
            return
        raw = _args(event)
        parts = raw.split(maxsplit=1)
        dns_mode = bool(parts and parts[0].lower() == "dns")
        supplied = parts[1].strip() if dns_mode and len(parts) > 1 else ("" if dns_mode else raw)
        target = extract_bgp_ip(supplied) or ""
        if not target:
            target = extract_bgp_ip(_reply_text(event)) or ""
        if not target:
            command = "/vps_bgp dns" if dns_mode else "/vps_bgp"
            content = ("❌ 请提供有效的IP地址\n\n支持的格式:\n"
                       f"• {command} 1.1.1.1\n• {command} 1.1.1.0/24\n"
                       f"• 回复包含IP的消息使用 {command}")
            if await self._send_html(event, content):
                return
            yield event.plain_result(content)
            return
        try:
            if dns_mode:
                try:
                    prefix, lines = await fetch_bgp_dns(target)
                except QueryError as exc:
                    if "未找到DNS记录" in str(exc):
                        prefix = str(ipaddress.ip_network(f"{target}/24", strict=False))
                        content = ("❌ <b>未找到DNS解析记录</b>\n\n"
                                   "请确认该前缀是否在公网上有宣告或有可见的 DNS 记录\n\n"
                                   f"🔗 直达链接: https://bgp.tools/prefix/{prefix}#dns")
                        if await self._send_html(event, content):
                            return
                        yield event.plain_result(_plain_html(content))
                        return
                    raise
                if await self._send_html(event, bgp_dns_html(target, prefix, lines)):
                    return
                content = "\n".join(lines)
                yield event.plain_result(
                    f"🌐 BGP DNS 解析记录\n目标：{target}\n前缀：{prefix}\n"
                    f"A\tDNS\n{content[:3300]}\n"
                    f"https://bgp.tools/prefix/{prefix}#dns\n"
                    f"查询时间：{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"
                )
                return
            result = await fetch_bgp_image(target)
            link = f"https://bgp.tools/prefix/{result.prefix}"
            if result.png:
                if await self._send_bgp_photo(event, result.png, target, result.prefix):
                    return
                yield event.chain_result([
                    Image.fromBytes(result.png),
                    Plain(f"🌐 BGP 路由图\n目标：{target}\n前缀：{result.prefix}\n"
                          f"查询时间：{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n{link}"),
                ])
            else:
                if result.placeholder:
                    content = ("❌ <b>没有可用的BGP路由图</b>\n\n"
                               f"当前前缀 <code>{result.prefix}</code> 在 DFZ 中不可见或没有路径数据\n\n"
                               f"🔗 直达链接: {link}")
                else:
                    content = ("❌ <b>未找到可用的BGP路由图</b>\n\n"
                               "请确认该前缀是否在公网上有宣告\n\n"
                               f"🔗 直达链接: {link}")
                if await self._send_html(event, content):
                    return
                yield event.plain_result(_plain_html(content))
        except QueryError as exc:
            content = f"❌ <b>BGP查询失败</b>\n\n{html_escape(str(exc))}"
            if await self._send_html(event, content):
                return
            yield event.plain_result(_plain_html(content))
        except Exception as exc:
            logger.exception("VPS BGP lookup failed")
            content = f"❌ <b>BGP查询失败</b>\n\n{html_escape(str(exc) or '未知错误')}"
            if await self._send_html(event, content):
                return
            yield event.plain_result(_plain_html(content))

    @filter.command("vps_whois")
    async def vps_whois(self, event: AstrMessageEvent):
        """查询 WHOIS、批量查询、历史和 24 小时缓存"""
        if not self._group_allowed(event):
            mark_handled(event)
            return
        raw = _args(event)
        parts = raw.split()
        sub = parts[0].lower() if parts else ""
        if sub in {"help", "h"}:
            content = whois_help_html()
            if await self._send_html(event, content, preview=False):
                return
            yield event.plain_result(_plain_html(content))
            return
        if sub == "history":
            records, total, count = await self.whois_history.history()
            if await self._send_html(event, whois_history_html(records, total, count), preview=False):
                return
            yield event.plain_result(format_history(records, total, count)[:3800])
            return
        if sub == "clear":
            total, count = await self.whois_history.clear()
            if total == 0 and count == 0:
                content = "📭 <b>没有需要清除的记录</b>"
            else:
                content = ("🗑️ <b>清除完成</b>\n\n"
                           f"• 清除历史记录：{total} 条\n• 清除缓存：{count} 个域名")
            if await self._send_html(event, content, preview=False):
                return
            yield event.plain_result(f"🗑️ 已清除历史 {total} 条、缓存 {count} 个域名")
            return
        if sub == "batch":
            if not parts[1:]:
                content = ("❌ <b>请提供要查询的域名</b>\n\n"
                           "💡 使用示例：<code>/vps_whois batch google.com github.com</code>")
                if await self._send_html(event, content, preview=False):
                    return
                yield event.plain_result(_plain_html(content))
                return
            if len(parts[1:]) > 10:
                content = ("❌ <b>批量查询限制</b>\n\n"
                           f"每次最多查询 10 个域名，您提供了 {len(parts[1:])} 个")
                if await self._send_html(event, content, preview=False):
                    return
                yield event.plain_result(_plain_html(content))
                return
            progress = await start_progress(
                event, f"🔍 <b>批量查询中...</b>\n\n<b>域名数量：</b> {len(parts[1:])}"
            )
            results = []
            html_results = []
            success = 0
            for index, value in enumerate(parts[1:]):
                domain = clean_domain(value)
                if progress is not None:
                    try:
                        await progress.edit_text(
                            f"🔍 <b>批量查询中...</b>\n\n<b>进度：</b> {index + 1}/{len(parts[1:])}"
                            f"\n<b>当前域名：</b> <code>{html_escape(domain)}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        logger.warning("Could not update VPS WHOIS batch progress")
                try:
                    record = await self.whois_history.cached(domain)
                    if record is not None:
                        results.append(f"✅ {domain} - 缓存")
                        html_results.append(f"✅ <code>{html_escape(domain)}</code> - <i>缓存</i>")
                        success += 1
                        continue
                    record = await query_whois_raw(domain)
                    await self.whois_history.save(record)
                    results.append(f"✅ {domain}")
                    html_results.append(f"✅ <code>{html_escape(domain)}</code>")
                    success += 1
                except QueryError:
                    results.append(f"❌ {domain} - 查询失败")
                    html_results.append(f"❌ <code>{html_escape(domain)}</code> - 查询失败")
                except Exception:
                    logger.exception("VPS WHOIS batch lookup failed")
                    results.append(f"❌ {domain} - 查询失败")
                    html_results.append(f"❌ <code>{html_escape(domain)}</code> - 查询失败")
                if index < len(parts[1:]) - 1:
                    await asyncio.sleep(0.5)
            final_html = whois_batch_html(html_results, success)
            if progress is not None:
                try:
                    await progress.edit_text(final_html, parse_mode="HTML", disable_web_page_preview=True)
                    self.auto_delete.schedule(event, progress)
                    mark_handled(event)
                    return
                except Exception:
                    logger.warning("Could not finish VPS WHOIS batch progress")
                    self.auto_delete.schedule(event, progress)
            if await self._send_html(event, final_html, preview=False):
                return
            yield event.plain_result(
                f"📊 批量查询完成\n成功：{success}；失败：{len(results) - success}\n"
                + "\n".join(results) + "\n使用 /vps_whois history 查看详细信息"
            )
            return

        target = find_domain(_reply_text(event)) or (parts[0].lower() if parts else "")
        if not target:
            content = ("❌ <b>请指定域名</b>\n\n"
                       "💡 用法: <code>/vps_whois &lt;域名&gt;</code>\n"
                       "或回复包含域名的消息后使用 <code>/vps_whois</code>")
            if await self._send_html(event, content, preview=False):
                return
            yield event.plain_result(_plain_html(content))
            return
        progress = None
        try:
            domain = normalize_domain(target)
            record = await self.whois_history.cached(domain)
            cached = record is not None
            if record is None:
                progress = await start_progress(
                    event, f"🔍 <b>正在查询域名信息...</b>\n\n<b>域名：</b> <code>{html_escape(domain)}</code>"
                )
                record = await query_whois(domain)
                await self.whois_history.save(record)
            if await self._finish_progress(event, progress, whois_html(record, cached=cached), preview=False):
                return
            yield event.plain_result(_plain_html(whois_html(record, cached=cached))[:3800])
        except QueryError as exc:
            if str(exc) == "未查询到 WHOIS 数据":
                content = ("❌ <b>查询失败</b>\n\n"
                           f"<b>域名：</b> <code>{html_escape(target)}</code>\n\n"
                           "💡 可能的原因：\n• 域名不存在或未注册\n"
                           "• 域名格式不正确\n• WHOIS 信息不可用\n\n"
                           "📖 请检查域名拼写是否正确")
                if await self._finish_progress(event, progress, content, preview=False):
                    return
            elif str(exc) == "域名格式无效":
                content = ("❌ <b>域名格式无效</b>\n\n"
                           f"<b>输入的域名：</b> <code>{html_escape(target)}</code>\n\n"
                           "💡 请输入有效的域名，例如：\n"
                           "• example.com\n• google.com\n• github.io")
                if await self._finish_progress(event, progress, content, preview=False):
                    return
            content = f"WHOIS 查询失败：{html_escape(str(exc))}"
            if await self._finish_progress(event, progress, content, preview=False):
                return
            yield event.plain_result(_plain_html(content))
        except Exception as exc:
            logger.exception("VPS WHOIS lookup failed")
            if isinstance(exc, HTTPError) and exc.code == 429:
                detail = "<b>错误：</b> 请求过于频繁\n\n💡 请稍后再试"
            elif isinstance(exc, HTTPError) and exc.code == 403:
                detail = "<b>错误：</b> API 访问被拒绝\n\n💡 可能需要更换 API 服务"
            elif isinstance(exc, HTTPError):
                detail = (f"<b>错误代码：</b> {exc.code}\n"
                          f"<b>错误信息：</b> {html_escape(str(exc))}\n\n💡 请稍后重试")
            elif isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                detail = "<b>错误：</b> 请求超时\n\n💡 请检查网络连接后重试"
            elif isinstance(exc, URLError):
                detail = "<b>错误：</b> 无法连接到 API 服务器\n\n💡 请检查网络连接"
            else:
                detail = f"<b>错误信息：</b> {html_escape(str(exc))}\n\n💡 请稍后重试"
            content = "❌ <b>查询失败</b>\n\n" + detail
            if await self._finish_progress(event, progress, content, preview=False):
                return
            yield event.plain_result(_plain_html(content))

    @filter.command("vps_dig")
    async def vps_dig(self, event: AstrMessageEvent):
        """调用系统 dig 查询 DNS，支持记录类型、服务器及常用选项"""
        if not self._group_allowed(event):
            mark_handled(event)
            return
        target = _args(event)
        if not target:
            content = "❌ 未提供任何参数"
            if await self._send_html(event, content, preview=False):
                return
            yield event.plain_result(content)
            return
        progress = await start_progress(event, "🔍 正在执行 DNS 查询...")
        try:
            result = await lookup_dig_native(target)
            if await self._finish_progress(event, progress, result, preview=False):
                return
            yield event.plain_result(_plain_html(result))
        except QueryError as exc:
            content = ("❌ <b>DNS 查询失败</b>\n\n"
                       f"<b>错误详情:</b>\n<code>{html_escape(str(exc))}</code>")
            if "未安装 dig 工具" in str(exc):
                content += ("\n\n<b>解决方案:</b>\n"
                            "• Ubuntu/Debian: <code>sudo apt-get install dnsutils</code>\n"
                            "• RHEL/CentOS: <code>sudo yum install bind-utils</code>\n"
                            "• Fedora: <code>sudo dnf install bind-utils</code>")
            if await self._finish_progress(event, progress, content, preview=False):
                return
            yield event.plain_result(_plain_html(content))
        except Exception:
            logger.exception("VPS dig lookup failed")
            content = "DNS 查询失败：dig 执行异常"
            if await self._finish_progress(event, progress, content, preview=False):
                return
            yield event.plain_result(content)

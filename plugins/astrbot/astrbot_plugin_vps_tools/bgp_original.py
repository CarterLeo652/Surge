"""BGP image and reverse-DNS views compatible with the TeleBox commands."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import gzip
from html import unescape
from html.parser import HTMLParser
import ipaddress
import re
import struct
import zlib
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .queries import QueryError


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://bgp.tools/",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="122", "Google Chrome";v="122", "Not=A?Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Dnt": "1",
    "Sec-Gpc": "1",
    "Pragma": "no-cache",
}
_MAX_SVG = 4_000_000
_MAX_HTML = 2_000_000


@dataclass(frozen=True)
class BgpImage:
    png: bytes | None
    prefix: str
    placeholder: bool = False


def _ipv4(value: str) -> ipaddress.IPv4Address:
    # TeleBox accepts either a bare IP or a CIDR and uses the IP part.
    value = value.strip().split("/", 1)[0]
    try:
        parts = value.split(".")
        if len(parts) != 4 or not all(part.isascii() and part.isdigit() and 1 <= len(part) <= 3 and int(part) <= 255 for part in parts):
            raise ValueError("invalid IPv4")
        ip = ipaddress.IPv4Address(".".join(str(int(part)) for part in parts))
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise QueryError("请输入有效的 IPv4 地址或 IPv4/CIDR") from exc
    return ip


def extract_bgp_ip(text: str) -> str | None:
    for pattern in (r"(\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}",
                    r"(\d{1,3}(?:\.\d{1,3}){3})"):
        match = re.search(pattern, text)
        if match:
            try:
                _ipv4(match.group(1))
                return match.group(1)
            except QueryError:
                continue
    return None


def _prefixes(ip: ipaddress.IPv4Address) -> list[str]:
    return [str(ipaddress.ip_network(f"{ip}/{mask}", strict=False)) for mask in (24, 23)]


def _fetch_bytes(url: str, limit: int) -> bytes:
    try:
        with urlopen(Request(url, headers=_HEADERS), timeout=15) as response:
            data = response.read(limit + 1)
            encoding = response.headers.get("Content-Encoding", "").lower()
    except HTTPError as exc:
        if exc.code == 404:
            return b""
        raise QueryError(f"bgp.tools 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise QueryError("无法连接 bgp.tools") from exc
    if len(data) > limit:
        raise QueryError("bgp.tools 返回的文件过大")
    try:
        if encoding == "gzip":
            data = gzip.decompress(data)
        elif encoding == "deflate":
            data = zlib.decompress(data)
    except (OSError, zlib.error) as exc:
        raise QueryError("bgp.tools 返回的压缩数据无效") from exc
    if len(data) > limit:
        raise QueryError("bgp.tools 返回的文件过大")
    return data


def _svg_to_png(svg: bytes) -> bytes:
    try:
        import cairosvg
    except ImportError as exc:
        raise QueryError("缺少 CairoSVG 依赖，无法生成 BGP 图片") from exc
    try:
        chart = cairosvg.svg2png(bytestring=svg, scale=300 / 72)
        if chart[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("SVG rasterizer did not return PNG")
        width, height = struct.unpack(">II", chart[16:24])
        if not width or not height:
            raise ValueError("empty graph")
        # The TeleBox photo has a wide, light background around the tall graph.
        # CairoSVG otherwise emits only a narrow, portrait chart on Telegram.
        side = 1800
        scale = min(side / height, side / width)
        drawn_width = round(width * scale)
        drawn_height = round(height * scale)
        x = (side - drawn_width) // 2
        y = (side - drawn_height) // 2
        encoded = base64.b64encode(chart).decode("ascii")
        framed = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}">'
            '<defs><linearGradient id="background">'
            '<stop offset="0%" stop-color="#f7f7f7"/>'
            '<stop offset="100%" stop-color="#e4f5fb"/>'
            '</linearGradient></defs>'
            f'<rect width="{side}" height="{side}" fill="url(#background)"/>'
            f'<rect x="{x}" y="{y}" width="{drawn_width}" height="{drawn_height}" fill="#ffffff"/>'
            f'<image x="{x}" y="{y}" width="{drawn_width}" height="{drawn_height}" '
            f'href="data:image/png;base64,{encoded}"/>'
            '</svg>'
        )
        return cairosvg.svg2png(bytestring=framed.encode("utf-8"))
    except Exception as exc:
        raise QueryError("BGP 路由图转换为 PNG 失败") from exc


async def fetch_bgp_image(target: str) -> BgpImage:
    ip = _ipv4(target)
    placeholder = None
    for prefix in _prefixes(ip):
        slug = prefix.replace("/", "_")
        url = (
            f"https://bgp.tools/pathimg/rt-{slug}"
            "?4c1db184-e649-4491-8b7f-06177bcb4f25&loggedin"
        )
        try:
            svg = await asyncio.to_thread(_fetch_bytes, url, _MAX_SVG)
        except QueryError:
            continue
        if not svg:
            continue
        text = svg.decode("utf-8", errors="replace")
        if "Not_Visible" in text and "in_DFZ" in text:
            placeholder = prefix
            continue
        png = await asyncio.to_thread(_svg_to_png, svg)
        return BgpImage(png=png, prefix=prefix)
    return BgpImage(png=None, prefix=placeholder or _prefixes(ip)[0],
                    placeholder=placeholder is not None)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        return unescape("".join(self.parts))


def _extract_dns(html: str) -> list[str]:
    parser = _TextParser()
    parser.feed(html)
    matches = re.findall(
        r"\b((?:\d{1,3}\.){3}\d{1,3})\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b",
        parser.text(),
    )
    records = matches
    counts: dict[str, int] = {}
    for _, domain in records:
        top = ".".join(domain.split(".")[-2:])
        counts[top] = counts.get(top, 0) + 1
    result = []
    grouped: dict[str, list[str]] = {}
    for ip, domain in records:
        grouped.setdefault(ip, []).append(domain)
    for ip, domains in grouped.items():
        for domain in domains:
            top = ".".join(domain.split(".")[-2:])
            if counts[top] <= 2:
                result.append(f"{ip}\t{domain}")
    return result


async def fetch_bgp_dns(target: str) -> tuple[str, list[str]]:
    ip = _ipv4(target)
    for prefix in _prefixes(ip):
        url = f"https://bgp.tools/prefix/{prefix}#dns"
        try:
            raw = await asyncio.to_thread(_fetch_bytes, url, _MAX_HTML)
        except QueryError:
            continue
        if not raw:
            continue
        lines = _extract_dns(raw.decode("utf-8", errors="replace"))
        if lines:
            return prefix, lines
    raise QueryError("未找到DNS记录")

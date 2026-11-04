"""TeleBox dig behavior and Telegram HTML output adapted to AstrBot."""

from __future__ import annotations

import asyncio
from html import escape
import json
import re
from urllib.request import Request, urlopen

from .queries import QueryError


def _validate_args(raw: str) -> tuple[list[str], bool]:
    args = raw.split()
    if not args:
        raise QueryError("未提供任何参数")
    for arg in args:
        if re.search(r"[;&|\x60$(){}!#<>'\"\\]", arg) or ".." in arg or "/" in arg:
            raise QueryError("查询参数包含非法字符，只允许域名、记录类型和 dig 标志")
    result = list(args)
    if not any(arg.startswith("+") or arg.startswith("-") for arg in result):
        result.append("+short")
    return result, "+short" in result


def _domain(args: list[str]) -> str | None:
    return next((arg for arg in args if not arg.startswith(("+", "-", "@")) and "/" not in arg), None)


def _is_ipv4(value: str) -> bool:
    octet = r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
    return re.fullmatch(rf"(?:{octet}\.){{3}}{octet}", value) is not None


async def _run_dig(args: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "dig", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise QueryError("系统未安装 dig 工具。请安装 bind-utils (Linux) 或 dnsutils (Ubuntu/Debian) 包。") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise QueryError("DNS 查询失败: dig 查询超时") from exc
    if stderr and not stdout:
        raise QueryError(f"DNS 查询失败: dig 命令执行失败: {stderr.decode(errors='replace').strip()}")
    if process.returncode and not stdout:
        raise QueryError(f"DNS 查询失败: dig 命令执行失败: {process.returncode}")
    return stdout.decode("utf-8", errors="replace").strip()


def _fetch_geo(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "TeleBox-DNS-Plugin/1.0"})
    with urlopen(request, timeout=3) as response:
        data = json.loads(response.read(1_000_001))
    return data if isinstance(data, dict) else {}


async def _location(ip: str) -> str:
    try:
        data = await asyncio.to_thread(_fetch_geo, f"https://api.ip.sb/geoip/{ip}")
        country = str(data.get("country") or "")
        region = str(data.get("region") or "")
        city = str(data.get("city") or "")
        asn = str(data.get("asn") or "")
        fields = []
        if country:
            fields.append(country)
        if region and region != country:
            fields.append(region)
        if city and city != region:
            fields.append(city)
        location = "-".join(fields)
        if location and asn:
            return f"{location}-{asn}"
        return location or asn or "未知"
    except Exception:
        try:
            data = await asyncio.to_thread(_fetch_geo, f"https://ipinfo.io/{ip}/json")
            if data.get("bogon"):
                return ""
            country = str(data.get("country") or "")
            region = str(data.get("region") or "")
            city = str(data.get("city") or "")
            org = str(data.get("org") or "")
            fields = []
            if city:
                fields.append(city)
            if region and region != city:
                fields.append(region)
            if country:
                fields.append(country)
            location = ",".join(fields)
            match = re.search(r"AS\d+", org)
            asn = match.group(0) if match else ""
            if location and asn:
                return f"{location}-{asn}"
            return location or asn or org or "未知"
        except Exception:
            return ""


async def _format_short(output: str, args: list[str]) -> str:
    if not output:
        return "❌ <b>解析失败</b>\n\n无记录或查询失败"
    lines = [line for line in output.split("\n") if line.strip()]
    domain = _domain(args)
    result = "🔍 <b>DNS 解析</b>"
    if domain:
        result += f" - <code>{escape(domain)}</code>"
    result += "\n"
    for line in lines[:3]:
        line = line.strip()
        if _is_ipv4(line):
            location = await _location(line)
            result += f"\n\n📍 <code>{escape(line)}</code>"
            if location:
                result += f"\n   <i>{escape(location)}</i>"
        else:
            result += f"\n\n🔗 <code>{escape(line)}</code>"
    if len(lines) > 3:
        result += f"\n\n<i>... 还有 {len(lines) - 3} 个结果未显示</i>"
    return result


def _parse_record(line: str) -> tuple[str, str, str, str] | None:
    parts = line.strip().split()
    if len(parts) < 4:
        return None
    domain = parts[0].removesuffix(".")
    if "IN" in parts:
        index = parts.index("IN")
        if index + 2 < len(parts):
            ttl = parts[index - 1] if index > 1 else ""
            return domain, parts[index + 1], " ".join(parts[index + 2:]), ttl
    return domain, parts[-2], parts[-1], ""


def _answer_lines(output: str) -> list[str]:
    lines = output.split("\n")
    answer = []
    in_answer = False
    for line in lines:
        if ";; ANSWER SECTION:" in line:
            in_answer = True
            continue
        if in_answer:
            if line.startswith(";;") or not line.strip():
                break
            if not line.startswith(";"):
                answer.append(line.strip())
    if answer:
        return answer
    return [line.strip() for line in lines
            if (line.strip() and not line.strip().startswith((";;", ";")) and "\t" in line)
            or len(line.strip().split()) >= 4]


async def _format_detailed(output: str, args: list[str]) -> str:
    if not output:
        return "❌ <b>解析失败</b>\n\n无记录或查询失败"
    answers = _answer_lines(output)
    domain = _domain(args)
    result = "🔍 <b>DNS 解析</b>"
    if domain:
        result += f" - <code>{escape(domain)}</code>"
    result += "\n"
    if not answers:
        return result + "\n\n❌ 无记录"
    icons = {"A": "📍", "AAAA": "📍", "CNAME": "🔗", "MX": "📧", "NS": "🌐", "TXT": "📝"}
    for line in answers[:3]:
        record = _parse_record(line)
        if record:
            name, kind, value, ttl = record
            icon = icons.get(kind, "📋")
            if name and "$" not in name:
                result += f"\n\n{icon} <b>{escape(name)}</b> → <b>{escape(kind)}</b>: <code>{escape(value)}</code>"
            else:
                result += f"\n\n{icon} <b>{escape(kind)}</b>: <code>{escape(value)}</code>"
            if ttl:
                result += f"\n   <i>TTL: {escape(ttl)}s</i>"
            if _is_ipv4(value):
                location = await _location(value)
                if location:
                    result += f"\n   <i>{escape(location)}</i>"
        else:
            last = line.split()[-1] if line.split() else ""
            if _is_ipv4(last):
                location = await _location(last)
                result += f"\n\n📍 <code>{escape(last)}</code>"
                if location:
                    result += f"\n   <i>{escape(location)}</i>"
            else:
                result += f"\n\n🔗 <code>{escape(last)}</code>"
    if len(answers) > 3:
        result += f"\n\n<i>... 还有 {len(answers) - 3} 个结果未显示</i>"
    return result


async def lookup_dig_native(raw: str) -> str:
    try:
        args, short = _validate_args(raw)
        output = await _run_dig(args)
        return await (_format_short(output, raw.split()) if short else _format_detailed(output, raw.split()))
    except QueryError as exc:
        if str(exc).startswith(("系统未安装 dig 工具", "DNS 查询失败:")):
            raise
        raise QueryError(f"DNS 查询失败: {exc}") from exc

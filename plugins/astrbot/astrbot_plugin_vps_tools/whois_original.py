"""TeleBox WHOIS lookup behavior adapted to AstrBot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .queries import QueryError


def clean_domain(value: str) -> str:
    value = re.sub(r"^https?://", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"^www\.", "", value, flags=re.IGNORECASE).split("/", 1)[0]


def normalize_domain(value: str) -> str:
    value = clean_domain(value)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]?(?:\.[a-zA-Z]{2,})+", value):
        raise QueryError("域名格式无效")
    return value


def find_domain(text: str) -> str | None:
    matches = re.findall(r"(?:https?://)?(?:www\.)?([a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]?(?:\.[a-zA-Z]{2,})+)", text, flags=re.IGNORECASE)
    for match in matches:
        try:
            return normalize_domain(match)
        except QueryError:
            continue
    return None


def _fetch_namebeta(domain: str) -> str:
    url = "https://namebeta.com/api/search/check?" + urlencode({"query": domain})
    request = Request(url, headers={"User-Agent": "TeleBox/1.0"})
    with urlopen(request, timeout=10) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise QueryError("WHOIS 原始数据过大")
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, TypeError):
            continue
        if event.get("type") == "check":
            whois = (event.get("data") or {}).get("whois") or {}
            if isinstance(whois, dict) and whois.get("whois"):
                return str(whois["whois"])
    raise QueryError("未查询到 WHOIS 数据")


def _field(raw: str, *names: str) -> str | None:
    for name in names:
        match = re.search(rf"{re.escape(name)}:\s*(.+)", raw, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _from_raw(domain: str, raw: str) -> dict:
    raw = raw.split("For more information", 1)[0].strip()
    nameservers = re.findall(r"(?:Name Server|nserver|NS):\s*(.+)", raw, flags=re.IGNORECASE)
    return {
        "domain": domain,
        "registrar": _field(raw, "Registrar"),
        "createdDate": _field(raw, "Creation Date"),
        "expiryDate": _field(raw, "Registry Expiry Date"),
        "updatedDate": _field(raw, "Updated Date"),
        "status": _field(raw, "Domain Status"),
        "nameServers": nameservers,
        "rawData": raw,
        "queryTime": datetime.now(timezone.utc).isoformat(),
    }


async def query_whois(domain: str) -> dict:
    raw = await asyncio.to_thread(_fetch_namebeta, domain)
    return _from_raw(domain, raw)


async def query_whois_raw(domain: str) -> dict:
    """The source batch command caches only the unmodified SSE WHOIS text."""
    raw = await asyncio.to_thread(_fetch_namebeta, domain)
    return {"domain": domain, "rawData": raw,
            "queryTime": datetime.now(timezone.utc).isoformat()}


def _expiry_note(value: str | None) -> str:
    if not value:
        return ""
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        days = int((date - datetime.now(timezone.utc)).total_seconds() // 86400)
    except ValueError:
        return ""
    if days < 0:
        return " ⚠️ 已过期"
    if days < 30:
        return f" ⚠️ {days} 天后过期"
    if days < 90:
        return f"（{days} 天后过期）"
    return ""


def format_history(records: list[dict], total: int, cache_count: int) -> str:
    if not records:
        return "📭 暂无查询历史"
    lines = [f"📜 查询历史（最近 {len(records)} 条）"]
    for index, record in enumerate(records, 1):
        date = str(record.get("queryTime", ""))[:16].replace("T", " ")
        expiry = _expiry_note(record.get("expiryDate"))
        lines.append(f"{index}. {record.get('domain', '?')}  {date}{expiry}")
    lines.append(f"总查询次数：{total}；缓存域名数：{cache_count}；缓存时长：24 小时")
    return "\n".join(lines)

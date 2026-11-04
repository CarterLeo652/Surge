"""TeleBox IP lookup behavior, with only the command name adapted for AstrBot."""

from __future__ import annotations

import asyncio
import json
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


_FIELDS = "status,message,country,regionName,city,isp,org,as,query,timezone,proxy,hosting"


def _fetch_ip_info(query: str) -> dict:
    if not query.strip():
        return {"status": "fail", "message": "请提供有效的IP地址或域名"}
    url = (f"http://ip-api.com/json/{quote(query.strip(), safe='')}?"
           f"lang=zh-CN&fields={_FIELDS}")
    request = Request(url, headers={"User-Agent": "TeleBox-IP-Plugin/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                return {"status": "fail", "message": f"API请求失败，HTTP状态码: {response.status}"}
            data = json.loads(response.read(1_000_001))
        if not isinstance(data, dict):
            return {"status": "fail", "message": "网络请求失败"}
        if data.get("status") == "fail":
            return {"status": "fail", "message": data.get("message") or "查询失败，请检查IP地址或域名是否正确"}
        return data
    except Exception as exc:
        detail = str(exc).lower()
        if "timed out" in detail or "timeout" in detail:
            message = "请求超时，请稍后重试"
        elif "name or service not known" in detail or "getaddrinfo" in detail or "nodename nor servname" in detail:
            message = "DNS解析失败，请检查网络连接"
        elif "connection refused" in detail:
            message = "连接被拒绝，请稍后重试"
        else:
            message = "网络请求失败"
        return {"status": "fail", "message": message}


async def query_ip(query: str) -> dict:
    return await asyncio.to_thread(_fetch_ip_info, query)

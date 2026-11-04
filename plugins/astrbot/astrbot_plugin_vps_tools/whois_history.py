"""Persistent 24-hour WHOIS cache and recent query history."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile


class WhoisHistory:
    def __init__(self, directory: Path):
        self.path = directory / "whois_data.json"
        self.lock = asyncio.Lock()
        self.data: dict = {"history": [], "cache": {}}

    async def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = await asyncio.to_thread(self._read)
            if isinstance(data, dict) and isinstance(data.get("history"), list) and isinstance(data.get("cache"), dict):
                # Earlier AstrBot builds cached RDAP fallback results. TeleBox's
                # WHOIS command uses only namebeta; do not serve those as WHOIS.
                data["cache"] = {key: value for key, value in data["cache"].items()
                                 if isinstance(value, dict) and value.get("source") != "RDAP"}
                data["history"] = [value for value in data["history"]
                                   if isinstance(value, dict) and value.get("source") != "RDAP"]
                self.data = data
        except (OSError, ValueError):
            # A broken history file must not prevent the query commands from loading.
            pass

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self.data, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".whois-", suffix=".tmp", delete=False) as file:
            file.write(raw)
            temp_path = Path(file.name)
        temp_path.replace(self.path)

    async def cached(self, domain: str) -> dict | None:
        async with self.lock:
            record = self.data["cache"].get(domain.lower())
            if not isinstance(record, dict):
                return None
            try:
                queried = datetime.fromisoformat(record["queryTime"].replace("Z", "+00:00"))
                if queried.tzinfo is None:
                    queried = queried.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                queried = datetime.min.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - queried > timedelta(hours=24):
                del self.data["cache"][domain.lower()]
                await asyncio.to_thread(self._write)
                return None
            return dict(record)

    async def save(self, record: dict) -> None:
        async with self.lock:
            self.data["cache"][record["domain"].lower()] = record
            self.data["history"].insert(0, record)
            self.data["history"] = self.data["history"][:100]
            await asyncio.to_thread(self._write)

    async def history(self) -> tuple[list[dict], int, int]:
        async with self.lock:
            return (list(self.data["history"][:20]), len(self.data["history"]),
                    len(self.data["cache"]))

    async def clear(self) -> tuple[int, int]:
        async with self.lock:
            counts = (len(self.data["history"]), len(self.data["cache"]))
            self.data = {"history": [], "cache": {}}
            await asyncio.to_thread(self._write)
            return counts

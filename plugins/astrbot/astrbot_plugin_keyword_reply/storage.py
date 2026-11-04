"""Atomic on-disk storage for administrator-managed rules and media."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .rules import MAX_RULES, validate_rule


class RuleStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.data_dir / "media"
        self.media_dir.mkdir(exist_ok=True)
        self.path = self.data_dir / "rules.json"
        self.rules: list[dict] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or len(data) > MAX_RULES:
                raise ValueError("规则文件格式无效")
            self.rules = [validate_rule(item) for item in data]
            ids = [item["id"] for item in self.rules]
            if len(ids) != len(set(ids)):
                raise ValueError("规则 ID 重复")

    def media_ids(self) -> set[str]:
        return {p.stem for p in self.media_dir.iterdir() if p.is_file()}

    def media_path(self, media_id: str) -> Path | None:
        for p in self.media_dir.glob(f"{media_id}.*"):
            if p.is_file() and p.stem == media_id:
                return p
        return None

    def replace(self, proposed: list) -> list[dict]:
        if not isinstance(proposed, list) or len(proposed) > MAX_RULES:
            raise ValueError(f"最多支持 {MAX_RULES} 条规则")
        allowed = self.media_ids()
        clean = [validate_rule(item, allowed_media=allowed) for item in proposed]
        ids = [item["id"] for item in clean]
        if len(ids) != len(set(ids)):
            raise ValueError("规则 ID 重复")
        payload = json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=".rules-", dir=self.data_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        self.rules = clean
        return clean

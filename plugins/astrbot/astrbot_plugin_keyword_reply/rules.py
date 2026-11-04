"""Validated keyword rules and matching, independent of AstrBot."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any


MAX_RULES = 500
MAX_TEMPLATES = 20
MAX_TEXT = 4096
TARGETS = {"text", "filename", "sticker_emoji"}
MODES = {"contains", "exact", "regex"}
MEDIA = {"photo", "video", "animation", "audio", "voice", "document"}


@dataclass(frozen=True)
class MessageFacts:
    text: str = ""
    filename: str = ""
    sticker_emoji: str = ""
    forwarded: bool = False
    topic_id: str = ""


def _string(value: Any, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是文本")
    value = value.strip()
    if (not empty and not value) or len(value) > limit:
        raise ValueError(f"{field} 长度无效（最多 {limit} 字符）")
    return value


def _integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} 必须在 {low} 到 {high} 之间")
    return value


def validate_rule(raw: Any, *, allowed_media: set[str] | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("规则必须是对象")
    rule_id = raw.get("id") or str(uuid.uuid4())
    try:
        rule_id = str(uuid.UUID(rule_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("规则 ID 无效") from exc
    group_id = _string(raw.get("group_id"), "群 ID", 32)
    if not re.fullmatch(r"-?\d+", group_id):
        raise ValueError("群 ID 必须是 Telegram 数字 ID")
    topic_id = _string(raw.get("topic_id", ""), "话题 ID", 32, empty=True)
    if topic_id and not re.fullmatch(r"\d+", topic_id):
        raise ValueError("话题 ID 必须是正整数")
    pattern = _string(raw.get("pattern"), "关键词", 256)
    mode = raw.get("mode", "contains")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("匹配方式无效")
    ignore_case = raw.get("ignore_case", True)
    if not isinstance(ignore_case, bool):
        raise ValueError("忽略大小写必须是布尔值")
    if mode == "regex":
        try:
            re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}") from exc
    targets = raw.get("targets", ["text"])
    if not isinstance(targets, list) or not targets or any(not isinstance(x, str) or x not in TARGETS for x in targets):
        raise ValueError("匹配内容无效")
    targets = list(dict.fromkeys(targets))
    templates = raw.get("templates", [])
    if not isinstance(templates, list) or not 1 <= len(templates) <= MAX_TEMPLATES:
        raise ValueError(f"每条规则需要 1 到 {MAX_TEMPLATES} 个回复模板")
    clean_templates = []
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("回复模板格式无效")
        kind = item.get("kind", "text")
        if kind == "text":
            clean_templates.append({"kind": "text", "text": _string(item.get("text"), "回复文字", MAX_TEXT)})
        elif isinstance(kind, str) and kind in MEDIA:
            media_id = _string(item.get("media_id"), "媒体 ID", 64)
            if not re.fullmatch(r"[a-f0-9]{32}", media_id):
                raise ValueError("媒体 ID 无效")
            if allowed_media is not None and media_id not in allowed_media:
                raise ValueError("媒体文件不存在")
            caption = _string(item.get("caption", ""), "媒体说明", 1024, empty=True)
            clean_templates.append({"kind": kind, "media_id": media_id, "caption": caption})
        else:
            raise ValueError("回复类型无效")
    response_mode = raw.get("response_mode", "random")
    if not isinstance(response_mode, str) or response_mode not in {"random", "all"}:
        raise ValueError("回复选择方式无效")
    enabled = raw.get("enabled", True)
    ignore_forward = raw.get("ignore_forward", False)
    reply_to_source = raw.get("reply_to_source", True)
    delete_source = raw.get("delete_source", False)
    for name, value in (("启用", enabled), ("忽略转发", ignore_forward),
                        ("引用原消息", reply_to_source), ("删除原消息", delete_source)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} 必须是布尔值")
    cooldown = _integer(raw.get("cooldown_seconds", 0), "冷却秒数", 0, 86400)
    priority = _integer(raw.get("priority", 0), "优先级", -100, 100)
    return {
        "id": rule_id, "group_id": group_id, "topic_id": topic_id,
        "pattern": pattern, "mode": mode, "targets": targets,
        "ignore_case": ignore_case, "ignore_forward": ignore_forward,
        "enabled": enabled, "templates": clean_templates,
        "response_mode": response_mode, "reply_to_source": reply_to_source,
        "delete_source": delete_source, "cooldown_seconds": cooldown,
        "priority": priority,
    }


def matches(rule: dict, facts: MessageFacts) -> bool:
    if not rule["enabled"] or (rule["ignore_forward"] and facts.forwarded):
        return False
    if rule["topic_id"] and rule["topic_id"] != facts.topic_id:
        return False
    flags = re.IGNORECASE if rule["ignore_case"] else 0
    needle = rule["pattern"]
    values = {
        "text": facts.text, "filename": facts.filename,
        "sticker_emoji": facts.sticker_emoji,
    }
    for target in rule["targets"]:
        value = values[target]
        if not value:
            continue
        if rule["mode"] == "regex":
            if re.search(needle, value[:4096], flags):
                return True
        else:
            a, b = (value.casefold(), needle.casefold()) if rule["ignore_case"] else (value, needle)
            if (rule["mode"] == "exact" and a == b) or (rule["mode"] == "contains" and b in a):
                return True
    return False

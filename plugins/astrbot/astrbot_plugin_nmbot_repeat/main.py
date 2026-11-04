"""AstrBot adapter for a nmBot-style group repeater on Telegram."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .repeater import RepeatTracker


_LINK = re.compile(r"(?:https?://|www\.|t\.me/)", re.IGNORECASE)
_MEDIA_FIELDS = (
    "animation",
    "audio",
    "document",
    "sticker",
    "video",
    "video_note",
    "voice",
)


def _media_identity(message: object) -> tuple[str, str] | None:
    photos = getattr(message, "photo", None)
    if photos:
        photo = photos[-1]
        return "photo", str(getattr(photo, "file_unique_id", ""))
    for kind in _MEDIA_FIELDS:
        media = getattr(message, kind, None)
        if media is not None:
            return kind, str(getattr(media, "file_unique_id", ""))
    return None


def message_key(message: object) -> str | None:
    """Return a stable identity for copyable user messages, or None."""
    content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    content = unicodedata.normalize("NFC", content).strip()
    media = _media_identity(message)
    if media is not None and not media[1]:
        return None
    if not content and media is None:
        return None

    entities = getattr(message, "entities", None) or getattr(
        message, "caption_entities", None
    ) or ()
    # Ordinary formatting does not change visible content. Hidden links and
    # custom emoji IDs do, so include them in the equality check.
    semantic_entities = sorted(
        (
            getattr(entity, "offset", 0),
            getattr(entity, "type", ""),
            getattr(entity, "custom_emoji_id", None),
            getattr(entity, "url", None),
        )
        for entity in entities
        if getattr(entity, "type", None) in {"custom_emoji", "text_link"}
    )
    # A forward and an ordinary message with the same visible content form
    # the same repeat run. Forward attribution only affects how we send it.
    data = (content, media, semantic_entities)
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def contains_link(message: object) -> bool:
    content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    if _LINK.search(content):
        return True
    entities = getattr(message, "entities", None) or getattr(
        message, "caption_entities", None
    ) or ()
    return any(getattr(entity, "type", None) in {"url", "text_link"} for entity in entities)


class NMBotRepeat(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.tracker = RepeatTracker()
        self._last_prune = 0.0
        logger.info("欢乐复读 v1.0.4 已启动")

    def _trace(self, group: str, message: object, reason: str) -> None:
        if not self.config.get("debug_logging", False):
            return
        content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        logger.info(
            f"[欢乐复读诊断] group={group} "
            f"message_id={getattr(message, 'message_id', '?')} "
            f"sender={getattr(getattr(message, 'from_user', None), 'id', '?')} "
            f"text={content[:80]!r} reason={reason}"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enabled", True):
            return
        if getattr(event.platform_meta, "name", "") != "telegram":
            return

        group = str(event.message_obj.group_id or "")
        if not group:
            return
        chat_id = group.split("#", 1)[0]
        enabled_groups = {str(value) for value in self.config.get("enabled_groups", [])}
        if enabled_groups and chat_id not in enabled_groups and group not in enabled_groups:
            self.tracker.reset(group)
            self._trace(group, getattr(event.message_obj.raw_message, "message", None), "群不在启用列表")
            return
        disabled_groups = {str(value) for value in self.config.get("disabled_groups", [])}
        if chat_id in disabled_groups or group in disabled_groups:
            self.tracker.reset(group)
            self._trace(group, getattr(event.message_obj.raw_message, "message", None), "群在禁用列表")
            return

        update = event.message_obj.raw_message
        message = getattr(update, "message", None)
        if message is None or getattr(message, "chat", None) is None:
            self.tracker.reset(group)
            return
        sender = getattr(message, "from_user", None)
        if sender is None:
            self.tracker.reset(group)
            self._trace(group, message, "缺少发送者")
            return
        # The bot's own copy must not break the run or create a loop.
        bot = getattr(event, "client", None)
        if bot is None:
            try:
                bot = message.get_bot()
            except (AttributeError, RuntimeError):
                self.tracker.reset(group)
                self._trace(group, message, "无法获取 Telegram 客户端")
                return
        if str(sender.id) == str(bot.id):
            return
        if getattr(sender, "is_bot", False):
            self.tracker.reset(group)
            self._trace(group, message, "其他机器人消息")
            return

        content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if content.lstrip().startswith("/") or (
            self.config.get("block_links", True) and contains_link(message)
        ):
            self.tracker.reset(group)
            self._trace(group, message, "命令或链接被过滤")
            return
        key = message_key(message)
        if key is None:
            self.tracker.reset(group)
            self._trace(group, message, "消息类型未支持")
            return

        now = time.monotonic()
        window = max(1, int(self.config.get("window_seconds", 120)))
        if now - self._last_prune > window:
            self.tracker.prune(now, window)
            self._last_prune = now

        previous_run = self.tracker._runs.get(group)
        previous_id = previous_run.last_message_id if previous_run else "-"
        should_repeat = self.tracker.observe(
            group,
            key,
            str(sender.id),
            str(message.message_id),
            now,
            min_members=max(2, int(self.config.get("min_members", 2))),
            window_seconds=window,
            # Topic IDs are shared across the whole group, so messages in
            # other topics can create harmless gaps in one topic's sequence.
            require_adjacent_ids="#" not in group,
        )
        run = self.tracker._runs[group]
        self._trace(
            group,
            message,
            f"key={key[:10]} 前ID={previous_id} 成员数={len(run.members)} "
            f"门槛={max(2, int(self.config.get('min_members', 2)))} "
            f"{'触发' if should_repeat else '等待'}",
        )
        if not should_repeat:
            return

        kwargs = {
            "chat_id": message.chat.id,
            "from_chat_id": message.chat.id,
            "message_id": message.message_id,
        }
        thread_id = getattr(message, "message_thread_id", None)
        if getattr(message, "is_topic_message", False) and thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            if getattr(message, "forward_origin", None) is not None:
                # Telegram preserves forwarding attribution on this path.
                await bot.forward_message(**kwargs)
            else:
                # Telegram copy preserves original text entities and media.
                await bot.copy_message(**kwargs)
            self._trace(group, message, "发送成功")
        except Exception as exc:
            self.tracker.send_failed(group, key)
            logger.warning(f"欢乐复读发送失败（群 {group}）：{exc}")

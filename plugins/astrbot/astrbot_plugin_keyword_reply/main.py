"""Keyword replies for AstrBot's Telegram adapter."""

from __future__ import annotations

import asyncio
import mimetypes
import random
import time
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .rules import MessageFacts, matches
from .storage import RuleStore


PLUGIN_NAME = "astrbot_plugin_keyword_reply"
MAX_UPLOAD = 20 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mp3", ".ogg", ".wav", ".pdf"}


def message_facts(message: object) -> MessageFacts:
    media = (getattr(message, "document", None) or getattr(message, "audio", None)
             or getattr(message, "video", None))
    sticker = getattr(message, "sticker", None)
    return MessageFacts(
        text=(getattr(message, "text", None) or getattr(message, "caption", None) or ""),
        filename=getattr(media, "file_name", "") or "",
        sticker_emoji=getattr(sticker, "emoji", "") or "",
        forwarded=getattr(message, "forward_origin", None) is not None,
        topic_id=str(getattr(message, "message_thread_id", "") or ""),
    )


class KeywordReply(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = RuleStore(data_dir)
        self._save_lock = asyncio.Lock()
        self._last_sent: dict[tuple[str, str], float] = {}
        context.register_web_api(f"/{PLUGIN_NAME}/rules", self.get_rules, ["GET"], "列出关键词规则")
        context.register_web_api(f"/{PLUGIN_NAME}/rules/save", self.save_rules, ["POST"], "保存关键词规则")
        context.register_web_api(f"/{PLUGIN_NAME}/media/upload", self.upload_media, ["POST"], "上传回复媒体")
        logger.info(f"关键词回复已加载：{len(self.store.rules)} 条规则")

    async def get_rules(self):
        return json_response({"rules": self.store.rules})

    async def save_rules(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式无效", status_code=400)
        async with self._save_lock:
            try:
                rules = self.store.replace(payload.get("rules"))
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        return json_response({"rules": rules})

    async def upload_media(self):
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("请选择文件", status_code=400)
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            return error_response("仅支持图片、视频、音频和 PDF", status_code=400)
        media_id = uuid.uuid4().hex
        target = self.store.media_dir / f"{media_id}{ext}"
        try:
            await upload.save(target)
            if target.stat().st_size > MAX_UPLOAD or target.stat().st_size == 0:
                target.unlink(missing_ok=True)
                return error_response("文件须在 1 字节到 20 MB 之间", status_code=400)
        except Exception:
            target.unlink(missing_ok=True)
            logger.exception("关键词回复媒体上传失败")
            return error_response("上传失败", status_code=500)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            kind = "photo"
        elif ext == ".gif":
            kind = "animation"
        elif ext == ".mp4":
            kind = "video"
        elif ext in {".mp3", ".wav"}:
            kind = "audio"
        elif ext == ".ogg":
            kind = "voice"
        else:
            kind = "document"
        return json_response({"media_id": media_id, "kind": kind, "filename": upload.filename, "mime": mime})

    async def _send_template(self, bot, message, rule: dict, template: dict) -> None:
        options = {"chat_id": message.chat.id}
        if getattr(message, "is_topic_message", False) and getattr(message, "message_thread_id", None):
            options["message_thread_id"] = message.message_thread_id
        if rule["reply_to_source"]:
            options["reply_to_message_id"] = message.message_id
        kind = template["kind"]
        if kind == "text":
            await bot.send_message(text=template["text"], **options)
            return
        path = self.store.media_path(template["media_id"])
        if path is None:
            raise FileNotFoundError("回复媒体已丢失")
        with path.open("rb") as stream:
            if kind == "photo":
                await bot.send_photo(photo=stream, caption=template["caption"] or None, **options)
            elif kind == "video":
                await bot.send_video(video=stream, caption=template["caption"] or None, **options)
            elif kind == "animation":
                await bot.send_animation(animation=stream, caption=template["caption"] or None, **options)
            elif kind == "audio":
                await bot.send_audio(audio=stream, caption=template["caption"] or None, **options)
            elif kind == "voice":
                await bot.send_voice(voice=stream, caption=template["caption"] or None, **options)
            elif kind == "document":
                await bot.send_document(document=stream, caption=template["caption"] or None, **options)
            else:
                raise ValueError("不支持的回复类型")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enabled", True):
            return
        if getattr(event.platform_meta, "name", "") != "telegram":
            return
        update = event.message_obj.raw_message
        message = getattr(update, "message", None)
        if message is None or getattr(message, "chat", None) is None:
            return
        sender = getattr(message, "from_user", None)
        if sender is None or getattr(sender, "is_bot", False):
            return
        facts = message_facts(message)
        if facts.text.lstrip().startswith("/"):
            return
        bot = getattr(event, "client", None)
        if bot is None:
            try:
                bot = message.get_bot()
            except (AttributeError, RuntimeError):
                logger.warning("关键词回复：无法获取 Telegram 客户端")
                return
        if str(sender.id) == str(bot.id):
            return
        group_id = str(message.chat.id)
        now = time.monotonic()
        # Deterministic order: higher priority first, then the editor's order.
        rules = sorted(self.store.rules, key=lambda rule: -rule["priority"])
        for rule in rules:
            if rule["group_id"] != group_id or not matches(rule, facts):
                continue
            cooldown_key = (group_id, rule["id"])
            if now - self._last_sent.get(cooldown_key, -float("inf")) < rule["cooldown_seconds"]:
                continue
            templates = (random.sample(rule["templates"], k=1) if rule["response_mode"] == "random"
                         else rule["templates"])
            sent_any = False
            try:
                for template in templates:
                    await self._send_template(bot, message, rule, template)
                    sent_any = True
            except Exception as exc:
                logger.warning(f"关键词回复发送失败（规则 {rule['id']}，群 {group_id}）：{exc}")
                if not sent_any:
                    return
            if rule["delete_source"]:
                try:
                    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
                except Exception as exc:
                    logger.warning(f"关键词回复删除原消息失败（规则 {rule['id']}，群 {group_id}）：{exc}")
            self._last_sent[cooldown_key] = now
            event.stop_event()
            return

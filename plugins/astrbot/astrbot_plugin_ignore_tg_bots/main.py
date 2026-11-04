"""Ignore messages whose Telegram sender is a bot."""

from sys import maxsize

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star


class IgnoreTelegramBots(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    async def ignore_bot_messages(self, event: AstrMessageEvent):
        """Stop Telegram bot messages before other handlers and the LLM run."""
        update = getattr(event.message_obj, "raw_message", None)
        message = getattr(update, "message", None)
        sender = getattr(message, "from_user", None)

        if sender is not None and getattr(sender, "is_bot", False) is True:
            event.stop_event()

    @filter.on_decorating_result()
    async def skip_invisible_telegram_reply(self, event: AstrMessageEvent):
        """Drop Telegram replies containing only U+200C, which Telegram rejects."""
        if event.get_platform_name() != "telegram":
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # A Reply/At header cannot be sent without message content. Preserve
        # every response containing visible text or another content component.
        if not all(isinstance(part, (Plain, Reply, At)) for part in result.chain):
            return

        text_parts = [part.text for part in result.chain if isinstance(part, Plain)]
        if text_parts and not "".join(text_parts).replace("\u200c", "").strip():
            result.chain.clear()

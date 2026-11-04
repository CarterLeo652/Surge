"""Schedule deletion of this plugin's own Telegram messages."""

from __future__ import annotations

import asyncio
import logging


logger = logging.getLogger(__name__)


class AutoDelete:
    def __init__(self, config):
        self.config = config
        self.tasks: set[asyncio.Task] = set()

    def _delay(self) -> int | None:
        if self.config.get("auto_delete") is not True:
            return None
        try:
            delay = int(self.config.get("auto_delete_seconds", 300))
        except (TypeError, ValueError):
            delay = 300
        return min(max(delay, 1), 86400)

    def schedule(self, event, message) -> None:
        delay = self._delay()
        if delay is None or message is None:
            return
        message_id = getattr(message, "message_id", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is None or message_id is None:
            logger.warning("VPS 自动删除：无法取得已发送消息的 chat_id 或 message_id")
            return
        task = asyncio.create_task(
            self._delete_after(event.client, chat_id, message_id, delay)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    async def _delete_after(client, chat_id, message_id, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await client.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.warning("VPS 自动删除失败（chat=%s, message=%s）：%s",
                           chat_id, message_id, exc)

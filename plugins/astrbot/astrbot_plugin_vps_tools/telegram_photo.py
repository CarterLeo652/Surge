"""Send a single Telegram photo with its caption, which AstrBot's Image chain cannot do."""

from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO


def telegram_destination(event) -> tuple[str, int | None] | None:
    if event.get_platform_name() != "telegram" or not hasattr(event, "client"):
        return None
    destination = event.get_group_id() or event.get_sender_id()
    if not destination:
        return None
    chat_id, _, thread_id = str(destination).partition("#")
    return chat_id, int(thread_id) if thread_id else None


async def send_bgp_photo(event, png: bytes, target: str, prefix: str,
                         *, on_sent=None) -> bool:
    """Return False for other adapters so they can use the generic message chain."""
    destination = telegram_destination(event)
    if destination is None:
        return False
    chat_id, thread_id = destination
    now = datetime.now().astimezone()
    caption = (
        "🌐 <b>BGP路由图</b>\n\n"
        f"<code>{escape(target)}</code>\n"
        f"<i>使用前缀: {escape(prefix)}</i>\n\n"
        f"⏰ <i>{now.year}/{now.month}/{now.day} {now:%H:%M:%S}</i>"
    )
    photo = BytesIO(png)
    photo.name = "bgp-route.png"
    options = {"chat_id": chat_id, "photo": photo, "caption": caption,
               "parse_mode": "HTML"}
    if thread_id is not None:
        options["message_thread_id"] = thread_id
    sent = await event.client.send_photo(**options)
    if on_sent is not None:
        on_sent(event, sent)
    event.clear_result()
    event.stop_event()
    return True

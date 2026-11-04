import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = SimpleNamespace(warning=lambda *_: None, info=lambda *_: None)
event_api = types.ModuleType("astrbot.api.event")
event_api.AstrMessageEvent = object
event_api.filter = SimpleNamespace(
    EventMessageType=SimpleNamespace(GROUP_MESSAGE="group"),
    event_message_type=lambda *_, **__: lambda fn: fn,
)
star_api = types.ModuleType("astrbot.api.star")
star_api.Context = object
star_api.Star = type("Star", (), {"__init__": lambda self, context: None})
sys.modules.update(
    {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
    }
)

from astrbot_plugin_nmbot_repeat.main import NMBotRepeat, message_key  # noqa: E402
from astrbot_plugin_nmbot_repeat.repeater import RepeatTracker  # noqa: E402


class FakeBot:
    id = 999

    def __init__(self):
        self.copied = []
        self.forwarded = []

    async def copy_message(self, **kwargs):
        self.copied.append(kwargs)

    async def forward_message(self, **kwargs):
        self.forwarded.append(kwargs)


def make_event(bot, group, sender, message_id, text="hello", **kwargs):
    topic = int(group.split("#", 1)[1]) if "#" in group else None
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=sender, is_bot=sender == 999),
        message_id=message_id,
        text=text,
        caption=None,
        entities=(),
        caption_entities=(),
        photo=None,
        forward_origin=None,
        is_topic_message=topic is not None,
        message_thread_id=topic,
        get_bot=lambda: bot,
        **kwargs,
    )
    return SimpleNamespace(
        platform_meta=SimpleNamespace(name="telegram"),
        message_obj=SimpleNamespace(
            group_id=group, raw_message=SimpleNamespace(message=message)
        ),
    )


class RepeatTrackerTests(unittest.TestCase):
    def test_distinct_members_and_one_copy_per_run(self):
        tracker = RepeatTracker()
        observe = lambda member, mid, now: tracker.observe(
            "g", "same", member, mid, now, min_members=2, window_seconds=120
        )
        self.assertFalse(observe("a", "1", 0))
        self.assertFalse(observe("a", "2", 1))
        self.assertTrue(observe("b", "3", 2))
        self.assertFalse(observe("c", "4", 3))

    def test_time_window_and_topic_isolation(self):
        tracker = RepeatTracker()
        self.assertFalse(tracker.observe("g#1", "x", "a", "1", 0, min_members=2, window_seconds=120))
        self.assertFalse(tracker.observe("g#2", "x", "b", "2", 1, min_members=2, window_seconds=120))
        self.assertFalse(tracker.observe("g#1", "x", "b", "3", 121, min_members=2, window_seconds=120))
        self.assertTrue(tracker.observe("g#1", "x", "a", "4", 122, min_members=2, window_seconds=120))


class PluginTests(unittest.TestCase):
    def test_repeats_and_resets_on_intervening_message(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123", 1, 1))
            await plugin.on_group_message(make_event(bot, "-100123", 2, 2))
            await plugin.on_group_message(make_event(bot, "-100123", 3, 3))
            await plugin.on_group_message(make_event(bot, "-100123", 1, 4, text="different"))
            await plugin.on_group_message(make_event(bot, "-100123", 1, 5))
            await plugin.on_group_message(make_event(bot, "-100123", 2, 6))

        asyncio.run(run())
        self.assertEqual([item["message_id"] for item in bot.copied], [2, 6])

    def test_topic_copy_and_link_blocking(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123#5", 1, 1, text="https://example.com"))
            await plugin.on_group_message(make_event(bot, "-100123#5", 2, 2, text="https://example.com"))
            await plugin.on_group_message(make_event(bot, "-100123#5", 1, 3))
            await plugin.on_group_message(make_event(bot, "-100123#5", 2, 4))

        asyncio.run(run())
        self.assertEqual(len(bot.copied), 1)
        self.assertEqual(bot.copied[0]["message_thread_id"], 5)

    def test_media_key_is_stable_across_users(self):
        photo = SimpleNamespace(file_unique_id="same-photo")
        first = SimpleNamespace(text=None, caption="hi", photo=[photo], entities=(), caption_entities=(), forward_origin=None)
        second = SimpleNamespace(text=None, caption="hi", photo=[photo], entities=(), caption_entities=(), forward_origin=None)
        self.assertEqual(message_key(first), message_key(second))

    def test_reply_then_plain_message_with_same_visible_text(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            first = make_event(bot, "-100123", 5734634964, 100, text="啥复读机")
            first.message_obj.raw_message.message.reply_to_message = SimpleNamespace(text="鲨哥我们试试复读机")
            await plugin.on_group_message(first)
            second = make_event(bot, "-100123", 5158534585, 101, text="啥复读机 ")
            await plugin.on_group_message(second)

        asyncio.run(run())
        self.assertEqual([item["message_id"] for item in bot.copied], [101])

    def test_reply_then_forward_with_same_text(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            first = make_event(bot, "-100123", 5734634964, 100, text="啥复读机")
            first.message_obj.raw_message.message.reply_to_message = SimpleNamespace(text="鲨哥我们试试复读机")
            await plugin.on_group_message(first)
            second = make_event(bot, "-100123", 5158534585, 101, text="啥复读机")
            second.message_obj.raw_message.message.forward_origin = SimpleNamespace(
                sender_user=SimpleNamespace(id=5158534585),
                chat=None,
                message_id=None,
                date="2026-11-03T15:58:00Z",
                sender_user_name=None,
            )
            await plugin.on_group_message(second)

        asyncio.run(run())
        self.assertEqual([item["message_id"] for item in bot.forwarded], [101])

    def test_photo_breaks_run(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123", 1, 100, text="复读机也开发出来了"))
            photo = make_event(bot, "-100123", 2, 101, text=None)
            photo.message_obj.raw_message.message.photo = [SimpleNamespace(file_unique_id="frog")]
            await plugin.on_group_message(photo)
            await plugin.on_group_message(make_event(bot, "-100123", 3, 102, text="复读机也开发出来了"))

        asyncio.run(run())
        self.assertEqual(bot.copied, [])

    def test_missing_photo_event_still_breaks_run_by_message_id(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123", 1, 100, text="复读机也开发出来了"))
            # Telegram assigned 101 to a photo, but the adapter did not emit it.
            await plugin.on_group_message(make_event(bot, "-100123", 3, 102, text="复读机也开发出来了"))
            await plugin.on_group_message(make_event(bot, "-100123", 4, 103, text="复读机也开发出来了"))

        asyncio.run(run())
        self.assertEqual([item["message_id"] for item in bot.copied], [103])

    def test_late_photo_event_from_reported_log(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123", 5158534585, 98551, text="复读机也开发出来了"))
            # AstrBot dispatched 98553 first while it was resolving photo 98552.
            await plugin.on_group_message(make_event(bot, "-100123", 8115552162, 98553, text="复读机也开发出来了"))
            photo = make_event(bot, "-100123", 5734634964, 98552, text=None)
            photo.message_obj.raw_message.message.photo = [SimpleNamespace(file_unique_id="frog")]
            await plugin.on_group_message(photo)

        asyncio.run(run())
        self.assertEqual(bot.copied, [])
        self.assertEqual(bot.forwarded, [])

    def test_forwarded_message_uses_telegram_forward(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {})
        origin = SimpleNamespace(
            sender_user=SimpleNamespace(id=77),
            chat=None,
            message_id=None,
            date="2026-11-03T12:00:00Z",
            sender_user_name=None,
        )

        async def run():
            for sender, mid in ((1, 1), (2, 2)):
                event = make_event(bot, "-100123", sender, mid)
                event.message_obj.raw_message.message.forward_origin = origin
                await plugin.on_group_message(event)

        asyncio.run(run())
        self.assertEqual(len(bot.forwarded), 1)
        self.assertEqual(bot.forwarded[0]["message_id"], 2)

    def test_group_allowlist(self):
        bot = FakeBot()
        plugin = NMBotRepeat(object(), {"enabled_groups": ["-100456"]})

        async def run():
            await plugin.on_group_message(make_event(bot, "-100123", 1, 1))
            await plugin.on_group_message(make_event(bot, "-100123", 2, 2))

        asyncio.run(run())
        self.assertEqual(bot.copied, [])


if __name__ == "__main__":
    unittest.main()

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch


def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    return obj


class FakeBot:
    id = 99

    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent), chat=types.SimpleNamespace(id=kwargs["chat_id"]))

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


class FakeEvent:
    def __init__(self, bot, message):
        self.platform_meta = types.SimpleNamespace(name="telegram")
        self.message_obj = types.SimpleNamespace(raw_message=types.SimpleNamespace(message=message))
        self.client = bot
        self.stopped = False

    def stop_event(self):
        self.stopped = True


class RuntimeTests(unittest.TestCase):
    def test_group_message_sends_only_one_matching_rule_and_stops_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            identity = lambda *args, **kwargs: (lambda fn: fn)
            fake_filter = types.SimpleNamespace(
                event_message_type=identity,
                EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group"),
            )
            class Context:
                def register_web_api(self, *args, **kwargs):
                    pass
            stubs = {
                "astrbot": module("astrbot"),
                "astrbot.api": module("astrbot.api", AstrBotConfig=dict, logger=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)),
                "astrbot.api.event": module("astrbot.api.event", AstrMessageEvent=FakeEvent, filter=fake_filter),
                "astrbot.api.star": module("astrbot.api.star", Context=Context, Star=type("Star", (), {"__init__": lambda self, context: None})),
                "astrbot.api.web": module("astrbot.api.web", PluginUploadFile=type("PluginUploadFile", (), {}), error_response=lambda *a, **k: None, json_response=lambda x: x, request=None),
                "astrbot.core": module("astrbot.core"),
                "astrbot.core.utils": module("astrbot.core.utils"),
                "astrbot.core.utils.astrbot_path": module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_data_path=lambda: folder),
            }
            with patch.dict(sys.modules, stubs):
                sys.modules.pop("astrbot_plugin_keyword_reply.main", None)
                plugin_module = importlib.import_module("astrbot_plugin_keyword_reply.main")
                plugin = plugin_module.KeywordReply(Context(), {"enabled": False})
                plugin.store.replace([
                    {"group_id": "-100123", "pattern": "菜单", "priority": 1, "templates": [{"kind": "text", "text": "高优先级"}]},
                    {"group_id": "-100123", "pattern": "菜单", "priority": 0, "templates": [{"kind": "text", "text": "低优先级"}]},
                ])
                bot = FakeBot()
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=-100123), from_user=types.SimpleNamespace(id=42, is_bot=False),
                    text="今天有什么菜单", caption=None, document=None, audio=None, video=None,
                    sticker=None, forward_origin=None, message_thread_id=None, is_topic_message=False,
                    message_id=7,
                )
                event = FakeEvent(bot, message)
                asyncio.run(plugin.on_group_message(event))
                self.assertEqual([item["text"] for item in bot.sent], ["高优先级"])
                self.assertTrue(event.stopped)
                self.assertEqual(bot.sent[0]["reply_to_message_id"], 7)
                # A nonmatching group cannot use another group's rule.
                message.chat.id = -100456
                event = FakeEvent(bot, message)
                asyncio.run(plugin.on_group_message(event))
                self.assertFalse(event.stopped)
                self.assertEqual(len(bot.sent), 1)
                plugin.store.replace([{
                    "group_id": "-100123", "pattern": "菜单", "response_mode": "all",
                    "auto_delete_reply": True, "auto_delete_minutes": 2,
                    "templates": [{"kind": "text", "text": "回复一"}, {"kind": "text", "text": "回复二"}],
                }])
                message.chat.id = -100123
                message.text = "菜单"

                async def send_and_delete():
                    with patch.object(plugin_module.asyncio, "sleep", new=AsyncMock()) as sleep:
                        await plugin.on_group_message(FakeEvent(bot, message))
                        await asyncio.gather(*plugin._delete_tasks)
                        self.assertEqual([call.args[0] for call in sleep.await_args_list], [120, 120])

                asyncio.run(send_and_delete())
                self.assertEqual([item["message_id"] for item in bot.deleted], [2, 3])
                self.assertTrue(all(item["chat_id"] == -100123 for item in bot.deleted))
            sys.modules.pop("astrbot_plugin_keyword_reply.main", None)


if __name__ == "__main__":
    unittest.main()

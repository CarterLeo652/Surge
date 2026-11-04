"""使用真实 AstrBot / Telegram 类和本地 HTTP 模拟，绝不连接 Telegram。"""

import atexit
import importlib.util
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HAS_ASTRBOT = importlib.util.find_spec("astrbot") is not None

if HAS_ASTRBOT:
    _runtime = tempfile.TemporaryDirectory(prefix="dick-astrbot-test-")
    atexit.register(_runtime.cleanup)
    with patch.dict(os.environ, {"ASTRBOT_ROOT": _runtime.name}):
        from astrbot.api import AstrBotConfig
        from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata
        from astrbot.core.platform.button_interaction import (
            _ButtonCallbackRegistry,
            configure_button_callback_registry,
            decode_button_callback,
        )
        from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
        from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
        from astrbot.core.star.filter.button_action import ButtonActionFilter
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterTypeFilter
        from astrbot.core.star.star_handler import star_handlers_registry
        from astrbot.core.star.star_manager import PluginManager
        from telegram import Update
        from telegram.error import NetworkError
        from telegram.ext import ExtBot
        from telegram.request import BaseRequest

        from ..localization import tr
        from ..main import DickGrowerBotPlugin

    class LocalTelegramRequest(BaseRequest):
        @property
        def read_timeout(self):
            return 5

        def __init__(self):
            self.calls = []
            self.next_id = 1000
            self.fail_once = None
            self.fail_with = None
            self.members = {}
            self.clock = lambda: 1793491200
            self.message_dates = {}

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def do_request(self, url, method, request_data=None, **kwargs):
            action = url.rsplit("/", 1)[-1]
            params = request_data.parameters if request_data else {}
            self.calls.append((action, params))
            if self.fail_once == action:
                self.fail_once = None
                failure = self.fail_with or NetworkError("local injected network fault")
                self.fail_with = None
                raise failure
            if action == "getMe":
                result = {
                    "id": 12345,
                    "is_bot": True,
                    "first_name": "首席增鸡师",
                    "username": "dick_test_bot",
                }
            elif action == "getChatMember":
                uid = int(params["user_id"])
                result = {
                    "status": "member",
                    "user": {"id": uid, "is_bot": uid == 12345, "first_name": "测试成员"},
                    "is_anonymous": False,
                    "can_be_edited": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_manage_video_chats": True,
                    "can_restrict_members": True,
                    "can_promote_members": True,
                    "can_change_info": True,
                    "can_invite_users": True,
                    "can_post_stories": True,
                    "can_edit_stories": True,
                    "can_delete_stories": True,
                } | self.members.get(uid, {})
            elif action in {"sendMessage", "editMessageText"}:
                self.next_id += 1
                message_id = params.get("message_id", self.next_id)
                created = self.message_dates.setdefault(message_id, int(self.clock()))
                result = {
                    "message_id": message_id,
                    "date": created,
                    "chat": {
                        "id": int(params["chat_id"]),
                        "type": "supergroup",
                        "title": "拼鸡测试群",
                    },
                    "text": params.get("text", ""),
                }
            else:
                result = True
            return 200, json.dumps({"ok": True, "result": result}).encode()


@unittest.skipUnless(HAS_ASTRBOT, "适配器测试需要安装 AstrBot 4.29.4")
class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_patch = patch.dict(os.environ, {"ASTRBOT_ROOT": self.temp.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        configure_button_callback_registry(Path(self.temp.name) / "core.sqlite3")
        self.request = LocalTelegramRequest()
        self.bot = ExtBot(
            "12345:LOCAL_TEST_TOKEN",
            request=self.request,
            get_updates_request=LocalTelegramRequest(),
        )
        await self.bot.initialize()
        self.addAsyncCleanup(self.bot.shutdown)
        self.plugin = DickGrowerBotPlugin(
            SimpleNamespace(), {"growth_min": 10, "growth_max": 10, "leaderboard_page_size": 1}
        )
        self.plugin.settings = replace(
            self.plugin.settings, growth_min=10, growth_max=10, leaderboard_page_size=1
        )
        self.plugin.game.settings = self.plugin.settings
        self.scope = ("12345", "-100123")

    @staticmethod
    def user(uid):
        return {"id": int(uid), "is_bot": False, "first_name": f"鸡友{uid}"}

    def command_event(self, uid, body, message_id=10, reply_uid=None, chat_id=-100123, topic=None):
        message = {
            "message_id": message_id,
            "date": 1793491200,
            "from": self.user(uid),
            "chat": {"id": chat_id, "type": "supergroup", "title": "测试"},
            "text": body,
        }
        if reply_uid:
            message["reply_to_message"] = {
                "message_id": 1,
                "date": 1793491200,
                "from": self.user(reply_uid),
                "chat": message["chat"],
                "text": "鸡友消息",
            }
        if topic:
            message.update(is_topic_message=True, message_thread_id=topic)
        update = Update.de_json({"update_id": message_id, "message": message}, self.bot)
        abm = AstrBotMessage()
        abm.raw_message = update
        abm.sender = None
        abm.message = []
        abm.type = MessageType.GROUP_MESSAGE
        abm.group_id = str(chat_id)
        abm.session_id = str(chat_id)
        abm.message_str = body
        return TelegramPlatformEvent(
            body, abm, PlatformMetadata("telegram", "测试", "test"), str(chat_id), self.bot
        )

    async def click_event(self, uid, callback, message_id, chat_id=-100123):
        update = Update.de_json(
            {
                "update_id": 9999,
                "callback_query": {
                    "id": "local_query",
                    "from": self.user(uid),
                    "chat_instance": "local_chat",
                    "data": callback,
                    "message": {
                        "message_id": message_id,
                        "date": self.request.message_dates.get(message_id, 1793491200),
                        "chat": {"id": chat_id, "type": "supergroup", "title": "测试"},
                        "text": "原消息",
                    },
                },
            },
            self.bot,
        )
        captured = []
        adapter = object.__new__(TelegramPlatformAdapter)

        async def handle(abm):
            captured.append(abm)

        adapter.handle_msg = handle
        await adapter.callback_query_handler(update, SimpleNamespace(bot=self.bot))
        self.assertEqual(len(captured), 1)
        abm = captured[0]
        return TelegramPlatformEvent(
            abm.message_str,
            abm,
            PlatformMetadata("telegram", "测试", "test"),
            abm.session_id,
            self.bot,
        )

    def calls(self, action):
        return [params for method, params in self.request.calls if method == action]

    def seed(self, uid):
        return self.plugin.game.grow(self.scope, str(uid), f"鸡友{uid}", f"seed{uid}")

    async def make_pk(self):
        event = self.command_event(1, "/dick_pvp 5", 100)
        await self.plugin.pvp(event, "5")
        payload = self.calls("sendMessage")[-1]
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        with self.plugin.game.connect() as db:
            row = db.execute("SELECT * FROM challenges").fetchone()
        return button["callback_data"], int(row["message_id"])

    async def test_real_handler_registration_is_telegram_only(self):
        handlers = [
            h
            for h in star_handlers_registry
            if h.handler_module_path == DickGrowerBotPlugin.__module__
        ]
        self.assertEqual(len(handlers), 12)
        self.assertTrue(
            all(
                any(isinstance(f, PlatformAdapterTypeFilter) for f in h.event_filters)
                for h in handlers
            )
        )
        actions = {
            f.action_id
            for h in handlers
            for f in h.event_filters
            if isinstance(f, ButtonActionFilter)
        }
        self.assertEqual(
            actions,
            {
                "dick:pk",
                "dick:top",
                "dick:stats",
                "dick:loan",
                "dick:cleanup",
            },
        )
        commands = [f for h in handlers for f in h.event_filters if isinstance(f, CommandFilter)]
        self.assertEqual(
            {f.command_name for f in commands},
            {
                "dick_grow",
                "dick_pvp",
                "dick_stats",
                "dick_top",
                "dick_of_day",
                "dick_loan",
                "dick_cleanup",
            },
        )
        for command in commands:
            self.assertFalse(command.alias)
        # 核对截图六项菜单，包括原仓库中的标点和中文描述。
        menu = object.__new__(TelegramPlatformAdapter).collect_commands()
        plugin_names = {f.command_name for f in commands}
        self.assertEqual(plugin_names, {item.command for item in menu})
        self.assertEqual(
            {item.command: item.description for item in menu},
            {
                "dick_grow": "让你的丁丁变大！",
                "dick_top": "获取聊天中最大的丁丁",
                "dick_of_day": "选举今日丁丁",
                "dick_pvp": "斗鸡！",
                "dick_loan": "负数？申请贷款！",
                "dick_stats": "统计",
                "dick_cleanup": "设置我的消息在本群留多久",
            },
        )

    async def test_real_metadata_and_configuration_parser_accept_the_plugin(self):
        root = Path(__file__).resolve().parents[1]
        metadata = PluginManager._load_plugin_metadata(str(root))
        self.assertEqual(metadata.display_name, "DickGrowerBot")
        self.assertEqual(metadata.support_platforms, ["telegram"])
        schema = PluginManager._load_plugin_config_schema(str(root / "_conf_schema.json"))
        config = AstrBotConfig(str(Path(self.temp.name) / "plugin_config.json"), schema=schema)
        plugin = DickGrowerBotPlugin(SimpleNamespace(), config)
        self.assertEqual(plugin.settings.refresh_mode, "group_interval")
        self.assertEqual(plugin.settings.daily_time, "00:00")
        self.assertEqual(set(schema), {"refresh_hours"})

    async def test_grow_preserves_forum_topic_and_stops_model_pipeline(self):
        event = self.command_event(1, "/dick_grow", topic=42)
        await self.plugin.grow(event)
        payload = self.calls("sendMessage")[-1]
        self.assertEqual(payload["message_thread_id"], 42)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(event._force_stopped)
        self.assertTrue(payload["text"].startswith("你的丁丁已经<b>"))

    async def test_pk_native_callback_edits_original_and_removes_button(self):
        self.seed(1)
        self.seed(2)
        token, message = await self.make_pk()
        self.assertLessEqual(len(token.encode()), 64)
        action_id, _ = decode_button_callback(token)
        self.assertEqual(action_id, "dick:pk")
        event = await self.click_event(2, token, message)
        self.assertTrue(ButtonActionFilter("dick:pk").filter(event, {}))
        await self.plugin.pk_clicked(event)
        edit = self.calls("editMessageText")[-1]
        self.assertEqual(edit["message_id"], message)
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})
        self.assertTrue(edit["text"].startswith("胜利者是<b>"))
        self.assertEqual(sum(self.plugin.game.stats(self.scope, str(i))["wins"] for i in [1, 2]), 1)

    async def test_reload_keeps_current_buttons_and_same_message_is_not_resent(self):
        self.seed(1)
        self.seed(2)
        token, message = await self.make_pk()
        count = len(self.calls("sendMessage"))
        self.plugin = DickGrowerBotPlugin(SimpleNamespace(), {})
        await self.plugin.pvp(self.command_event(1, "/dick_pvp 5", 100), "5")
        self.assertEqual(len(self.calls("sendMessage")), count)
        event = await self.click_event(2, token, message)
        await self.plugin.pk_clicked(event)
        await self.plugin.pk_clicked(event)
        self.assertEqual(sum(self.plugin.game.stats(self.scope, str(i))["wins"] for i in [1, 2]), 1)

    async def test_callback_lookup_survives_empty_in_memory_registry(self):
        self.seed(1)
        self.seed(2)
        token, message = await self.make_pk()
        fresh = _ButtonCallbackRegistry()
        fresh.configure(Path(self.temp.name) / "core.sqlite3")
        with patch("astrbot.core.platform.button_interaction._button_callback_registry", fresh):
            event = await self.click_event(2, token, message)
            await self.plugin.pk_clicked(event)
        self.assertEqual(sum(self.plugin.game.stats(self.scope, str(i))["wins"] for i in [1, 2]), 1)

    async def test_failed_edit_can_be_retried_without_another_pk(self):
        self.seed(1)
        self.seed(2)
        token, message = await self.make_pk()
        event = await self.click_event(2, token, message)
        self.request.fail_once = "editMessageText"
        await self.plugin.pk_clicked(event)
        await self.plugin.pk_clicked(event)
        self.assertEqual(sum(self.plugin.game.stats(self.scope, str(i))["wins"] for i in [1, 2]), 1)
        self.assertEqual(len(self.calls("editMessageText")), 2)

    async def test_self_click_alert_keeps_challenge_open(self):
        self.seed(1)
        token, message = await self.make_pk()
        await self.plugin.pk_clicked(await self.click_event(1, token, message))
        self.assertFalse(self.calls("editMessageText"))
        self.assertEqual("你不能和自己斗鸡！", self.calls("answerCallbackQuery")[-1]["text"])

    async def test_forwarded_button_in_another_group_cannot_move_lengths(self):
        self.seed(1)
        self.seed(2)
        token, message = await self.make_pk()
        await self.plugin.pk_clicked(await self.click_event(2, token, message, chat_id=-100999))
        self.assertEqual(
            "可能此消息是通过旧版本的机器人发送的。请尝试重新发送。",
            self.calls("answerCallbackQuery")[-1]["text"],
        )
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 10)

    async def test_leaderboard_buttons_edit_in_place(self):
        self.seed(1)
        self.seed(2)
        await self.plugin.top(self.command_event(1, "/dick_top"))
        payload = self.calls("sendMessage")[-1]
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        event = await self.click_event(1, button["callback_data"], 1001)
        await self.plugin.top_clicked(event)
        self.assertIn("2|<b><u>鸡友1</u></b>", self.calls("editMessageText")[-1]["text"])
        self.assertEqual(len(self.calls("sendMessage")), 1)

    async def test_bad_amount_has_chinese_prompt(self):
        self.seed(1)
        await self.plugin.pvp(self.command_event(1, "/dick_pvp wrong"), "wrong")
        self.assertEqual("发起斗鸡要下注。", self.calls("sendMessage")[-1]["text"])

    async def test_loan_confirmation_buttons_and_owner_check(self):
        self.seed(1)
        with self.plugin.game.transaction() as db:
            db.execute("UPDATE players SET length=-20")
        await self.plugin.loan(self.command_event(1, "/dick_loan", message_id=200))
        payload = self.calls("sendMessage")[-1]
        self.assertEqual(
            payload["text"],
            tr("commands.loan.confirmation.text", debt=20, payout_percentage="10.00%"),
        )
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in buttons], ["我同意！", "不同意"])
        with self.plugin.game.connect() as db:
            message = int(db.execute("SELECT message_id FROM loan_offers").fetchone()[0])
        await self.plugin.loan_clicked(
            await self.click_event(2, buttons[0]["callback_data"], message)
        )
        self.assertEqual(
            self.calls("answerCallbackQuery")[-1]["text"], tr("inline.callback.errors.another_user")
        )
        await self.plugin.loan_clicked(
            await self.click_event(1, buttons[0]["callback_data"], message)
        )
        self.assertEqual(self.calls("editMessageText")[-1]["text"], "小额贷款已成功发放！")
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 0)

    async def test_loan_cancellation_deletes_original_request(self):
        self.seed(1)
        with self.plugin.game.transaction() as db:
            db.execute("UPDATE players SET length=-10")
        await self.plugin.loan(self.command_event(1, "/dick_loan", message_id=201))
        buttons = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0]
        with self.plugin.game.connect() as db:
            message = int(db.execute("SELECT message_id FROM loan_offers").fetchone()[0])
        await self.plugin.loan_clicked(
            await self.click_event(1, buttons[1]["callback_data"], message)
        )
        self.assertEqual(self.calls("deleteMessage")[-1]["message_id"], message)
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], -10)

    async def test_dick_of_day_command_uses_upstream_results(self):
        self.seed(1)
        await self.plugin.dick_of_day(self.command_event(2, "/dick_of_day", message_id=300))
        self.assertTrue(
            self.calls("sendMessage")[-1]["text"].startswith(
                '今日丁丁是<b><a href="tg://user?id=1">鸡友1</a></b>！'
            )
        )
        await self.plugin.dick_of_day(self.command_event(2, "/dick_of_day", message_id=301))
        self.assertTrue(
            self.calls("sendMessage")[-1]["text"].startswith(
                "今日丁丁已经被选出来了！是<b>鸡友1</b>。"
            )
        )

    async def test_stats_private_uses_upstream_cross_chat_summary(self):
        self.seed(1)
        event = self.command_event(1, "/dick_stats", message_id=400, chat_id=1)
        update = event.message_obj.raw_message
        raw = update.to_dict()
        raw["message"]["chat"]["type"] = "private"
        event.message_obj.raw_message = Update.de_json(raw, self.bot)
        await self.plugin.stats(event)
        self.assertEqual(
            self.calls("sendMessage")[-1]["text"],
            tr("commands.stats.personal", chats=1, max_length=10, total_length=10),
        )


if __name__ == "__main__":
    unittest.main()

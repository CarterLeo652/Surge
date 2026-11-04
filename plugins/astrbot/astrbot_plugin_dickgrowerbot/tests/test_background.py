"""真实 AstrBot 4.29.4 / Telegram SDK 下的后台与管理员交互回归。"""

import asyncio
import unittest
from types import SimpleNamespace

from . import test_adapter as fixtures
from .test_game import Clock

if fixtures.HAS_ASTRBOT:
    from telegram.error import BadRequest, RetryAfter

    from ..localization import tr
    from ..main import DickGrowerBotPlugin


@unittest.skipUnless(fixtures.HAS_ASTRBOT, "需要 AstrBot 4.29.4")
class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    command_event = fixtures.AdapterTests.command_event
    click_event = fixtures.AdapterTests.click_event
    calls = fixtures.AdapterTests.calls
    seed = fixtures.AdapterTests.seed
    make_pk = fixtures.AdapterTests.make_pk
    user = staticmethod(fixtures.AdapterTests.user)

    async def asyncSetUp(self):
        await fixtures.AdapterTests.asyncSetUp(self)
        self.clock = Clock()
        self.request.clock = self.clock
        self.plugin.game.clock = self.clock
        self.plugin.background.observe(self.bot)
        self.request.members = {1: {"status": "creator"}, 12345: {"status": "administrator"}}
        self.addAsyncCleanup(self.plugin.terminate)

    def table(self, name):
        with self.plugin.game.transaction() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {name}")]

    async def prepare_shrink(self, users=("1",)):
        for uid in users:
            self.seed(uid)
        with self.plugin.game.transaction() as db:
            db.execute("UPDATE players SET length=100")
        self.clock.now += 7 * 86400
        await self.plugin.background.run_daily_once()

    async def test_disabled_group_skips_daily_shrink_until_reenabled(self):
        self.seed(1)
        with self.plugin.game.transaction() as db:
            db.execute("UPDATE players SET length=100")
        self.clock.now += 7 * 86400
        self.plugin.config["disabled_groups"] = [self.scope[1]]
        await self.plugin.background.run_daily_once()
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 100)
        self.assertEqual(self.table("shrink_runs"), [])
        self.plugin.config["disabled_groups"] = []
        await self.plugin.background.run_daily_once()
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 98)

    async def test_disabled_group_cancels_pending_message_cleanup(self):
        self.seed(1)
        self.plugin.maintenance.set_cleanup(self.scope, "report", 1)
        await self.plugin.top(self.command_event(1, "/dick_top"))
        self.assertTrue(self.table("deletion_jobs"))
        self.clock.now += 61
        self.plugin.config["disabled_groups"] = [self.scope[1]]
        await self.plugin.background.run_queue_once()
        self.assertEqual(self.calls("deleteMessage"), [])
        self.assertTrue(all(job["state"] == "cancelled" for job in self.table("deletion_jobs")))

    async def test_cleanup_menu_rejects_non_admin_and_requires_owner(self):
        await self.plugin.cleanup(self.command_event(2, "/dick_cleanup"))
        self.assertEqual(
            self.calls("sendMessage")[-1]["text"], tr("commands.cleanup.errors.admins_only")
        )
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup", message_id=20))
        payload = self.calls("sendMessage")[-1]
        self.assertTrue(payload["text"].startswith(tr("commands.cleanup.state.header")))
        self.assertEqual(len(payload["reply_markup"]["inline_keyboard"]), 5)
        token = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.request.members[2] = {"status": "creator"}
        await self.plugin.cleanup_clicked(await self.click_event(2, token, 1002))
        self.assertEqual(
            self.calls("answerCallbackQuery")[-1]["text"], tr("inline.callback.errors.another_user")
        )
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["notice"], 0)

    async def test_cleanup_picker_and_role_recheck_update_existing_message(self):
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup"))
        token = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, token, 1001))
        picker = self.calls("editMessageText")[-1]
        self.assertEqual(
            [b["text"] for b in picker["reply_markup"]["inline_keyboard"][0]],
            ["1 分钟", "5 分钟", "15 分钟"],
        )
        self.assertEqual(picker["reply_markup"]["inline_keyboard"][-1][0]["text"], "✖️ 关闭")
        choice = picker["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        self.request.members[1] = {"status": "member"}
        await self.plugin.cleanup_clicked(await self.click_event(1, choice, 1001))
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["report"], 0)
        self.request.members[1] = {"status": "creator"}
        await self.plugin.cleanup_clicked(await self.click_event(1, choice, 1001))
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["report"], 5)
        self.assertIn("5 分钟", self.calls("editMessageText")[-1]["text"])

    async def test_missing_bot_rights_shows_original_confirmation_before_saving(self):
        self.request.members[12345] = {"status": "member"}
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup"))
        token = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, token, 1001))
        choice = self.calls("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, choice, 1001))
        warning = self.calls("editMessageText")[-1]
        self.assertEqual(warning["text"], tr("commands.cleanup.warning.no_rights", group="排行榜"))
        self.assertEqual(warning["reply_markup"]["inline_keyboard"][-1][0]["text"], "✖️ 关闭")
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["report"], 0)
        confirm = warning["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await self.plugin.cleanup_clicked(await self.click_event(1, confirm, 1001))
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["report"], 1)

    async def test_close_cleanup_deletes_menu_and_cancels_its_jobs(self):
        self.plugin.maintenance.set_cleanup(self.scope, "report", 5)
        self.plugin.maintenance.set_cleanup(self.scope, "application", 1)
        before = self.plugin.maintenance.cleanup(self.scope)
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup", message_id=20))
        payload = self.calls("sendMessage")[-1]
        button = payload["reply_markup"]["inline_keyboard"][-1][0]
        self.assertEqual(button["text"], "✖️ 关闭")
        self.assertEqual(len(self.table("deletion_jobs")), 2)
        await self.plugin.cleanup_clicked(await self.click_event(1, button["callback_data"], 1001))
        self.assertEqual([call["message_id"] for call in self.calls("deleteMessage")], [1001])
        self.assertTrue(all(job["state"] == "cancelled" for job in self.table("deletion_jobs")))
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope), before)
        self.clock.now += 61
        await self.plugin.background.run_queue_once()
        self.assertEqual(len(self.calls("deleteMessage")), 1)

    async def test_close_cleanup_requires_menu_owner_and_current_admin_role(self):
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup"))
        token = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][-1][0][
            "callback_data"
        ]
        self.request.members[2] = {"status": "creator"}
        await self.plugin.cleanup_clicked(await self.click_event(2, token, 1001))
        self.assertEqual(
            self.calls("answerCallbackQuery")[-1]["text"], tr("inline.callback.errors.another_user")
        )
        self.request.members[1] = {"status": "member"}
        await self.plugin.cleanup_clicked(await self.click_event(1, token, 1001))
        self.assertEqual(
            self.calls("answerCallbackQuery")[-1]["text"], tr("commands.cleanup.errors.admins_only")
        )
        self.assertEqual(self.calls("deleteMessage"), [])

    async def test_close_cleanup_old_menu_removes_keyboard_when_delete_is_refused(self):
        self.request.members[12345] = {"status": "member"}
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup"))
        pick = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, pick, 1001))
        token = self.calls("editMessageText")[-1]["reply_markup"]["inline_keyboard"][-1][0][
            "callback_data"
        ]
        self.request.fail_once = "deleteMessage"
        self.request.fail_with = BadRequest("Message can't be deleted")
        await self.plugin.cleanup_clicked(await self.click_event(1, token, 1001))
        closed = self.calls("editMessageText")[-1]
        self.assertEqual(closed["message_id"], 1001)
        self.assertEqual(closed["text"], "设置菜单已关闭。")
        self.assertEqual(closed["reply_markup"], {"inline_keyboard": []})

    async def test_close_cleanup_permission_warning_and_already_deleted_menu(self):
        self.request.members[12345] = {"status": "member"}
        await self.plugin.cleanup(self.command_event(1, "/dick_cleanup"))
        pick = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, pick, 1001))
        choice = self.calls("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        await self.plugin.cleanup_clicked(await self.click_event(1, choice, 1001))
        token = self.calls("editMessageText")[-1]["reply_markup"]["inline_keyboard"][-1][0][
            "callback_data"
        ]
        edit_count = len(self.calls("editMessageText"))
        self.request.fail_once = "deleteMessage"
        self.request.fail_with = BadRequest("Message to delete not found")
        await self.plugin.cleanup_clicked(await self.click_event(1, token, 1001))
        self.assertEqual(len(self.calls("editMessageText")), edit_count)
        self.assertEqual(self.plugin.maintenance.cleanup(self.scope)["report"], 0)

    async def test_report_deletion_includes_command_and_survives_plugin_reload(self):
        self.seed(1)
        self.plugin.maintenance.set_cleanup(self.scope, "report", 1)
        await self.plugin.top(self.command_event(1, "/dick_top", message_id=10))
        self.assertEqual(len(self.table("deletion_jobs")), 2)
        self.clock.now += 60
        reopened = DickGrowerBotPlugin(SimpleNamespace(), {})
        reopened.game.clock = self.clock
        reopened.background.observe(self.bot)
        await reopened.background.run_queue_once()
        self.assertEqual({p["message_id"] for p in self.calls("deleteMessage")}, {10, 1001})
        await reopened.background.run_queue_once()
        self.assertEqual(len(self.calls("deleteMessage")), 2)

    async def test_without_bot_delete_rights_only_own_reply_is_queued(self):
        self.request.members[12345] = {"status": "member"}
        self.plugin.maintenance.set_cleanup(self.scope, "report", 1)
        await self.plugin.top(self.command_event(1, "/dick_top"))
        self.assertEqual([row["target"] for row in self.table("deletion_jobs")], ["reply"])
        self.clock.now += 60
        await self.plugin.background.run_queue_once()
        self.assertEqual([p["message_id"] for p in self.calls("deleteMessage")], [1001])

    async def test_pk_result_reclassifies_and_cancels_application_deletions(self):
        self.seed(1)
        self.seed(2)
        self.plugin.maintenance.set_cleanup(self.scope, "application", 1)
        token, message = await self.make_pk()
        self.assertEqual(len(self.table("deletion_jobs")), 2)
        await self.plugin.pk_clicked(await self.click_event(2, token, message))
        self.clock.now += 61
        await self.plugin.background.run_queue_once()
        self.assertEqual(self.calls("deleteMessage"), [])
        self.assertTrue(all(row["state"] == "cancelled" for row in self.table("deletion_jobs")))

    async def test_loan_confirmation_kept_and_success_reschedules_event_cleanup(self):
        self.seed(1)
        with self.plugin.game.transaction() as db:
            db.execute("UPDATE players SET length=-20")
        self.plugin.maintenance.set_cleanup(self.scope, "event", 1)
        await self.plugin.loan(self.command_event(1, "/dick_loan", message_id=200))
        self.assertEqual(self.table("deletion_jobs"), [])
        button = self.calls("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]
        await self.plugin.loan_clicked(await self.click_event(1, button["callback_data"], 1001))
        self.assertEqual(len(self.table("deletion_jobs")), 2)
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 0)

    async def test_delete_retry_after_wait_and_already_deleted_message(self):
        self.plugin.maintenance.set_cleanup(self.scope, "report", 1)
        self.request.members[12345] = {"status": "member"}
        await self.plugin.top(self.command_event(1, "/dick_top"))
        self.clock.now += 60
        self.request.fail_once = "deleteMessage"
        self.request.fail_with = RetryAfter(10)
        await self.plugin.background.run_queue_once()
        self.assertEqual(self.table("deletion_jobs")[0]["due"], self.clock() + 11)
        self.clock.now += 11
        self.request.fail_once = "deleteMessage"
        self.request.fail_with = BadRequest("Message to delete not found")
        await self.plugin.background.run_queue_once()
        self.assertEqual(self.table("deletion_jobs")[0]["state"], "done")

    async def test_lost_delete_permission_preserves_command_but_cleans_reply(self):
        self.plugin.maintenance.set_cleanup(self.scope, "report", 1)
        await self.plugin.top(self.command_event(1, "/dick_top"))
        self.request.members[12345] = {"status": "member"}
        self.clock.now += 60
        await self.plugin.background.run_queue_once()
        self.assertEqual([p["message_id"] for p in self.calls("deleteMessage")], [1001])
        command = next(row for row in self.table("deletion_jobs") if row["target"] == "command")
        self.assertEqual(command["state"], "cancelled")

    async def test_repeated_daily_election_is_a_notice_for_cleanup(self):
        self.seed(1)
        self.plugin.maintenance.set_cleanup(self.scope, "notice", 1)
        await self.plugin.dick_of_day(self.command_event(1, "/dick_of_day", message_id=300))
        self.assertEqual(self.table("deletion_jobs"), [])
        await self.plugin.dick_of_day(self.command_event(1, "/dick_of_day", message_id=301))
        self.assertEqual(len(self.table("deletion_jobs")), 2)

    async def test_runtime_discovers_clients_after_restart_and_stops_all_tasks(self):
        await self.prepare_shrink()
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(
                platform_insts=[
                    SimpleNamespace(
                        meta=lambda: SimpleNamespace(name="telegram"),
                        get_client=lambda: self.bot,
                    )
                ]
            )
        )
        plugin = DickGrowerBotPlugin(context, {})
        plugin.game.clock = self.clock
        self.assertEqual(plugin.background.observed, {})
        self.clock.now += 86400
        await plugin.background.run_daily_once()
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["length"], 95)
        self.assertEqual(self.calls("sendMessage"), [])
        await plugin.initialize()
        await asyncio.sleep(0)
        tasks = list(plugin.background.tasks)
        await plugin.terminate()
        self.assertEqual(plugin.background.tasks, [])
        self.assertTrue(all(task.done() for task in tasks))

    async def test_daily_shrink_is_quiet_and_next_grow_reports_total_once(self):
        await self.prepare_shrink()
        self.clock.now += 86400
        await self.plugin.background.run_daily_once()
        await self.plugin.background.run_daily_once()
        await self.plugin.background.run_queue_once()
        self.assertEqual(self.calls("sendMessage"), [])
        self.assertEqual(self.plugin.maintenance.pending_loss(self.scope, "1"), 5)
        self.plugin.maintenance.set_cleanup(self.scope, "event", 1)
        await self.plugin.grow(self.command_event(1, "/dick_grow", message_id=300))
        payload = self.calls("sendMessage")[-1]
        self.assertTrue(payload["text"].endswith("太久没签到了，你的鸡鸡累计缩水了 5 厘米。"))
        self.assertEqual(self.plugin.maintenance.pending_loss(self.scope, "1"), 0)
        self.assertEqual([job["target"] for job in self.table("deletion_jobs")], ["reply"])
        await self.plugin.grow(self.command_event(1, "/dick_grow", message_id=301))
        self.assertNotIn("累计缩水", self.calls("sendMessage")[-1]["text"])

    async def test_failed_grow_send_keeps_reminder_for_next_attempt_after_restart(self):
        await self.prepare_shrink()
        self.request.fail_once = "sendMessage"
        await self.plugin.grow(self.command_event(1, "/dick_grow", message_id=300))
        self.assertEqual(self.plugin.maintenance.pending_loss(self.scope, "1"), 2)
        checkins = self.plugin.game.stats(self.scope, "1")["checkins"]
        reopened = DickGrowerBotPlugin(SimpleNamespace(), {})
        reopened.game.clock = self.clock
        await reopened.grow(self.command_event(1, "/dick_grow", message_id=301))
        self.assertIn("累计缩水了 2 厘米", self.calls("sendMessage")[-1]["text"])
        self.assertEqual(reopened.maintenance.pending_loss(self.scope, "1"), 0)
        self.assertEqual(reopened.game.stats(self.scope, "1")["checkins"], checkins)

    async def test_concurrent_grow_replies_show_pending_loss_once(self):
        await self.prepare_shrink()
        await asyncio.gather(
            self.plugin.grow(self.command_event(1, "/dick_grow", message_id=300)),
            self.plugin.grow(self.command_event(1, "/dick_grow", message_id=301)),
        )
        self.assertEqual(sum("累计缩水" in call["text"] for call in self.calls("sendMessage")), 1)
        self.assertEqual(self.plugin.maintenance.pending_loss(self.scope, "1"), 0)
        self.assertEqual(self.plugin.game.stats(self.scope, "1")["checkins"], 2)

    async def test_other_players_and_commands_leave_reminder_for_owner(self):
        await self.prepare_shrink()
        await self.plugin.grow(self.command_event(2, "/dick_grow", message_id=300))
        await self.plugin.stats(self.command_event(1, "/dick_stats", message_id=301))
        await self.plugin.top(self.command_event(1, "/dick_top", message_id=302))
        self.assertTrue(all("累计缩水" not in p["text"] for p in self.calls("sendMessage")))
        self.assertEqual(self.plugin.maintenance.pending_loss(self.scope, "1"), 2)
        await self.plugin.grow(self.command_event(1, "/dick_grow", message_id=303))
        self.assertIn("累计缩水了 2 厘米", self.calls("sendMessage")[-1]["text"])

"""每日评选、贷款和原项目文案的行为约束。"""

import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from ..data_path import database_path
from ..game import Game, GameError
from ..localization import tr
from ..presentation import grow_text, loan_text, stats_text, top_text
from ..settings import Settings
from . import test_game as fixtures
from .test_game import RiggedRandom


class UpstreamTests(unittest.TestCase):
    setUp = fixtures.GameTests.setUp
    grow = fixtures.GameTests.grow
    length = fixtures.GameTests.length
    set_length = fixtures.GameTests.set_length
    battle = fixtures.GameTests.battle

    def offer(self, uid="1", request="loan", message="200"):
        result = self.game.loan_request(self.scope, uid, request)
        if result["status"] == "confirm":
            self.game.bind_loan(self.scope, result["id"], message)
        return result

    def test_dod_empty_and_once_per_utc_day(self):
        self.assertEqual(self.game.dick_of_day(self.scope)["status"], "no_candidates")
        self.grow("1")
        first = self.game.dick_of_day(self.scope)
        self.assertEqual(first["status"], "chosen")
        self.assertEqual(self.length("1"), 11)
        self.assertEqual(self.game.dick_of_day(self.scope)["status"], "already_chosen")
        self.clock.now += 86400
        self.assertEqual(self.game.dick_of_day(self.scope)["status"], "chosen")
        stats = self.game.stats(self.scope, "1")
        self.assertEqual((stats["checkins"], stats["streak"]), (1, 1))

    def test_dod_uses_active_poor_candidates_and_preserves_ties(self):
        for uid in ["1", "2", "3"]:
            self.grow(uid)
        self.set_length("1", 100)
        self.game.rng = RiggedRandom(winner=1)
        result = self.game.dick_of_day(self.scope)
        self.assertIn(result["uid"], {"2", "3"})
        self.clock.now += 8 * 86400
        self.assertEqual(self.game.dick_of_day(self.scope)["status"], "no_candidates")

    def test_dod_concurrent_calls_have_one_award(self):
        self.grow()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.game.dick_of_day(self.scope), range(16)))
        self.assertEqual(sum(r["status"] == "chosen" for r in results), 1)
        self.assertEqual(self.length("1"), 11)

    def test_loan_confirmation_idempotency_owner_and_repayment(self):
        self.grow()
        self.set_length("1", -20)
        offer = self.offer()
        self.assertEqual(
            loan_text(offer),
            "你的浦西将被重置为零，但每次增长将减少 <b>10.00%</b>，直到偿还所有 <b>20 厘米</b>。",
        )
        with self.assertRaises(GameError):
            self.game.loan_answer(self.scope, offer["id"], "2", "200", True)
        result = self.game.loan_answer(self.scope, offer["id"], "1", "200", True)
        self.assertEqual(result, {"status": "granted"})
        self.assertEqual(self.length("1"), 0)
        self.assertEqual(result, self.game.loan_answer(self.scope, offer["id"], "1", "200", True))
        self.assertEqual(
            self.game.loan_request(self.scope, "1", "later"), {"status": "debt", "debt": 20}
        )
        self.clock.now += 86400
        growth = self.grow(request="repay")
        self.assertEqual((growth["base"], growth["payout"], growth["change"]), (10, 1, 9))
        self.assertEqual(self.game.loan_request(self.scope, "1", "remaining")["debt"], 19)

    def test_loan_rechecks_eligibility_and_cancel_is_final(self):
        self.grow()
        self.set_length("1", -10)
        offer = self.offer()
        self.set_length("1", 0)
        self.assertEqual(
            self.game.loan_answer(self.scope, offer["id"], "1", "200", True)["status"],
            "not_eligible",
        )
        self.set_length("1", -10)
        other = self.offer(request="other", message="201")
        self.assertEqual(
            self.game.loan_answer(self.scope, other["id"], "1", "201", False)["status"], "refused"
        )
        self.assertEqual(
            self.game.loan_answer(self.scope, other["id"], "1", "201", True)["status"], "refused"
        )
        self.assertEqual(self.length("1"), -10)

    def test_loan_double_clicks_cannot_create_duplicate_debt(self):
        self.grow()
        self.set_length("1", -10)
        offer = self.offer()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.game.loan_answer(self.scope, offer["id"], "1", "200", True),
                    range(16),
                )
            )
        self.assertTrue(all(r["status"] == "granted" for r in results))
        self.assertEqual(self.length("1"), 0)
        self.assertEqual(self.game.loan_request(self.scope, "1", "debt")["debt"], 10)

    def test_pk_winnings_and_dod_bonuses_pay_loans(self):
        self.grow("1")
        self.grow("2")
        self.set_length("1", -10)
        offer = self.offer()
        self.game.loan_answer(self.scope, offer["id"], "1", "200", True)
        self.game.rng = RiggedRandom(winner=1)
        pk = self.game.challenge(self.scope, "2", "乙", 5, "pvp")
        self.game.bind_challenge(self.scope, pk["id"], "100")
        result = self.game.accept(self.scope, pk["id"], "1", "甲", "100")
        self.assertEqual((result["winner"], result["payout"], self.length("1")), ("1", 1, 4))
        self.game.rng = RiggedRandom(winner=0)
        self.game.rng.randint = lambda low, high: high
        winner = self.game.dick_of_day(self.scope)
        self.assertEqual(winner["uid"], "1")
        self.assertEqual(winner["payout"], 1)
        self.assertEqual(self.game.loan_request(self.scope, "1", "remaining")["debt"], 8)

    def test_negative_aid_uses_original_rounding(self):
        self.grow()
        self.set_length("1", -50)
        self.clock.now += 86400
        r = self.grow(request="aid")
        self.assertEqual((r["help_bonus"], r["change"]), (1, 11))

    def test_locale_file_and_menu_copy_are_identical_to_upstream(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "UPSTREAM_SOURCE.json").read_text())
        digest = hashlib.sha256((root / "locales/zh.yml").read_bytes()).hexdigest()
        self.assertEqual(digest, manifest["locale_sha256"])
        self.assertEqual(manifest["commit"], "e305c88ce601fba0f4189da69bd118640376bd2e")
        self.assertEqual(
            [
                tr(f"commands.{key}.description")
                for key in ["grow", "top", "dod", "pvp", "loan", "stats"]
            ],
            [
                "让你的丁丁变大！",
                "获取聊天中最大的丁丁",
                "选举今日丁丁",
                "斗鸡！",
                "负数？申请贷款！",
                "统计",
            ],
        )

    def test_upstream_rendered_growth_top_and_statistics(self):
        r = self.grow()
        self.assertEqual(
            grow_text(r, self.cfg, self.clock()),
            "你的丁丁已经<b>10 厘米</b> 增长了，现在长度为<b>10 厘米</b>。\n你在排行榜上的位置是<b>1</b>。\n\n下次尝试在 <b>24</b>小时 <b>0</b>分钟后。",
        )
        self.assertEqual(
            top_text(self.game.top(self.scope), "1"),
            "最大的丁丁排行榜：\n\n1|<b><u>鸡友1</u></b> — <b>10</b> 厘米\n\n<i>[+] 表示一个牛子怪今天还没有增长他的丁丁。</i>",
        )
        snapshot = self.game.stats(self.scope, "1")
        self.assertEqual(
            stats_text(snapshot, self.cfg),
            "长度: <b>10</b>\n在排行榜上的位置: <b>1</b>\n\n胜率: <b>0%</b>。\n战斗次数: <b>0</b>。\n胜利次数: <b>0</b>。\n最大连胜: <b>0</b>。\n获得长度: <b>0 厘米</b>。\n失去长度: <b>0 厘米</b>。",
        )

    def test_only_refresh_interval_is_publicly_configurable(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
        self.assertEqual(set(schema), {"refresh_hours"})
        cfg = Settings.from_mapping(
            {"refresh_hours": 0.5, "growth_min": 999, "streak_enabled": False}
        )
        self.assertEqual((cfg.interval_minutes, cfg.growth_min, cfg.streak_enabled), (30, -5, True))
        self.game.settings = replace(self.cfg, interval_minutes=30, refresh_mode="group_interval")
        self.grow()
        self.clock.now += 1799
        with self.assertRaises(GameError):
            self.grow(request="early")
        self.clock.now += 1
        self.assertEqual(self.grow(request="due")["length"], 20)

    def test_rename_migrates_old_database_without_changing_original(self):
        legacy = Path(self.temp.name) / "plugin_data/astrbot_plugin_pinjj/pinjj.sqlite3"
        old = Game(legacy, self.cfg, self.clock, RiggedRandom())
        old.grow(self.scope, "1", "旧用户", "old")
        with old.transaction() as db:
            for table in ["loans", "loan_offers", "daily_winners"]:
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=1")
        target = database_path(legacy.parent.parent / "astrbot_plugin_dickgrowerbot")
        new = Game(target, self.cfg, self.clock, RiggedRandom())
        self.assertEqual(new.stats(self.scope, "1")["length"], 10)
        self.clock.now += 86400
        new.grow(self.scope, "1", "新用户", "next")
        self.assertEqual(old.stats(self.scope, "1")["length"], 10)
        self.assertEqual(new.stats(self.scope, "1")["length"], 20)

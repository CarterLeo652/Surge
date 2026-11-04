import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ..game import Game, GameError
from ..presentation import mention, top_text
from ..settings import Settings


class Clock:
    def __init__(self, value="2026-11-01T00:00:00+00:00"):
        self.now = datetime.fromisoformat(value).timestamp()

    def __call__(self):
        return self.now


class RiggedRandom:
    def __init__(self, negative=False, winner=0):
        self.negative, self.winner = negative, winner

    def random(self):
        return 0.99 if self.negative else 0.01

    def randint(self, low, high):
        return low

    def randrange(self, stop):
        return self.winner


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "game.sqlite3"
        self.clock = Clock()
        self.cfg = Settings(
            timezone="UTC",
            daily_time="00:00",
            growth_min=10,
            growth_max=10,
            newcomer_days=0,
            streak_enabled=False,
        )
        self.game = Game(self.path, self.cfg, self.clock, RiggedRandom())
        self.scope = ("12345", "-100123")

    def grow(self, uid="1", request=None):
        return self.game.grow(self.scope, uid, "鸡友" + uid, request or "grow" + uid)

    def length(self, uid):
        return self.game.stats(self.scope, uid)["length"]

    def set_length(self, uid, value):
        """只用于准备 0 长度等边界测试数据。"""
        with self.game.transaction() as db:
            db.execute(
                "UPDATE players SET length=? WHERE bot=? AND chat=? AND uid=?",
                (value, *self.scope, uid),
            )

    def battle(self, bet=5, request="pvp", message="100"):
        result = self.game.challenge(self.scope, "1", "鸡友1", bet, request)
        self.game.bind_challenge(self.scope, result["id"], message)
        return result

    def test_daily_signin_once_and_receipt_survives_restart(self):
        first = self.grow()
        with self.assertRaises(GameError):
            self.grow(request="second")
        self.clock.now += 86400
        other = Game(self.path, self.cfg, self.clock, RiggedRandom())
        self.assertEqual(first, other.grow(self.scope, "1", "改名", "grow1"))
        self.assertEqual(other.grow(self.scope, "1", "改名", "next")["length"], 20)

    def test_concurrent_signin_has_one_success(self):
        def run(i):
            try:
                return self.grow(request=str(i))
            except GameError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(16)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.game.stats(self.scope, "1")["checkins"], 1)

    def test_daily_wall_clock_boundary(self):
        cfg = replace(self.cfg, timezone="Asia/Shanghai", daily_time="08:00")
        self.game.settings = cfg
        self.clock.now -= 1
        self.grow()
        self.clock.now += 1
        self.assertEqual(self.grow(request="new_window")["length"], 20)

    def test_personal_interval_ignores_midnight_and_boundary_is_inclusive(self):
        self.game.settings = replace(
            self.cfg, refresh_mode="personal_interval", interval_minutes=60
        )
        self.clock.now -= 10
        self.grow()
        self.clock.now += 3599
        with self.assertRaises(GameError):
            self.grow(request="early")
        self.clock.now += 1
        self.assertEqual(self.grow(request="on_time")["length"], 20)

    def test_shared_period_is_fixed_and_restart_does_not_reset(self):
        cfg = replace(self.cfg, refresh_mode="group_interval", interval_minutes=60)
        self.game.settings = cfg
        self.clock.now += 3500
        self.grow()
        self.clock.now += 99
        with self.assertRaises(GameError):
            self.grow(request="early")
        self.clock.now += 1
        other = Game(self.path, cfg, self.clock, RiggedRandom())
        self.assertEqual(other.grow(self.scope, "1", "甲", "due")["length"], 20)

    def test_streak_counts_natural_days_not_number_of_signins(self):
        self.game.settings = replace(
            self.cfg, refresh_mode="personal_interval", interval_minutes=1, streak_enabled=True
        )
        self.assertEqual(self.grow()["streak"], 1)
        self.clock.now += 60
        self.assertEqual(self.grow(request="same_day")["streak"], 1)
        self.clock.now += 86400
        result = self.grow(request="next_day")
        self.assertEqual((result["streak"], result["bonus"]), (2, 1))
        self.clock.now += 2 * 86400
        self.assertEqual(self.grow(request="missed")["streak"], 1)
        self.assertEqual(self.game.stats(self.scope, "1")["max_streak"], 2)

    def test_newcomer_protection_is_shared_across_groups(self):
        self.game.settings = replace(self.cfg, growth_min=-5, newcomer_days=7)
        self.game.rng = RiggedRandom(negative=True)
        self.assertEqual(self.grow()["change"], 1)
        self.clock.now += 8 * 86400
        result = self.game.grow((self.scope[0], "-100999"), "1", "甲", "new_group")
        self.assertEqual(result["change"], -5)
        self.assertFalse(result["protected"])

    def test_negative_growth_and_zero_length_do_not_block_signin(self):
        self.game.settings = replace(self.cfg, growth_min=-5)
        self.game.rng = RiggedRandom(negative=True)
        self.assertEqual(self.grow()["length"], -5)
        self.clock.now += 86400
        self.assertEqual(self.grow(request="again")["length"], -10)

    def test_bonus_does_not_cancel_growth_to_zero(self):
        self.game.settings = replace(self.cfg, growth_min=-1, growth_max=1, streak_enabled=True)
        self.game.rng = RiggedRandom(negative=True)
        self.grow()
        self.clock.now += 86400
        self.assertNotEqual(self.grow(request="bonus")["change"], 0)

    def test_group_and_bot_accounts_are_isolated(self):
        self.grow()
        for scope in [(self.scope[0], "-100999"), ("another_bot", self.scope[1])]:
            self.assertEqual(self.game.top(scope)["total"], 0)
            self.assertEqual(self.game.stats(scope, "1")["length"], 0)

    def test_zero_acceptor_can_lose_into_negative_with_original_rules(self):
        self.grow("1")
        self.grow("2")
        self.set_length("1", 20)
        self.set_length("2", 0)
        b = self.battle()
        result = self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual((self.length("1"), self.length("2")), (25, -5))
        self.assertEqual(result["winner"], "1")
        with self.assertRaises(GameError):
            self.game.challenge(self.scope, "2", "乙", 1, "negative")

    def test_strict_acceptor_length_check(self):
        self.grow("1")
        self.grow("2")
        self.set_length("1", 20)
        self.set_length("2", 0)
        self.game.settings = replace(self.cfg, pk_check_acceptor_length=True)
        b = self.battle()
        with self.assertRaises(GameError):
            self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual((self.length("1"), self.length("2")), (20, 0))

    def test_pk_self_unregistered_bad_bet_and_wrong_message(self):
        self.grow("1")
        b = self.battle()
        for uid, message in [("1", "100"), ("2", "100"), ("1", "999")]:
            with self.assertRaises(GameError):
                self.game.accept(self.scope, b["id"], uid, "甲", message)
        for bet in [-1, True, 11, 65536]:
            with self.assertRaises(GameError):
                self.game.challenge(self.scope, "1", "甲", bet, f"bad{bet}")

    def test_old_pk_button_survives_restart_and_only_settles_once(self):
        self.grow("1")
        self.grow("2")
        b = self.battle()
        other = Game(self.path, self.cfg, self.clock, RiggedRandom())
        first = other.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual(first, other.accept(self.scope, b["id"], "2", "乙", "100"))
        self.assertEqual(self.game.stats(self.scope, "1")["wins"], 1)
        self.assertEqual(self.game.stats(self.scope, "2")["losses"], 1)

    def test_concurrent_accepts_have_one_transfer_and_result(self):
        for uid in ["1", "2", "3"]:
            self.grow(uid)
        b = self.battle()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda i: self.game.accept(self.scope, b["id"], str(2 + i % 2), "鸡友", "100"),
                    range(16),
                )
            )
        self.assertTrue(all(r == results[0] for r in results))
        stats = [self.game.stats(self.scope, uid) for uid in ["1", "2", "3"]]
        self.assertEqual(sum(s["length"] for s in stats), 30)
        self.assertEqual(sum(s["wins"] for s in stats), 1)
        self.assertEqual(sum(s["losses"] for s in stats), 1)

    def test_pending_pk_rechecks_owner_balance_after_another_pk(self):
        self.grow("1")
        self.grow("2")
        self.grow("3")
        b = self.battle(bet=10)
        other = self.battle(bet=1, request="other", message="101")
        self.game.rng.winner = 1
        self.game.accept(self.scope, other["id"], "3", "丙", "101")
        with self.assertRaises(GameError):
            self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual((self.length("1"), self.length("2"), self.length("3")), (9, 10, 11))

    def test_pk_expiry_and_scope_validation(self):
        self.grow("1")
        self.grow("2")
        self.game.settings = replace(self.cfg, pk_timeout_seconds=60)
        b = self.battle()
        with self.assertRaises(GameError):
            self.game.accept(("other", self.scope[1]), b["id"], "2", "乙", "100")
        self.clock.now += 60
        with self.assertRaises(GameError):
            self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual((self.length("1"), self.length("2")), (10, 10))

    def test_pk_database_failure_rolls_back_both_players_and_statistics(self):
        self.grow("1")
        self.grow("2")
        b = self.battle()
        with self.game.transaction() as db:
            db.execute(
                "CREATE TRIGGER fail_loss BEFORE UPDATE ON players WHEN NEW.losses > OLD.losses BEGIN SELECT RAISE(ABORT,'injected fault'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual((self.length("1"), self.length("2")), (10, 10))
        self.assertEqual(self.game.stats(self.scope, "1")["wins"], 0)
        with self.game.transaction() as db:
            db.execute("DROP TRIGGER fail_loss")
        self.game.accept(self.scope, b["id"], "2", "乙", "100")
        self.assertEqual(self.game.stats(self.scope, "1")["wins"], 1)

    def test_top_pagination_uses_same_tie_order_as_stats(self):
        self.game.settings = replace(self.cfg, leaderboard_page_size=1)
        self.grow("2")
        self.grow("1")
        self.assertEqual(self.game.top(self.scope)["rows"][0]["uid"], "1")
        self.assertEqual(self.game.stats(self.scope, "2")["rank"], 2)
        self.assertEqual(self.game.top(self.scope, 999)["page"], 1)

    def test_username_html_and_linebreaks_are_safe(self):
        r = self.game.grow(self.scope, "1", "<b>甲</b>\n伪装\u202e", "grow")
        body = top_text(self.game.top(self.scope), "1")
        self.assertIn("&lt;b&gt;甲&lt;/b&gt;", body)
        self.assertEqual(r["name"], "<b>甲</b>\n伪装\u202e")
        with self.assertRaises(ValueError):
            mention('1" onclick="x', "甲")


class SettingsTests(unittest.TestCase):
    def test_schema_defaults_match_runtime_defaults(self):
        path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(path.read_text())
        self.assertEqual(
            Settings.from_mapping({k: v["default"] for k, v in schema.items()}), Settings()
        )

    def test_invalid_configuration_is_rejected(self):
        cases = [
            {"refresh_hours": 0},
            {"refresh_hours": -1},
            {"refresh_hours": float("nan")},
            {"refresh_hours": float("inf")},
            {"refresh_hours": True},
            {"refresh_hours": "24"},
            {"refresh_hours": 9000},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings.from_mapping(values)

    def test_default_refresh_matches_original_utc_midnight(self):
        now = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
        self.assertEqual(Settings().window(now), (now, now + 86400))

    def test_clock_rollback_does_not_create_extra_signin(self):
        now = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
        self.assertGreater(Settings().next_due(now, now - 10), now - 10)


if __name__ == "__main__":
    unittest.main()

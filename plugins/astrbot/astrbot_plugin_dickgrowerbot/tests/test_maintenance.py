"""定时缩水、清理队列和重启恢复的事务回归。"""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from ..game import Game, GameError
from ..maintenance import LEASE_SECONDS, Maintenance
from . import test_game as fixtures
from .test_game import RiggedRandom


class MaintenanceTests(unittest.TestCase):
    grow = fixtures.GameTests.grow
    length = fixtures.GameTests.length
    set_length = fixtures.GameTests.set_length

    def setUp(self):
        fixtures.GameTests.setUp(self)
        self.store = Maintenance(self.game)

    def prepare_stale(self, length=100):
        self.grow()
        self.set_length("1", length)
        self.clock.now += 7 * 86400

    def rows(self, table):
        with self.game.transaction() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def track(self, kind="report", minutes=1, permissions=True):
        self.store.set_cleanup(self.scope, kind, minutes)
        self.store.track_message(self.scope, "100", self.clock(), "10", self.clock())
        self.store.schedule_deletion(self.scope, "100", kind, 100, permissions)

    def test_additive_tables_preserve_existing_game_records(self):
        old = self.grow()
        Maintenance(Game(self.path, self.cfg, self.clock, RiggedRandom()))
        self.assertEqual(self.grow(), old)
        self.assertEqual(self.length("1"), 10)
        self.assertEqual(
            self.store.cleanup(self.scope),
            dict.fromkeys(("notice", "report", "event", "application"), 0),
        )

    def test_seven_day_boundary_and_first_gradual_deduction(self):
        self.grow()
        self.set_length("1", 100)
        self.clock.now += 7 * 86400 - 1
        self.assertEqual(self.store.run_shrink(self.scope), 0)
        self.clock.now += 1
        self.assertEqual(self.store.run_shrink(self.scope), 1)
        self.assertEqual(self.length("1"), 98)
        self.assertEqual(self.store.pending_loss(self.scope, "1"), 2)

    def test_gradual_rate_reaches_ten_percent_and_uses_integer_ceiling(self):
        self.prepare_stale(1000)
        self.store.run_shrink(self.scope)
        self.assertEqual(self.length("1"), 985)
        self.clock.now += 86400
        self.store.run_shrink(self.scope)
        self.assertEqual(self.length("1"), 956)
        self.clock.now += 5 * 86400
        self.store.run_shrink(self.scope)
        self.assertEqual(self.length("1"), 860)

    def test_one_centimeter_floor_and_zero_negative_players_are_preserved(self):
        for uid in ("1", "2", "3"):
            self.grow(uid)
        for uid, value in [("1", 1), ("2", 0), ("3", -10)]:
            self.set_length(uid, value)
        self.clock.now += 14 * 86400
        self.assertEqual(self.store.run_shrink(self.scope), 1)
        self.assertEqual([self.length(uid) for uid in ("1", "2", "3")], [0, 0, -10])
        self.clock.now += 86400
        self.assertEqual(self.store.run_shrink(self.scope), 0)

    def test_shrink_does_not_change_signup_streak_debt_or_pk_stats(self):
        self.prepare_stale()
        with self.game.transaction() as db:
            db.execute("INSERT INTO loans VALUES(?,?,?,?,?)", (*self.scope, "1", 50, 0.1))
        before = self.game.stats(self.scope, "1")
        self.store.run_shrink(self.scope)
        after = self.game.stats(self.scope, "1")
        for key in ["last_grow", "streak", "streak_day", "checkins", "wins", "losses"]:
            self.assertEqual(before[key], after[key])
        self.assertEqual(self.rows("loans")[0]["debt"], 50)

    def test_restart_and_concurrent_runs_deduct_and_accumulate_once(self):
        self.prepare_stale()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.run_shrink(self.scope), range(16)))
        self.assertEqual(sum(results), 1)
        reopened = Maintenance(Game(self.path, self.cfg, self.clock, RiggedRandom()))
        self.assertEqual(reopened.run_shrink(self.scope), 0)
        self.assertEqual(self.length("1"), 98)
        self.assertEqual(reopened.pending_loss(self.scope, "1"), 2)

    def test_pending_failure_rolls_back_deduction_and_run_marker(self):
        self.prepare_stale()
        with self.game.transaction() as db:
            db.execute(
                "CREATE TRIGGER fail_pending BEFORE INSERT ON shrink_pending BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.run_shrink(self.scope)
        self.assertEqual(self.length("1"), 100)
        self.assertEqual(self.rows("shrink_pending"), [])
        self.assertEqual(self.rows("shrink_runs"), [])
        with self.game.transaction() as db:
            db.execute("DROP TRIGGER fail_pending")
        self.assertEqual(self.store.run_shrink(self.scope), 1)

    def test_resuming_signup_stops_inactivity_and_pending_survives(self):
        self.prepare_stale()
        self.store.run_shrink(self.scope)
        self.grow(request="returning")
        self.clock.now += 86400
        self.assertEqual(self.store.run_shrink(self.scope), 0)
        self.assertEqual(self.length("1"), 108)
        self.assertEqual(self.store.pending_loss(self.scope, "1"), 2)

    def test_concurrent_signup_and_shrink_keep_accounting_consistent(self):
        self.prepare_stale()
        with ThreadPoolExecutor(max_workers=2) as pool:
            shrink = pool.submit(self.store.run_shrink, self.scope)
            signup = pool.submit(self.grow, request="concurrent")
            shrink.result()
            signup.result()
        losses = self.store.pending_loss(self.scope, "1")
        self.assertEqual(self.length("1"), 110 - losses)
        self.assertEqual(self.game.stats(self.scope, "1")["checkins"], 2)

    def test_bot_and_group_pending_losses_are_isolated(self):
        self.prepare_stale()
        other = ("54321", "-100123")
        self.game.grow(other, "1", "其他机器人", "other")
        self.store.run_shrink(self.scope)
        self.assertEqual(self.game.stats(other, "1")["length"], 10)
        self.assertEqual(self.store.pending_loss(other, "1"), 0)
        self.assertEqual(self.store.pending_loss((self.scope[0], "other"), "1"), 0)
        self.assertEqual(self.store.run_shrink(other), 0)

    def test_twelve_hour_signup_does_not_duplicate_daily_shrink(self):
        self.game.settings = replace(self.cfg, interval_minutes=720)
        self.prepare_stale()
        self.store.run_shrink(self.scope)
        self.clock.now += 12 * 3600
        self.assertEqual(self.store.run_shrink(self.scope), 0)
        self.clock.now += 12 * 3600
        self.assertEqual(self.store.run_shrink(self.scope), 1)

    def test_cleanup_is_opt_in_and_can_delete_only_reply_without_rights(self):
        self.store.track_message(self.scope, "100", self.clock(), "10", self.clock())
        self.store.schedule_deletion(self.scope, "100", "report", 100, True)
        self.assertEqual(self.rows("deletion_jobs"), [])
        self.track(permissions=False)
        self.assertEqual([row["target"] for row in self.rows("deletion_jobs")], ["reply"])
        self.track(permissions=True)
        self.assertEqual(len(self.rows("deletion_jobs")), 2)
        self.assertEqual({row["due"] for row in self.rows("deletion_jobs")}, {self.clock() + 60})

    def test_editing_challenge_cancels_old_jobs_and_invalidates_old_lease(self):
        self.track(kind="application")
        self.clock.now += 60
        old = self.store.claim("12345")[0]
        self.store.schedule_deletion(self.scope, "100", "event", 100, True)
        self.assertFalse(self.store.is_claimed(old))
        self.assertEqual(self.store.claim("12345"), [])
        self.store.set_cleanup(self.scope, "event", 5)
        self.store.schedule_deletion(self.scope, "100", "event", 100, True)
        self.assertEqual({row["due"] for row in self.rows("deletion_jobs")}, {self.clock() + 300})

    def test_leases_prevent_double_claim_and_stale_completion(self):
        self.track(permissions=False)
        self.clock.now += 60
        first = self.store.claim("12345")[0]
        self.assertEqual(self.store.claim("12345"), [])
        self.clock.now += LEASE_SECONDS + 1
        second = Maintenance(self.game).claim("12345")[0]
        self.store.finish(first)
        self.assertTrue(self.store.is_claimed(second))
        self.store.finish(second)
        self.assertEqual(self.rows("deletion_jobs")[0]["state"], "done")

    def test_retry_schedule_and_expiration_keep_history(self):
        self.track(permissions=False)
        self.clock.now += 60
        job = self.store.claim("12345")[0]
        self.store.finish(job, delay=60)
        self.assertEqual(self.store.claim("12345"), [])
        self.clock.now += 60
        self.assertEqual(self.store.claim("12345")[0]["attempts"], 1)
        self.clock.now += 48 * 3600
        self.store.prune()
        self.assertEqual(self.rows("deletion_jobs")[0]["state"], "expired")

    def test_reading_time_extends_delay_and_old_messages_use_age_cap(self):
        self.track(permissions=False)
        self.store.schedule_deletion(self.scope, "100", "report", 4000, False)
        self.assertEqual(self.rows("deletion_jobs")[0]["due"], self.clock() + 480)
        self.store.track_message(self.scope, "200", self.clock() - 46 * 3600)
        self.store.set_cleanup(self.scope, "report", 180)
        self.store.schedule_deletion(self.scope, "200", "report", 100, False)
        old = next(row for row in self.rows("deletion_jobs") if row["message"] == "200")
        self.assertEqual(old["due"], self.clock() + 3600)

    def test_invalid_cleanup_values_are_rejected(self):
        for kind, minutes in [("bad", 1), ("notice", True), ("notice", -1), ("notice", 999)]:
            with self.assertRaises(GameError):
                self.store.set_cleanup(self.scope, kind, minutes)
        self.store.set_cleanup(self.scope, "report", 5)
        self.store.set_cleanup(self.scope, "report")
        self.assertEqual(self.store.cleanup(self.scope)["report"], 0)

    def test_acknowledging_snapshot_preserves_new_loss_and_clears_empty_row(self):
        self.prepare_stale()
        self.store.run_shrink(self.scope)
        snapshot = self.store.pending_loss(self.scope, "1")
        self.clock.now += 86400
        self.store.run_shrink(self.scope)
        total = self.store.pending_loss(self.scope, "1")
        self.assertGreater(total, snapshot)
        self.store.acknowledge_loss(self.scope, "1", snapshot)
        self.assertEqual(self.store.pending_loss(self.scope, "1"), total - snapshot)
        self.store.acknowledge_loss(self.scope, "1", total - snapshot)
        self.assertEqual(self.rows("shrink_pending"), [])
        self.assertEqual(self.length("1"), 100 - total)

    def test_upgrade_cancels_legacy_notices_once_and_keeps_daily_marker(self):
        self.prepare_stale()
        self.store.run_shrink(self.scope)
        with self.game.transaction() as db:
            day = db.execute("SELECT day FROM shrink_runs").fetchone()[0]
            db.execute("DELETE FROM shrink_pending")
            db.executescript("""
                CREATE TABLE shrink_events (
                    bot TEXT,chat TEXT,day TEXT,uid TEXT,
                    before_length INTEGER,loss INTEGER,after_length INTEGER
                );
                CREATE TABLE shrink_notices (
                    id INTEGER PRIMARY KEY,bot TEXT,chat TEXT,day TEXT,
                    state TEXT,lease TEXT,lease_until REAL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO shrink_events VALUES(?,?,?,?,100,2,98)", (*self.scope, day, "1")
            )
            db.execute(
                "INSERT INTO shrink_notices VALUES(1,?,?,?,'pending','old-lease',9999999999)",
                (*self.scope, day),
            )
            db.execute(
                "INSERT INTO shrink_events VALUES(?,?,?,?,102,2,100)",
                (*self.scope, "2026-11-07", "1"),
            )
            db.execute(
                "INSERT INTO shrink_notices VALUES(2,?,?,?,'done',NULL,0)",
                (*self.scope, "2026-11-07"),
            )
        reopened = Maintenance(Game(self.path, self.cfg, self.clock, RiggedRandom()))
        self.assertEqual(reopened.run_shrink(self.scope), 0)
        self.assertEqual(self.length("1"), 98)
        self.assertEqual(reopened.pending_loss(self.scope, "1"), 2)
        self.assertEqual(self.rows("shrink_notices")[0]["state"], "cancelled")
        self.assertIsNone(self.rows("shrink_notices")[0]["lease"])
        self.assertEqual(len(self.rows("shrink_events")), 2)
        self.assertEqual(Maintenance(self.game).pending_loss(self.scope, "1"), 2)
        self.clock.now += 86400
        reopened.run_shrink(self.scope)
        self.assertEqual(len(self.rows("shrink_events")), 2)

"""SQLite 游戏账本。所有长度变化、去重和战绩写入在同一事务完成。"""

import json
import math
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from .localization import INVALID_DATA, tr
from .settings import Settings

MAX_LENGTH = 2**63 - 1


class GameError(Exception):
    """可直接展示给玩家的中文业务提示。"""


def clean_name(value):
    return str(value)


class Game:
    def __init__(self, path, settings=None, clock=time.time, rng=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings()
        self.settings.validate()
        self.clock = clock
        self.rng = rng or secrets.SystemRandom()
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    bot TEXT NOT NULL, uid TEXT NOT NULL, joined REAL NOT NULL,
                    PRIMARY KEY(bot, uid)
                );
                CREATE TABLE IF NOT EXISTS players (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, uid TEXT NOT NULL,
                    name TEXT NOT NULL, length INTEGER NOT NULL DEFAULT 0,
                    last_grow REAL, streak_day TEXT,
                    streak INTEGER NOT NULL DEFAULT 0,
                    max_streak INTEGER NOT NULL DEFAULT 0,
                    checkins INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
                    win_streak INTEGER NOT NULL DEFAULT 0,
                    max_win_streak INTEGER NOT NULL DEFAULT 0,
                    acquired INTEGER NOT NULL DEFAULT 0, lost INTEGER NOT NULL DEFAULT 0,
                    given INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(bot, chat, uid)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, request TEXT NOT NULL,
                    kind TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(bot, chat, request, kind)
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY, bot TEXT NOT NULL, chat TEXT NOT NULL,
                    owner TEXT NOT NULL, bet INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
                    expires REAL, message_id TEXT, result TEXT
                );
                CREATE INDEX IF NOT EXISTS player_top ON players(bot, chat, length DESC, uid);
                CREATE TABLE IF NOT EXISTS daily_winners (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, day TEXT NOT NULL,
                    uid TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(bot,chat,day)
                );
                CREATE TABLE IF NOT EXISTS loans (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, uid TEXT NOT NULL,
                    debt INTEGER NOT NULL, ratio REAL NOT NULL,
                    PRIMARY KEY(bot,chat,uid)
                );
                CREATE TABLE IF NOT EXISTS loan_offers (
                    id TEXT PRIMARY KEY, bot TEXT NOT NULL, chat TEXT NOT NULL,
                    uid TEXT NOT NULL, amount INTEGER NOT NULL, ratio REAL NOT NULL,
                    message_id TEXT, state TEXT NOT NULL DEFAULT 'pending', result TEXT
                );
                PRAGMA user_version=2;
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def player(db, scope, uid):
        return db.execute(
            "SELECT * FROM players WHERE bot=? AND chat=? AND uid=?", (*scope, str(uid))
        ).fetchone()

    @staticmethod
    def rank(db, scope, uid):
        row = db.execute(
            """SELECT pos FROM (
            SELECT uid, ROW_NUMBER() OVER (ORDER BY length DESC,last_grow DESC,name,uid) AS pos
            FROM players WHERE bot=? AND chat=?) WHERE uid=?""",
            (*scope, str(uid)),
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def receipt(db, scope, request, kind):
        row = db.execute(
            "SELECT result FROM receipts WHERE bot=? AND chat=? AND request=? AND kind=?",
            (*scope, str(request), kind),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def remember(db, scope, request, kind, result):
        db.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
            (*scope, str(request), kind, json.dumps(result, ensure_ascii=False)),
        )
        return result

    @staticmethod
    def bounded(value):
        if not -MAX_LENGTH <= value <= MAX_LENGTH:
            raise GameError(tr("errors.feature_disabled"))
        return value

    @staticmethod
    def rounded(value):
        return math.floor(value + 0.5)

    def loan_payment(self, db, scope, uid, base):
        loan = db.execute(
            "SELECT debt,ratio FROM loans WHERE bot=? AND chat=? AND uid=? AND debt>0",
            (*scope, uid),
        ).fetchone()
        return min(loan["debt"], self.rounded(base * loan["ratio"])) if loan and base > 0 else 0

    def increment(self, db, scope, uid, factory, streak=None):
        player = self.player(db, scope, uid)
        for _ in range(10):
            base = factory()
            help_bonus = (
                self.rounded(abs(player["length"]) * self.settings.help_negative_ratio)
                if player["length"] < 0
                else 0
            )
            payout = self.loan_payment(db, scope, uid, base)
            streak_bonus = (
                sum(streak >= grade for grade in self.settings.grades)
                if streak is not None and self.settings.streak_enabled
                else 0
            )
            change = base + help_bonus - payout + streak_bonus
            if change:
                if payout:
                    db.execute(
                        "UPDATE loans SET debt=debt-? WHERE bot=? AND chat=? AND uid=?",
                        (payout, *scope, uid),
                    )
                return {
                    "base": base,
                    "change": change,
                    "bonus": streak_bonus,
                    "help_bonus": help_bonus,
                    "payout": payout,
                    "perks_applied": True,
                }
        # 原版连续十次被加成抵消时，丢弃加成并重新抽取基础变化。
        base = factory()
        return {
            "base": base,
            "change": base,
            "bonus": 0,
            "help_bonus": 0,
            "payout": 0,
            "perks_applied": False,
        }

    def grow(self, scope, uid, name, request):
        uid, now = str(uid), self.clock()
        cfg = self.settings
        with self.transaction() as db:
            old = self.receipt(db, scope, request, "grow")
            if old:
                return old
            db.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?)", (scope[0], uid, now))
            db.execute(
                "INSERT OR IGNORE INTO players (bot,chat,uid,name) VALUES (?,?,?,?)",
                (*scope, uid, clean_name(name)),
            )
            db.execute(
                "UPDATE players SET name=? WHERE bot=? AND uid=?", (clean_name(name), scope[0], uid)
            )
            p = self.player(db, scope, uid)
            due = cfg.next_due(p["last_grow"], now)
            if due > now:
                raise GameError(tr("commands.grow.tomorrow"))
            day = datetime.fromtimestamp(now, cfg.tz).date()
            previous_day = p["streak_day"]
            if previous_day and previous_day >= day.isoformat():
                streak = p["streak"]
            elif previous_day and (day - datetime.fromisoformat(previous_day).date()).days == 1:
                streak = p["streak"] + 1
            else:
                streak = 1
            joined = db.execute(
                "SELECT joined FROM users WHERE bot=? AND uid=?", (scope[0], uid)
            ).fetchone()[0]
            protected = cfg.newcomer_days > 0 and int((now - joined) // 86400) <= cfg.newcomer_days

            # 正负方向分别抽签，保持原版 50% 正增长，不把 -5～10 当作均匀分布。
            def roll():
                if cfg.growth_min > 0:
                    return self.rng.randint(cfg.growth_min, cfg.growth_max)
                if protected or cfg.growth_min >= 0 or self.rng.random() < cfg.positive_probability:
                    return self.rng.randint(1, cfg.growth_max)
                return self.rng.randint(cfg.growth_min, -1)

            increment = self.increment(db, scope, uid, roll, streak)
            change = increment["change"]
            if not increment["perks_applied"]:
                streak = p["streak"]
            length = self.bounded(p["length"] + change)
            db.execute(
                """UPDATE players SET name=?,length=?,last_grow=?,streak_day=?,streak=?,
                max_streak=MAX(max_streak,?),checkins=checkins+1
                WHERE bot=? AND chat=? AND uid=?""",
                (
                    clean_name(name),
                    length,
                    now,
                    max(previous_day or "", day.isoformat())
                    if increment["perks_applied"]
                    else previous_day,
                    streak,
                    streak,
                    *scope,
                    uid,
                ),
            )
            return self.remember(
                db,
                scope,
                request,
                "grow",
                {
                    "uid": uid,
                    "name": clean_name(name),
                    "change": change,
                    **increment,
                    "length": length,
                    "streak": streak,
                    "rank": self.rank(db, scope, uid),
                    "next": cfg.next_due(now, now),
                    "protected": protected,
                },
            )

    def challenge(self, scope, uid, name, bet, request):
        uid, now = str(uid), self.clock()
        if type(bet) is not int or not 0 <= bet <= 65535:
            raise GameError(tr("commands.pvp.errors.no_args"))
        with self.transaction() as db:
            old = self.receipt(db, scope, request, "pvp")
            if old:
                existing = db.execute(
                    "SELECT message_id,state FROM challenges WHERE id=?", (old["id"],)
                ).fetchone()
                if existing["state"] == "cancelled":
                    raise GameError(INVALID_DATA)
                old["message_id"] = existing["message_id"]
                return old
            p = self.player(db, scope, uid)
            if not p:
                raise GameError(tr("commands.pvp.errors.not_enough.initiator"))
            if p["length"] < bet:
                raise GameError(tr("commands.pvp.errors.not_enough.initiator"))
            token = secrets.token_hex(12)
            expires = (
                now + self.settings.pk_timeout_seconds if self.settings.pk_timeout_seconds else None
            )
            db.execute(
                "INSERT INTO challenges (id,bot,chat,owner,bet,created,expires) VALUES (?,?,?,?,?,?,?)",
                (token, *scope, uid, bet, now, expires),
            )
            db.execute(
                "UPDATE players SET name=? WHERE bot=? AND uid=?",
                (clean_name(name), scope[0], uid),
            )
            return self.remember(
                db,
                scope,
                request,
                "pvp",
                {"id": token, "uid": uid, "name": clean_name(name), "bet": bet, "expires": expires},
            )

    def bind_challenge(self, scope, token, message_id):
        with self.transaction() as db:
            db.execute(
                "UPDATE challenges SET message_id=? WHERE id=? AND bot=? AND chat=? AND message_id IS NULL AND state='pending'",
                (str(message_id), token, *scope),
            )

    def cancel_unsent(self, scope, token):
        with self.transaction() as db:
            db.execute(
                "UPDATE challenges SET state='cancelled' WHERE id=? AND bot=? AND chat=? AND message_id IS NULL",
                (token, *scope),
            )

    def accept(self, scope, token, uid, name, message_id):
        uid, now = str(uid), self.clock()
        with self.transaction() as db:
            battle = db.execute(
                "SELECT * FROM challenges WHERE id=? AND bot=? AND chat=?", (token, *scope)
            ).fetchone()
            if not battle or battle["message_id"] != str(message_id):
                raise GameError(INVALID_DATA)
            if battle["state"] == "settled":
                return json.loads(battle["result"])
            if battle["state"] != "pending":
                raise GameError(INVALID_DATA)
            if battle["expires"] is not None and now >= battle["expires"]:
                raise GameError(INVALID_DATA)
            if battle["owner"] == uid:
                raise GameError(tr("commands.pvp.errors.same_person"))
            owner = self.player(db, scope, battle["owner"])
            acceptor = self.player(db, scope, uid)
            if not acceptor:
                raise GameError(tr("commands.pvp.errors.not_enough.acceptor"))
            bet = battle["bet"]
            needed = bet if self.settings.pk_check_acceptor_length else 0
            if acceptor["length"] < needed:
                raise GameError(tr("commands.pvp.errors.not_enough.acceptor"))
            if not owner or owner["length"] < bet:
                raise GameError(tr("commands.pvp.errors.not_enough.initiator"))
            db.execute(
                "UPDATE players SET name=? WHERE bot=? AND chat=? AND uid=?",
                (clean_name(name), *scope, uid),
            )
            winner_id, loser_id = (
                (owner["uid"], uid) if self.rng.randrange(2) == 0 else (uid, owner["uid"])
            )
            winner, loser = self.player(db, scope, winner_id), self.player(db, scope, loser_id)
            winner_length = self.bounded(winner["length"] + bet)
            payout = self.loan_payment(db, scope, winner_id, bet)
            if payout:
                winner_length -= payout
                db.execute(
                    "UPDATE loans SET debt=debt-? WHERE bot=? AND chat=? AND uid=?",
                    (payout, *scope, winner_id),
                )
            loser_length = self.bounded(loser["length"] - bet)
            db.execute(
                """UPDATE players SET length=?,wins=wins+1,win_streak=win_streak+1,
                max_win_streak=MAX(max_win_streak,win_streak+1),acquired=acquired+?
                WHERE bot=? AND chat=? AND uid=?""",
                (winner_length, bet, *scope, winner_id),
            )
            db.execute(
                """UPDATE players SET length=?,losses=losses+1,win_streak=0,lost=lost+?
                WHERE bot=? AND chat=? AND uid=?""",
                (loser_length, bet, *scope, loser_id),
            )
            result = {
                "id": token,
                "bet": bet,
                "winner": winner_id,
                "winner_name": winner["name"],
                "winner_length": winner_length,
                "loser": loser_id,
                "loser_name": loser["name"],
                "loser_length": loser_length,
                "winner_rank": self.rank(db, scope, winner_id),
                "loser_rank": self.rank(db, scope, loser_id),
                "payout": payout,
                "winner_wins": winner["wins"] + 1,
                "winner_battles": winner["wins"] + winner["losses"] + 1,
                "winner_streak": winner["win_streak"] + 1,
                "winner_max_streak": max(winner["max_win_streak"], winner["win_streak"] + 1),
                "loser_wins": loser["wins"],
                "loser_battles": loser["wins"] + loser["losses"] + 1,
                "lost_win_streak": loser["win_streak"],
            }
            db.execute(
                "UPDATE challenges SET state='settled',result=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), token),
            )
            return result

    def stats(self, scope, uid):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            row = self.player(db, scope, str(uid))
            result = (
                dict(row)
                if row
                else dict(
                    uid=str(uid),
                    length=0,
                    wins=0,
                    losses=0,
                    win_streak=0,
                    max_win_streak=0,
                    acquired=0,
                    lost=0,
                    streak=0,
                    max_streak=0,
                    streak_day=None,
                )
            )
            result["rank"] = self.rank(db, scope, str(uid))
            result["next"] = self.settings.next_due(row["last_grow"] if row else None, self.clock())
            return result

    def personal_stats(self, bot, uid):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT COUNT(*),MAX(length),SUM(length) FROM players WHERE bot=? AND uid=?",
                (bot, str(uid)),
            ).fetchone()
            return {"chats": row[0], "max_length": row[1] or 0, "total_length": row[2] or 0}

    def dick_of_day(self, scope):
        now = self.clock()
        day = datetime.fromtimestamp(now, self.settings.tz).date().isoformat()
        next_day = (math.floor(now / 86400) + 1) * 86400
        with self.transaction() as db:
            old = db.execute(
                "SELECT uid FROM daily_winners WHERE bot=? AND chat=? AND day=?", (*scope, day)
            ).fetchone()
            if old:
                player = self.player(db, scope, old["uid"])
                return {"status": "already_chosen", "name": player["name"], "next": next_day}
            candidates = db.execute(
                "SELECT * FROM players WHERE bot=? AND chat=? AND last_grow>? ORDER BY length,uid",
                (*scope, now - self.settings.inactivity_days * 86400),
            ).fetchall()
            if not candidates:
                return {"status": "no_candidates"}
            eligible = []
            first_rank = 0
            for index, candidate in enumerate(candidates):
                if index == 0 or candidate["length"] != candidates[index - 1]["length"]:
                    first_rank = index
                percentile = first_rank / (len(candidates) - 1) if len(candidates) > 1 else 0
                if percentile <= 1 - self.settings.dod_rich_exclusion_ratio:
                    eligible.append(candidate)
            winner = eligible[self.rng.randrange(len(eligible))]
            increment = self.increment(
                db, scope, winner["uid"], lambda: self.rng.randint(1, self.settings.dod_bonus_max)
            )
            length = self.bounded(winner["length"] + increment["change"])
            db.execute(
                "UPDATE players SET length=? WHERE bot=? AND chat=? AND uid=?",
                (length, *scope, winner["uid"]),
            )
            result = {
                "status": "chosen",
                "uid": winner["uid"],
                "name": winner["name"],
                "length": length,
                "rank": self.rank(db, scope, winner["uid"]),
                "next": next_day,
                **increment,
            }
            db.execute(
                "INSERT INTO daily_winners VALUES (?,?,?,?,?)",
                (*scope, day, winner["uid"], json.dumps(result, ensure_ascii=False)),
            )
            return result

    def loan_request(self, scope, uid, request):
        uid = str(uid)
        with self.transaction() as db:
            old = self.receipt(db, scope, request, "loan")
            if old:
                row = db.execute(
                    "SELECT message_id FROM loan_offers WHERE id=?", (old["id"],)
                ).fetchone()
                old["message_id"] = row["message_id"]
                return old
            active = db.execute(
                "SELECT debt FROM loans WHERE bot=? AND chat=? AND uid=? AND debt>0", (*scope, uid)
            ).fetchone()
            if active:
                return {"status": "debt", "debt": active["debt"]}
            player = self.player(db, scope, uid)
            if player is None or player["length"] >= 0:
                raise GameError(tr("commands.loan.errors.positive_length"))
            token = secrets.token_hex(12)
            result = {
                "id": token,
                "status": "confirm",
                "uid": uid,
                "amount": -player["length"],
                "ratio": self.settings.loan_payout_ratio,
            }
            db.execute(
                "INSERT INTO loan_offers (id,bot,chat,uid,amount,ratio) VALUES (?,?,?,?,?,?)",
                (token, *scope, uid, result["amount"], result["ratio"]),
            )
            return self.remember(db, scope, request, "loan", result)

    def bind_loan(self, scope, token, message_id):
        with self.transaction() as db:
            db.execute(
                "UPDATE loan_offers SET message_id=? WHERE id=? AND bot=? AND chat=? AND message_id IS NULL",
                (str(message_id), token, *scope),
            )

    def loan_answer(self, scope, token, uid, message_id, agree):
        with self.transaction() as db:
            offer = db.execute(
                "SELECT * FROM loan_offers WHERE id=? AND bot=? AND chat=?", (token, *scope)
            ).fetchone()
            if not offer or offer["message_id"] != str(message_id):
                raise GameError(INVALID_DATA)
            if offer["uid"] != str(uid):
                raise GameError(tr("inline.callback.errors.another_user"))
            if offer["state"] != "pending":
                return json.loads(offer["result"])
            if not agree:
                result = {"status": "refused"}
            elif offer["ratio"] != self.settings.loan_payout_ratio:
                result = {"status": "payout_ratio_changed"}
            else:
                player = self.player(db, scope, str(uid))
                if player is None or player["length"] >= 0:
                    result = {"status": "not_eligible"}
                else:
                    length = self.bounded(player["length"] + offer["amount"])
                    db.execute(
                        "INSERT INTO loans VALUES (?,?,?,?,?) ON CONFLICT(bot,chat,uid) DO UPDATE SET debt=debt+excluded.debt,ratio=excluded.ratio",
                        (*scope, str(uid), offer["amount"], offer["ratio"]),
                    )
                    db.execute(
                        "UPDATE players SET length=? WHERE bot=? AND chat=? AND uid=?",
                        (length, *scope, str(uid)),
                    )
                    result = {"status": "granted"}
            db.execute(
                "UPDATE loan_offers SET state='finished',result=? WHERE id=?",
                (json.dumps(result), token),
            )
            return result

    def top(self, scope, page=0):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise GameError(INVALID_DATA)
        size = self.settings.leaderboard_page_size
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            total = db.execute(
                "SELECT COUNT(*) FROM players WHERE bot=? AND chat=? AND (length!=0 OR last_grow>?)",
                (*scope, self.clock() - self.settings.inactivity_days * 86400),
            ).fetchone()[0]
            page = min(page, max(0, (total - 1) // size))
            rows = db.execute(
                "SELECT uid,name,length,last_grow FROM players WHERE bot=? AND chat=? AND (length!=0 OR last_grow>?) ORDER BY length DESC,last_grow DESC,name,uid LIMIT ? OFFSET ?",
                (*scope, self.clock() - self.settings.inactivity_days * 86400, size, page * size),
            ).fetchall()
            return {
                "page": page,
                "size": size,
                "total": total,
                "rows": [
                    {
                        **dict(r),
                        "inactive": int((self.clock() - r["last_grow"]) // 86400)
                        > self.settings.inactivity_days,
                        "can_grow": self.settings.next_due(r["last_grow"], self.clock())
                        <= self.clock(),
                    }
                    for r in rows
                ],
                "more": (page + 1) * size < total,
            }

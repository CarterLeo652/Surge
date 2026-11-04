"""自动清理与闲置缩水，共用游戏账本的 SQLite 事务。"""

import math
import secrets
from datetime import UTC, datetime

from .game import GameError
from .localization import INVALID_DATA

CLEANUP_GROUPS = ("notice", "report", "event", "application")
CLEANUP_DELAYS = (1, 5, 15, 60, 180)
MAX_DELETE_AGE = 47 * 3600
LEASE_SECONDS = 120


class Maintenance:
    def __init__(self, game):
        self.game = game
        with game.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cleanup_settings (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, kind TEXT NOT NULL,
                    minutes INTEGER NOT NULL, PRIMARY KEY(bot,chat,kind)
                );
                CREATE TABLE IF NOT EXISTS message_links (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, reply TEXT NOT NULL,
                    command TEXT, created REAL NOT NULL, command_created REAL,
                    PRIMARY KEY(bot,chat,reply)
                );
                CREATE TABLE IF NOT EXISTS deletion_jobs (
                    id INTEGER PRIMARY KEY, bot TEXT NOT NULL, chat TEXT NOT NULL,
                    message TEXT NOT NULL, anchor TEXT NOT NULL, target TEXT NOT NULL,
                    created REAL NOT NULL, due REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    UNIQUE(bot,chat,message)
                );
                CREATE INDEX IF NOT EXISTS deletion_due ON deletion_jobs(bot,state,due);
                CREATE TABLE IF NOT EXISTS shrink_runs (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, day TEXT NOT NULL,
                    PRIMARY KEY(bot,chat,day)
                );
                CREATE TABLE IF NOT EXISTS shrink_pending (
                    bot TEXT NOT NULL, chat TEXT NOT NULL, uid TEXT NOT NULL,
                    loss INTEGER NOT NULL, PRIMARY KEY(bot,chat,uid)
                );
            """)
            # executescript 会提交建表事务，另起事务保证旧通知迁移和取消一起完成。
            db.execute("BEGIN IMMEDIATE")
            legacy = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shrink_notices'"
            ).fetchone()
            if legacy:
                db.execute(
                    """INSERT INTO shrink_pending(bot,chat,uid,loss)
                    SELECT e.bot,e.chat,e.uid,SUM(e.loss) FROM shrink_events e
                    JOIN shrink_notices n ON n.bot=e.bot AND n.chat=e.chat AND n.day=e.day
                    WHERE n.state='pending' GROUP BY e.bot,e.chat,e.uid
                    ON CONFLICT(bot,chat,uid) DO UPDATE SET loss=loss+excluded.loss"""
                )
                db.execute(
                    """UPDATE shrink_notices SET state='cancelled',lease=NULL,lease_until=0
                    WHERE state='pending'"""
                )

    def rooms(self, bot):
        with self.game.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT chat FROM players WHERE bot=? ORDER BY chat", (str(bot),)
                )
            ]

    def cleanup(self, scope):
        with self.game.transaction() as db:
            rows = db.execute(
                "SELECT kind,minutes FROM cleanup_settings WHERE bot=? AND chat=?", scope
            )
            return dict.fromkeys(CLEANUP_GROUPS, 0) | {row["kind"]: row["minutes"] for row in rows}

    def set_cleanup(self, scope, kind=None, minutes=None):
        if kind is not None and kind not in CLEANUP_GROUPS:
            raise GameError(INVALID_DATA)
        if minutes is not None and (
            type(minutes) is not int or minutes not in (0, *CLEANUP_DELAYS)
        ):
            raise GameError(INVALID_DATA)
        with self.game.transaction() as db:
            if kind is None:
                db.execute("DELETE FROM cleanup_settings WHERE bot=? AND chat=?", scope)
            elif minutes is None:
                db.execute(
                    "DELETE FROM cleanup_settings WHERE bot=? AND chat=? AND kind=?", (*scope, kind)
                )
            else:
                db.execute(
                    """INSERT INTO cleanup_settings VALUES(?,?,?,?)
                    ON CONFLICT(bot,chat,kind) DO UPDATE SET minutes=excluded.minutes""",
                    (*scope, kind, minutes),
                )

    def track_message(self, scope, reply, created, command=None, command_created=None):
        with self.game.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO message_links VALUES(?,?,?,?,?,?)",
                (
                    *scope,
                    str(reply),
                    str(command) if command is not None else None,
                    created,
                    command_created,
                ),
            )

    def schedule_deletion(self, scope, reply, kind, visible_length, delete_commands):
        if kind not in CLEANUP_GROUPS:
            raise ValueError(kind)
        now = self.game.clock()
        with self.game.transaction() as db:
            anchor = str(reply)
            db.execute(
                """UPDATE deletion_jobs SET state='cancelled',lease=NULL,lease_until=0
                WHERE bot=? AND chat=? AND anchor=?""",
                (*scope, anchor),
            )
            setting = db.execute(
                "SELECT minutes FROM cleanup_settings WHERE bot=? AND chat=? AND kind=?",
                (*scope, kind),
            ).fetchone()
            minutes = setting[0] if setting else 0
            link = db.execute(
                "SELECT * FROM message_links WHERE bot=? AND chat=? AND reply=?", (*scope, anchor)
            ).fetchone()
            if not minutes or not link:
                return
            delay = min(MAX_DELETE_AGE, max(minutes * 60, math.ceil(visible_length * 60 / 500)))
            targets = [(anchor, "reply", link["created"])]
            if delete_commands and link["command"] is not None:
                targets.append(
                    (link["command"], "command", link["command_created"] or link["created"])
                )
            for message, target, created in targets:
                if now >= created + MAX_DELETE_AGE:
                    continue
                due = min(now + delay, created + MAX_DELETE_AGE)
                db.execute(
                    """INSERT INTO deletion_jobs(bot,chat,message,anchor,target,created,due)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(bot,chat,message) DO UPDATE SET
                    anchor=excluded.anchor,target=excluded.target,created=excluded.created,
                    due=excluded.due,state='pending',attempts=0,lease=NULL,lease_until=0""",
                    (*scope, message, anchor, target, created, due),
                )

    def cancel_deletion(self, scope, reply):
        with self.game.transaction() as db:
            db.execute(
                """UPDATE deletion_jobs SET state='cancelled',lease=NULL,lease_until=0
                WHERE bot=? AND chat=? AND anchor=?""",
                (*scope, str(reply)),
            )

    def run_shrink(self, scope):
        now = self.game.clock()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.game.transaction() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO shrink_runs VALUES(?,?,?)", (*scope, day)
            ).rowcount
            if not inserted:
                return 0
            rows = db.execute(
                """SELECT uid,length,last_grow FROM players WHERE bot=? AND chat=?
                AND length>0 AND last_grow<=? ORDER BY uid""",
                (*scope, now - 7 * 86400),
            ).fetchall()
            for row in rows:
                overdue_days = int((now - row["last_grow"]) // 86400) - 7 + 1
                step = min(overdue_days, 7)
                # 最高 10%，7 天渐进。整数向上取整，避免大长度的浮点误差。
                loss = min(row["length"], max(1, (row["length"] * step + 69) // 70))
                length = row["length"] - loss
                db.execute(
                    "UPDATE players SET length=? WHERE bot=? AND chat=? AND uid=?",
                    (length, *scope, row["uid"]),
                )
                db.execute(
                    """INSERT INTO shrink_pending VALUES(?,?,?,?)
                    ON CONFLICT(bot,chat,uid) DO UPDATE SET loss=loss+excluded.loss""",
                    (*scope, row["uid"], loss),
                )
            return len(rows)

    def pending_loss(self, scope, uid):
        with self.game.transaction() as db:
            row = db.execute(
                "SELECT loss FROM shrink_pending WHERE bot=? AND chat=? AND uid=?",
                (*scope, str(uid)),
            ).fetchone()
            return row["loss"] if row else 0

    def acknowledge_loss(self, scope, uid, loss):
        with self.game.transaction() as db:
            # 只扣除本次已告知的数量，保留发送期间新产生的缩水量。
            db.execute(
                "UPDATE shrink_pending SET loss=MAX(0,loss-?) WHERE bot=? AND chat=? AND uid=?",
                (loss, *scope, str(uid)),
            )
            db.execute(
                "DELETE FROM shrink_pending WHERE bot=? AND chat=? AND uid=? AND loss=0",
                (*scope, str(uid)),
            )

    def claim(self, bot, limit=10):
        now = self.game.clock()
        with self.game.transaction() as db:
            rows = db.execute(
                "SELECT * FROM deletion_jobs WHERE bot=? AND state='pending' AND due<=? AND lease_until<=? ORDER BY due,id LIMIT ?",
                (str(bot), now, now, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                token = secrets.token_hex(12)
                db.execute(
                    "UPDATE deletion_jobs SET lease=?,lease_until=? WHERE id=?",
                    (token, now + LEASE_SECONDS, row["id"]),
                )
                claimed.append(dict(row) | {"lease": token})
            return claimed

    def is_claimed(self, job):
        with self.game.transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM deletion_jobs WHERE id=? AND state='pending' AND lease=?",
                    (job["id"], job["lease"]),
                ).fetchone()
                is not None
            )

    def finish(self, job, state="done", delay=None):
        with self.game.transaction() as db:
            if delay is not None:
                db.execute(
                    "UPDATE deletion_jobs SET attempts=attempts+1,due=?,lease=NULL,lease_until=0 WHERE id=? AND lease=? AND state='pending'",
                    (self.game.clock() + delay, job["id"], job["lease"]),
                )
            else:
                db.execute(
                    "UPDATE deletion_jobs SET state=?,lease=NULL,lease_until=0 WHERE id=? AND lease=? AND state='pending'",
                    (state, job["id"], job["lease"]),
                )

    def prune(self):
        now = self.game.clock()
        with self.game.transaction() as db:
            db.execute("DELETE FROM message_links WHERE created<?", (now - 48 * 3600,))
            db.execute("DELETE FROM deletion_jobs WHERE state!='pending' AND due<?", (now - 86400,))
            db.execute(
                "UPDATE deletion_jobs SET state='expired',lease=NULL,lease_until=0 WHERE created<=? AND state='pending'",
                (now - 48 * 3600,),
            )

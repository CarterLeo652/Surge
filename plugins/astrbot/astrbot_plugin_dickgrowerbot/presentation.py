"""按原 Rust handler 的拼接顺序呈现 locales/zh.yml。"""

import math
from html import escape

from .localization import tr


def mention(uid, name):
    return f'<a href="tg://user?id={int(uid)}">{escape(name)}</a>'


def time_left(due, now):
    minutes = int(max(0, due - now)) // 60
    return tr("titles.time_till_next_day.some", hours=minutes // 60, minutes=minutes % 60)


def percentage(wins, total):
    return f"{math.floor(wins / total * 100 + 0.5) if total else 0}%"


def perks_text(result, settings):
    if result.get("base", result["change"]) == result["change"]:
        return ""
    lines = []
    for key, value in [
        ("help-pussies", result.get("help_bonus", 0)),
        ("loan-payout", -result.get("payout", 0)),
        ("streak", result.get("bonus", 0)),
    ]:
        if not value:
            continue
        name = tr(f"titles.perks.{key}.name")
        note = ""
        if key == "streak":
            note = tr("titles.perks.streak.note", days=result["streak"])
            next_grade = next(
                (grade for grade in settings.grades if grade > result["streak"]), None
            )
            if next_grade is not None:
                note += "; " + tr(
                    "titles.perks.streak.next_grade", next_day=next_grade, next_bonus=value + 1
                )
        lines.append(f"— {name} ({value:+}, {note})" if note else f"— {name} ({value:+})")
    return "\n\n" + tr("titles.perks.top_line") + ":\n" + "\n".join(lines)


def grow_text(result, settings, now):
    direction = "shrunk" if result["change"] < 0 else "grown"
    body = tr(
        "commands.grow.result",
        event=tr(f"commands.grow.direction.{direction}"),
        incr=abs(result["change"]),
        length=result["length"],
    )
    body += "\n" + tr("commands.grow.position", pos=result["rank"])
    return body + perks_text(result, settings) + time_left(result["next"], now)


def challenge_text(result, settings=None):
    return tr("commands.pvp.results.start", name=escape(result["name"]), bet=result["bet"])


def battle_text(r):
    body = tr(
        "commands.pvp.results.finish",
        winner_name=escape(r["winner_name"]),
        winner_length=r["winner_length"],
        loser_length=r["loser_length"],
        bet=r["bet"],
    )
    body += "\n\n" + tr(
        "commands.pvp.results.position.winner", name=escape(r["winner_name"]), pos=r["winner_rank"]
    )
    body += "\n" + tr(
        "commands.pvp.results.position.loser", name=escape(r["loser_name"]), pos=r["loser_rank"]
    )
    if r.get("payout", 0) > 0:
        body += "\n\n" + tr("commands.pvp.results.withheld", payout=r["payout"])
    body += "\n\n" + tr(
        "commands.pvp.results.stats.text",
        winner_win_rate=percentage(r.get("winner_wins", 0), r.get("winner_battles", 0)),
        loser_win_rate=percentage(r.get("loser_wins", 0), r.get("loser_battles", 0)),
        winner_win_streak=r.get("winner_streak", 0),
        winner_win_streak_max=r.get("winner_max_streak", 0),
    )
    if r.get("lost_win_streak", 0) > 1:
        body += "\n" + tr(
            "commands.pvp.results.stats.lost_win_streak", lost_win_streak=r["lost_win_streak"]
        )
    return body


def stats_text(r, settings):
    body = tr("commands.stats.length", length=r["length"], pos=r["rank"])
    if settings.streak_enabled and r.get("streak_day"):
        body += "\n" + tr("commands.stats.streak", current=r["streak"], max=r["max_streak"])
    body += "\n\n" + tr(
        "commands.stats.pvp",
        win_rate=percentage(r["wins"], r["wins"] + r["losses"]),
        battles=r["wins"] + r["losses"],
        wins=r["wins"],
        win_streak=r["max_win_streak"],
        acquired=r["acquired"],
        lost=r["lost"],
    )
    return body


def top_text(r, viewer):
    if not r["rows"]:
        return tr("commands.top.empty")
    lines = []
    for index, row in enumerate(r["rows"], r["page"] * r["size"] + 1):
        name = escape(row["name"])
        if row["uid"] == str(viewer):
            name = f"<u>{name}</u>"
        line = tr("commands.top.line", n=index, name=name, length=row["length"])
        if row["inactive"]:
            line += " [~]"
        elif row["can_grow"]:
            line += " [+]"
        lines.append(line)
    tail = tr("commands.top.ending")
    if any(row["inactive"] for row in r["rows"]):
        tail += "\n" + tr("commands.top.ending_inactive", days=7)
    return tr("commands.top.title") + "\n\n" + "\n".join(lines) + "\n\n" + tail


def dod_text(r, settings, now):
    if r["status"] == "no_candidates":
        return tr("commands.dod.no_candidates")
    if r["status"] == "already_chosen":
        body = tr("commands.dod.already_chosen", name=escape(r["name"]))
    else:
        body = tr(
            "commands.dod.result",
            uid=int(r["uid"]),
            name=escape(r["name"]),
            growth=r["change"],
            length=r["length"],
        )
        body += "\n" + tr("commands.dod.position", pos=r["rank"]) + perks_text(r, settings)
    return body + time_left(r["next"], now)


def loan_text(r):
    if r["status"] == "debt":
        return tr("commands.loan.debt", debt=r["debt"])
    if r["status"] == "confirm":
        return tr(
            "commands.loan.confirmation.text",
            debt=r["amount"],
            payout_percentage=f"{r['ratio'] * 100:.2f}%",
        )
    if r["status"] == "not_eligible":
        return tr("commands.loan.errors.positive_length")
    key = "success" if r["status"] == "granted" else r["status"]
    return tr(f"commands.loan.callback.{key}")


def cleanup_delay(minutes):
    if not minutes:
        return tr("commands.cleanup.delays.kept")
    key = "hours" if minutes % 60 == 0 else "minutes"
    return tr(f"commands.cleanup.delays.{key}", count=minutes // 60 if key == "hours" else minutes)


def cleanup_text(settings):
    lines = []
    for kind, minutes in settings.items():
        group = tr(f"commands.cleanup.groups.{kind}")
        key = "cleaned" if minutes else "kept"
        lines.append(tr(f"commands.cleanup.state.{key}", group=group, delay=cleanup_delay(minutes)))
    return tr("commands.cleanup.state.header") + "\n\n" + "\n".join(lines)

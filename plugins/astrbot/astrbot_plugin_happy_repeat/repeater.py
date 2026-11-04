"""Pure repeat detection logic, independent of AstrBot and Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Run:
    key: str
    last_at: float
    last_message_id: str
    members: set[str] = field(default_factory=set)
    sent: bool = False


class RepeatTracker:
    """Recognize a run of identical messages by distinct group members."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}

    def reset(self, group: str) -> None:
        self._runs.pop(group, None)

    def observe(
        self,
        group: str,
        key: str,
        member: str,
        message_id: str,
        now: float,
        *,
        min_members: int,
        window_seconds: int,
        require_adjacent_ids: bool = False,
    ) -> bool:
        run = self._runs.get(group)
        if run is not None and run.last_message_id == message_id:
            return False
        # Telegram message IDs increase within a chat. A gap means there was
        # another message even if its event has not reached this plugin yet
        # (for example, while the adapter awaits Telegram photo metadata).
        id_gap = (
            require_adjacent_ids
            and run is not None
            and message_id.isdecimal()
            and run.last_message_id.isdecimal()
            and int(message_id) != int(run.last_message_id) + 1
        )
        if (
            run is None
            or run.key != key
            or now - run.last_at > window_seconds
            or now < run.last_at
            or id_gap
        ):
            self._runs[group] = _Run(key, now, message_id, {member})
            return False

        run.last_at = now
        run.last_message_id = message_id
        run.members.add(member)
        if not run.sent and len(run.members) >= min_members:
            run.sent = True
            return True
        return False

    def send_failed(self, group: str, key: str) -> None:
        run = self._runs.get(group)
        if run is not None and run.key == key:
            run.sent = False

    def prune(self, now: float, window_seconds: int) -> None:
        for group, run in list(self._runs.items()):
            if now - run.last_at > window_seconds:
                del self._runs[group]

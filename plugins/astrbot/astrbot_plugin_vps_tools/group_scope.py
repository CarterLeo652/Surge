"""Group allow/deny semantics shared by all VPS lookup commands."""

from __future__ import annotations


def group_allowed(group_id: str | None, enabled_groups: list, disabled_groups: list,
                  *, enabled: bool = True) -> bool:
    """Apply the plugin switch, then group and Telegram topic rules."""
    if not enabled:
        return False
    if not group_id:
        return True
    group = str(group_id)
    chat_id = group.split("#", 1)[0]
    enabled = {str(value) for value in enabled_groups}
    disabled = {str(value) for value in disabled_groups}
    if chat_id in disabled or group in disabled:
        return False
    return not enabled or chat_id in enabled or group in enabled

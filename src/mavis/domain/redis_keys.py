"""Per-user Redis key names: mavis:<area>:u<uid>:..., so deletion can SCAN one user's keys (spec 6.1)."""

from __future__ import annotations


def user_key(area: str, user_id: int, *parts: str) -> str:
    return ":".join(["mavis", area, f"u{int(user_id)}", *parts])


def user_pattern(user_id: int) -> str:
    return f"mavis:*:u{int(user_id)}:*"

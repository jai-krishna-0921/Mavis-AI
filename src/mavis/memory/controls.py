"""Per-user learning controls kept in the user's settings: which connectors are paused, and the daily
budget for learning from the user's own words.

Pausing a connector stops NEW learning from it (records, interaction signals, calendar occurrences); what was
already learned stays until it is forgotten (`vault.forget_source`).
"""

from __future__ import annotations

from mavis.domain import timeutil
from mavis.store.repo import users

KEY = "learning"
CONNECTORS = ("gmail", "slack", "calendar")
AUTHORED_LEARN_PER_DAY = 40  # LEARN jobs from the user's own mail and messages, per user per day
MIN_AUTHORED_WORDS = 5  # shorter own messages feed the style measure but are not worth an extraction


async def paused(user_id: int) -> set[str]:
    raw = (await users.get_state(user_id)).get(KEY) or {}
    return {str(c) for c in raw.get("paused", [])} & set(CONNECTORS)


async def set_paused(user_id: int, connector: str, value: bool) -> set[str]:
    """Pause or resume learning from one connector ("gmail", "slack", "calendar")."""
    if connector not in CONNECTORS:
        raise ValueError(f"unknown connector: {connector}")

    def change(cur: dict) -> dict:
        now = {str(c) for c in cur.get("paused", [])}
        now = now | {connector} if value else now - {connector}
        return {**cur, "paused": sorted(now)}

    return set((await users.modify_nested(user_id, KEY, change)).get("paused", []))


async def take_authored_budget(user_id: int) -> bool:
    """Spend one of today's LEARN slots for the user's own words. False when the day's budget is used up."""
    today = f"{timeutil.now():%Y-%m-%d}"
    granted = False

    def change(cur: dict) -> dict:
        nonlocal granted
        used = cur.get("authored") or {}
        n = int(used.get("n", 0)) if used.get("day") == today else 0
        granted = n < AUTHORED_LEARN_PER_DAY
        return {**cur, "authored": {"day": today, "n": n + (1 if granted else 0)}}

    await users.modify_nested(user_id, KEY, change)
    return granted

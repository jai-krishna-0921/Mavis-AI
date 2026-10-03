"""Bounded per-kind threshold offsets learned from button feedback, kept in users.state["attention"]."""

from __future__ import annotations

from typing import Any

from mavis.attention.schema import EmailKind, Feedback
from mavis.store.repo import users

STATE_KEY = "attention"
OFFSET_STEP: dict[Feedback, float] = {
    Feedback.MUTE: 0.1,
    Feedback.ALWAYS: -0.1,
    Feedback.CONFIRMED: 0.03,
    Feedback.DISPUTED: -0.05,
}
OFFSET_MIN, OFFSET_MAX = -0.2, 0.3
SENSITIVE_MAX = 0.1  # money_movement and security can be tuned down only a little
SENSITIVE_KINDS = frozenset({EmailKind.MONEY_MOVEMENT, EmailKind.SECURITY})


def learn(offsets: dict[str, float], kind: str, feedback: Feedback) -> dict[str, float]:
    if kind not in {k.value for k in EmailKind}:
        return dict(offsets)  # arbitrary keys would grow users.state without bound
    new = dict(offsets)
    top = SENSITIVE_MAX if kind in SENSITIVE_KINDS else OFFSET_MAX
    new[kind] = round(max(OFFSET_MIN, min(top, new.get(kind, 0.0) + OFFSET_STEP[feedback])), 3)
    return new


def offset_for(state: dict[str, Any], kind: str) -> float:
    return float((state.get("offsets") or {}).get(kind, 0.0))


class Thresholds:
    """Read-modify-write of one users.state key; other keys (polling cursors) are left alone."""

    async def load(self, user_id: int) -> dict[str, Any]:
        return dict((await users.get_state(user_id)).get(STATE_KEY) or {})

    async def patch(self, user_id: int, **kv: Any) -> dict[str, Any]:
        state = await self.load(user_id)
        state.update(kv)
        await users.update_state(user_id, {STATE_KEY: state})
        return state

    async def offset(self, user_id: int, kind: str) -> float:
        return offset_for(await self.load(user_id), kind)

    async def learn(self, user_id: int, kind: str, feedback: Feedback) -> dict[str, float]:
        offsets = learn((await self.load(user_id)).get("offsets") or {}, kind, feedback)
        await self.patch(user_id, offsets=offsets)
        return offsets

    async def urgent_used(self, user_id: int, local_day: str) -> bool:
        return (await self.load(user_id)).get("urgent_day") == local_day

    async def mark_urgent(self, user_id: int, local_day: str) -> None:
        await self.patch(user_id, urgent_day=local_day)

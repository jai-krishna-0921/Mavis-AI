"""Bounded per-kind threshold offsets learned from button feedback, kept in users.state["attention"]."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mavis.attention.schema import EmailKind, Feedback
from mavis.store.repo import users
from mavis.worker.locks import lock

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


def state_lock(user_id: int):
    """One writer at a time for users.state["attention"] (in-process FIFO, Redis across workers). A key of
    its own, not `user:{id}`: button turns already hold that one, so this cannot self-deadlock."""
    return lock(f"attention-state:{user_id}")


@dataclass
class UrgentSlot:
    free: bool  # no urgency-5 message has gone out yet on this local day
    claimed: bool = False

    def claim(self) -> None:
        self.claimed = True


class Thresholds:
    """Read-modify-write of one users.state key under the per-user state lock, so concurrent writers
    (ingest marking the urgent day, a button press learning an offset) never lose each other's update."""

    async def load(self, user_id: int) -> dict[str, Any]:
        return dict((await users.get_state(user_id)).get(STATE_KEY) or {})

    async def _patch(self, user_id: int, **kv: Any) -> dict[str, Any]:
        state = await self.load(user_id)
        state.update(kv)
        await users.update_state(user_id, {STATE_KEY: state})
        return state

    async def patch(self, user_id: int, **kv: Any) -> dict[str, Any]:
        async with state_lock(user_id):
            return await self._patch(user_id, **kv)

    async def offset(self, user_id: int, kind: str) -> float:
        return offset_for(await self.load(user_id), kind)

    async def learn(self, user_id: int, kind: str, feedback: Feedback) -> dict[str, float]:
        async with state_lock(user_id):
            offsets = learn((await self.load(user_id)).get("offsets") or {}, kind, feedback)
            await self._patch(user_id, offsets=offsets)
        return offsets

    async def urgent_used(self, user_id: int, local_day: str) -> bool:
        return (await self.load(user_id)).get("urgent_day") == local_day

    async def mark_urgent(self, user_id: int, local_day: str) -> None:
        await self.patch(user_id, urgent_day=local_day)

    @asynccontextmanager
    async def urgent_slot(self, user_id: int, local_day: str) -> AsyncIterator[UrgentSlot]:
        """Check, send and mark the one urgency-5 message per local day as one step under the state lock:
        two concurrent asks can never both bypass quiet hours. The day is marked only if the caller
        claims the slot (the message actually went out)."""
        async with state_lock(user_id):
            slot = UrgentSlot(free=(await self.load(user_id)).get("urgent_day") != local_day)
            yield slot
            if slot.free and slot.claimed:
                await self._patch(user_id, urgent_day=local_day)

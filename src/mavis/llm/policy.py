"""The slot policy, in one place (spec 8): who may start, who ranks ahead. Pure functions of a snapshot of the
limiter, used by the in-process `_Limiter` and mirrored line for line by the Redis Lua script
(`limiter_lua.TRY_ACQUIRE`); tests/llm/test_limiter_parity.py drives both with the same scenarios.

Rules (e2efix-capacity wins):
  * best_effort never takes the LAST free slot, uses at most size // share slots (min 1), and starts only in a
    lull (nothing else in flight, no higher-rank waiter, none started or ended lately). When nobody else runs,
    a backlog may drain on every slot but one. Otherwise it QUEUES.
  * with ONE slot there is nothing to share: it fails fast while other work is queued or ran within the grace.
  * a background waiter this old ranks with interactive; a best_effort waiter this old ranks with background.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timings:
    share: int = 3  # best_effort may use at most size // share slots (min 1)
    grace_s: float = 20.0  # single-slot only: how long after other work best_effort keeps off the slot
    quiet_s: float = 5.0
    background_aging_s: float = 30.0
    best_effort_aging_s: float = 45.0


@dataclass(frozen=True)
class Snapshot:
    size: int
    free: int
    be_inflight: int
    higher_waiting: bool  # an interactive or background caller is queued
    since_last_used_s: float  # since a non-best_effort call last took a slot or finished


def best_effort_cap(size: int, share: int) -> int:
    return max(1, size // share)


def work_active(s: Snapshot, t: Timings) -> bool:
    return s.higher_waiting or s.since_last_used_s < t.grace_s


def single_slot_blocked(s: Snapshot, t: Timings) -> bool:
    return s.size == 1 and work_active(s, t)


def best_effort_may_start(s: Snapshot, t: Timings) -> bool:
    others = s.size - s.free - s.be_inflight  # non-best_effort calls in flight
    cap = best_effort_cap(s.size, t.share)
    if others <= 0:
        cap = max(cap, s.size - 1)
    if s.free <= 0 or s.be_inflight >= cap:
        return False
    if s.size == 1:
        return not work_active(s, t)
    quiet = s.since_last_used_s >= t.quiet_s and not s.higher_waiting
    return s.free >= 2 and others == 0 and quiet


def effective_rank(rank: int, waited_s: float, t: Timings) -> int:
    if rank == 1 and waited_s >= t.background_aging_s:
        return 0
    if rank == 2 and waited_s >= t.best_effort_aging_s:
        return 1
    return rank

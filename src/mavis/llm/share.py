"""best_effort capacity policy: when may background work (memory extraction, summaries) use an LLM slot?

ONE pure function, `best_effort_verdict`, with no clock, no asyncio and no I/O: the in-process limiter
(mavis.llm.models._Limiter) and any shared limiter (the multi-user Redis one) feed it their counters and
act on the verdict, so the policy cannot drift between them.

The policy, for N slots (the examples are N = 3, Ollama Pro):

1. GUARANTEED SHARE. best_effort may start when it holds fewer than max(1, N // 3) slots, at least two
   slots are free (so a reply that arrives next finds one) and no interactive caller is queued. Recent
   chat activity does NOT block it: with many users a quiet moment never comes, and LEARN is the only way
   the user's facts, loops and profile get learned.
2. BOUNDED DUTY. Starts are at least MIN_START_GAP_S apart and a best_effort call is clamped to
   TIMEOUT_S with no cooldown hold (the caller enforces both), so best_effort occupies at most about
   a third of the capacity and a reply waits at worst for the remainder of one chat call.
3. LULL. With nothing else in flight and no chat call started or ended for LULL_QUIET_S, the cap rises
   to max(2, 2N // 3) to drain a backlog.
4. STARVATION ESCAPE. A waiter older than ESCAPE_AFTER_S may take the LAST free slot, only if no
   interactive caller is queued and no chat call started within ESCAPE_CHAT_GAP_S, and its call is
   clamped to ESCAPE_TIMEOUT_S, so the reply that might arrive next waits only briefly.
5. (job layer, mavis.memory.jobs) queued LEARN texts of one user are coalesced once the queue grows.
"""

from __future__ import annotations

from dataclasses import dataclass

TIMEOUT_S = 20.0  # a best_effort call's own timeout (the FAST tier's)
MIN_START_GAP_S = 2.0
LULL_QUIET_S = 5.0
ESCAPE_AFTER_S = 60.0
ESCAPE_CHAT_GAP_S = 2.0
ESCAPE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class SlotState:
    """What the limiter knows at one instant. Ages are seconds since the event (inf: it never happened)."""

    size: int  # N
    free: int
    best_effort_inflight: int
    interactive_waiting: bool  # an interactive or background caller is queued
    chat_in_flight: int  # non-best_effort calls currently holding a slot
    since_chat_activity_s: float  # since a chat/task call last started or ended
    since_chat_start_s: float  # since a chat/task call last started
    since_best_effort_start_s: float
    waited_s: float = 0.0  # how long THIS best_effort caller has queued


@dataclass(frozen=True)
class Verdict:
    start: bool
    timeout_s: float = 20.0  # the call's timeout if it starts (best_effort_verdict sets it explicitly)


NO = Verdict(False)


def share_cap(size: int, lull: bool) -> int:
    return max(2, 2 * size // 3) if lull else max(1, size // 3)


def best_effort_verdict(s: SlotState) -> Verdict:
    """May a best_effort call start now? See the module docstring for the rules."""
    if s.free <= 0 or s.interactive_waiting or s.since_best_effort_start_s < MIN_START_GAP_S:
        return NO
    lull = s.chat_in_flight == 0 and s.since_chat_activity_s >= LULL_QUIET_S
    if s.best_effort_inflight >= share_cap(s.size, lull):
        return NO
    if s.free >= 2:
        return Verdict(True, TIMEOUT_S)
    if s.waited_s >= ESCAPE_AFTER_S and s.since_chat_start_s >= ESCAPE_CHAT_GAP_S:
        return Verdict(True, ESCAPE_TIMEOUT_S)  # the starvation escape: the last slot, briefly
    return NO

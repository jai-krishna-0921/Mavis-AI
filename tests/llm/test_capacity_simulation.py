"""D5: LLM capacity under realistic load (3 slots, multi-call chat turns, LEARN, reasoner).

Time is scaled: 1 simulated second is SCALE real seconds, so a 20 s grace window is 1 s here. The
limiter and the LEARN job layer run for real; only the model call is a sleep. The live E2E showed
LEARN calls refused as "reserved for interactive work" for the whole session because chat calls were
never more than the grace window apart, while interactive work itself was nowhere near saturating 3 slots.
"""

import asyncio
import random
import time

import pytest

from mavis.domain.errors import LLMError
from mavis.llm import models, share

SCALE = 0.02  # real seconds per simulated second
CHAT_CALL_MAX_S = 6 * SCALE  # the longest simulated chat call (the 3 user tests use 2-6 s calls)


def s(sim_seconds: float) -> float:
    return sim_seconds * SCALE


@pytest.fixture(autouse=True)
def _sim(settings, monkeypatch) -> None:
    settings.llm_max_concurrency = 3
    settings.llm_timeout_cooldown_s = s(45)
    monkeypatch.setattr(models, "INTERACTIVE_GRACE_S", s(20))
    monkeypatch.setattr(models, "BACKGROUND_AGING_S", s(30))
    monkeypatch.setattr(models, "BEST_EFFORT_AGING_S", s(45))
    monkeypatch.setattr(models, "BEST_EFFORT_TICK_S", s(1))
    for name, sim_seconds in (("TIMEOUT_S", 20), ("MIN_START_GAP_S", 2), ("LULL_QUIET_S", 5),
                              ("ESCAPE_AFTER_S", 60), ("ESCAPE_CHAT_GAP_S", 2), ("ESCAPE_TIMEOUT_S", 10)):
        monkeypatch.setattr(share, name, s(sim_seconds))
    models._limiters.clear()
    models._ollama.reset()


async def _call(priority: str, seconds: float, waits: list[float] | None = None) -> None:
    started = time.monotonic()

    async def op() -> None:
        if waits is not None:
            waits.append(time.monotonic() - started)
        await asyncio.sleep(s(seconds))

    budget = models.INTERACTIVE_DEADLINE_S if priority == "interactive" else models.BACKGROUND_DEADLINE_S
    await models._call(op, priority, models._Deadline(s(budget)))


async def _chat_turn(rng: random.Random, waits: list[float], call_s: tuple[float, float] = (2, 6)) -> None:
    """A turn: 3-4 sequential model calls (route, tool-calling, answer) with tool waits in between;
    the slot is held only while a call is in flight."""
    for _ in range(rng.randint(3, 4)):
        await _call("interactive", rng.uniform(*call_s), waits)
        await asyncio.sleep(s(rng.uniform(0.5, 4)))  # tool wait: no slot held


async def _learn_until_done(attempts: list[int], finished: list[float], t0: float, call_s: float,
                            retry_after_s: float) -> None:
    """The LEARN job layer: a refused call is parked and retried, never dropped."""
    n = 0
    while True:
        n += 1
        try:
            await _call("best_effort", call_s)
            break
        except LLMError:
            await asyncio.sleep(s(retry_after_s))
    attempts.append(n)
    finished.append(time.monotonic() - t0)


async def _load(users: int, turns: int, learns_per_turn: bool, seed: int):
    rng = random.Random(seed)
    waits: list[float] = []
    attempts: list[int] = []
    finished: list[float] = []
    t0 = time.monotonic()
    learn_tasks: list[asyncio.Task] = []

    async def user_session() -> None:
        for _ in range(turns):
            await _chat_turn(rng, waits)
            if learns_per_turn:
                learn_tasks.append(asyncio.create_task(
                    _learn_until_done(attempts, finished, t0, call_s=rng.uniform(3, 6), retry_after_s=15)))
            await asyncio.sleep(s(rng.uniform(1, 5)))

    async def reasoner() -> None:
        for _ in range(turns):
            try:
                await _call("background", rng.uniform(4, 9))
            except LLMError:
                pass
            await asyncio.sleep(s(rng.uniform(3, 8)))

    await asyncio.gather(*(user_session() for _ in range(users)), reasoner())
    chat_done = time.monotonic() - t0
    try:
        await asyncio.wait_for(asyncio.gather(*learn_tasks), s(600))
    except TimeoutError:
        lim = next(iter(models._limiters.values()))
        pytest.fail(f"LEARN never finished: free={lim._free} be={lim._be_inflight} "
                    f"waiters={[(w.rank, w.fut.done()) for w in lim._waiters]} "
                    f"unfinished={sum(not t.done() for t in learn_tasks)} of {len(learn_tasks)}")
    return waits, attempts, finished, chat_done


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_learn_completes_while_chat_is_busy_and_chat_latency_stays_bounded(seed) -> None:
    base_waits, *_ = await _load(users=3, turns=6, learns_per_turn=False, seed=seed)
    models._limiters.clear()
    waits, attempts, finished, chat_done = await _load(users=3, turns=6, learns_per_turn=True, seed=seed)

    # LEARN does not wait for the chat to go quiet, and is never refused in a loop
    assert max(finished) < chat_done + s(90), "LEARN starved until the conversation ended"
    assert max(attempts) <= 2, f"LEARN refused repeatedly: {attempts}"
    # chat latency protected: background load adds at most one chat call to a reply's wait for a slot
    assert p95(waits) <= p95(base_waits) + CHAT_CALL_MAX_S


async def _crowd(users: int, seed: int, learn: bool):
    """`users` people, each 3 turns of 3-4 chat calls of 1-4 s (FAST model), a turn every 100-200 s (about one
    per 2 minutes): 30 users keep 3 slots about half busy."""
    rng = random.Random(seed)
    waits: list[float] = []
    latencies: list[float] = []

    async def learn_job(call_s: float) -> None:
        t0 = time.monotonic()
        while True:
            try:
                await _call("best_effort", call_s)
                break
            except LLMError:
                await asyncio.sleep(s(15))
        latencies.append(time.monotonic() - t0)

    async def user_session() -> None:
        await asyncio.sleep(s(rng.uniform(0, 100)))
        for _ in range(3):
            await _chat_turn(rng, waits, call_s=(1, 4))
            if learn:
                learn_tasks.append(asyncio.create_task(learn_job(rng.uniform(3, 6))))
            await asyncio.sleep(s(rng.uniform(100, 200)))

    learn_tasks: list[asyncio.Task] = []
    await asyncio.gather(*(user_session() for _ in range(users)))
    await asyncio.gather(*learn_tasks)
    return waits, latencies


@pytest.mark.parametrize("users", [10, 30])
async def test_many_users_every_learn_completes_within_three_minutes_and_chat_is_not_slowed(users) -> None:
    base_waits, _ = await _crowd(users, seed=users, learn=False)
    models._limiters.clear()
    waits, latencies = await _crowd(users, seed=users, learn=True)
    assert len(latencies) == users * 3
    assert max(latencies) <= s(180), f"slowest LEARN took {max(latencies) / SCALE:.0f} simulated seconds"
    assert p95(waits) <= p95(base_waits) + CHAT_CALL_MAX_S

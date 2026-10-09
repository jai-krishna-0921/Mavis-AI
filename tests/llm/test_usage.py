from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from mavis.llm import usage
from mavis.llm.context import bind_user
from mavis.store.repo import usage as repo
from mavis.store.repo import users


@pytest.mark.parametrize("model,pin,pout,micros", [("deepseek-v4.1-flash", 1_000_000, 0, 300_000),
                                                   ("glm-5.3", 1000, 1000, 5_800),
                                                   ("never-seen-model", 2000, 500, 5_000)])
def test_cost_from_the_price_table(settings, model, pin, pout, micros):
    assert usage.cost_micros(model, pin, pout) == micros


def _result(model: str, i: int, o: int) -> LLMResult:
    usage_md = {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}
    msg = AIMessage(content="x", usage_metadata=usage_md,
                    response_metadata={"model_name": model})
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


async def test_concurrent_calls_book_spend_to_their_own_users(db, fake_redis, settings, clock):
    clock.set(datetime(2026, 10, 8, 23, 30, tzinfo=UTC))
    ids = []
    for chat, tz in ((5001, "Asia/Tokyo"), (7302, "America/Bogota"), (9944, "Europe/Lisbon")):
        u, _ = await users.get_or_create_by_chat(chat, "x")
        await users.update(u.id, timezone=tz)
        ids.append(u.id)
    cb = usage.UsageCallback()

    async def one(uid: int, n: int) -> None:
        with bind_user(uid, "chat"):
            for _ in range(n):
                await cb.on_llm_end(_result("glm-5.3", 1000, 1000), run_id=None)

    await asyncio.gather(one(ids[0], 3), one(ids[1], 1), one(ids[2], 2))
    assert [await usage.spend_micros_today(u) for u in ids] == [3 * 5800, 5800, 2 * 5800]
    tokyo_rows = await repo.daily(ids[0], datetime(2026, 10, 9).date())  # 08:30 next day in Tokyo
    assert tokyo_rows and tokyo_rows[0].calls == 3 and tokyo_rows[0].purpose == "chat"


async def test_calls_without_a_user_go_to_system(db, settings):
    await usage.UsageCallback().on_llm_end(_result("deepseek-v4.1-flash", 10, 10), run_id=None)
    assert sum(r.calls for r in await repo.all_for_user(0)) == 1


@pytest.mark.parametrize("etype,purpose", [("user_message", "chat"), ("button_pressed", "chat"),
                                           ("email_received", "attention"), ("wakeup", "initiative"),
                                           ("task_completed", "other")])
def test_event_purposes(etype, purpose):
    from mavis.domain.events import Event, EventType
    from mavis.store.db import utcnow
    from mavis.worker.runner import _purpose

    ev = Event(id="e", user_id=1, type=EventType(etype), occurred_at=utcnow(), source="t")
    assert _purpose(ev) == purpose


@pytest.mark.parametrize("kind,purpose", [("run_task", "task"), ("resume_task", "task"), ("learn", "memory"),
                                          ("consolidate", "memory"), ("poll_provider", "other")])
def test_job_purposes(kind, purpose):
    from mavis.domain.events import Job, JobKind
    from mavis.worker.runner import _job_purpose

    assert _job_purpose(Job(id="j", user_id=1, kind=JobKind(kind))) == purpose


async def test_a_handler_runs_with_its_user_bound(db, settings):
    from mavis.domain.events import Event, EventType
    from mavis.llm.context import llm_purpose, llm_user_id
    from mavis.store.db import utcnow
    from mavis.worker import runner

    seen = []

    async def handler(event):
        seen.append((llm_user_id.get(), llm_purpose.get()))

    runner.clear_handlers()
    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    u, _ = await users.get_or_create_by_chat(5555, "x")
    await runner.handle_event(Event(id="u:1", user_id=u.id, type=EventType.USER_MESSAGE,
                                    occurred_at=utcnow(), source="t", payload={"text": "hi"}))
    runner.clear_handlers()
    assert seen == [(u.id, "chat")] and llm_user_id.get() is None


async def test_spend_counter_falls_back_to_the_database_without_redis(db, settings):
    u, _ = await users.get_or_create_by_chat(5556, "x")
    with bind_user(u.id, "chat"):
        await usage.UsageCallback().on_llm_end(_result("glm-5.3", 1000, 0), run_id=None)
    assert await usage.spend_micros_today(u.id) == 1400

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select

from mavis.attention.baselines import Baselines
from mavis.attention.feedback import FeedbackHandler
from mavis.attention.index import AttentionIndex
from mavis.attention.learning import Thresholds
from mavis.attention.schema import Feedback
from mavis.attention.speaker import can_mute
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind
from mavis.domain.wakeups import WakeupKind
from mavis.loops.service import LoopService
from mavis.store.db import Session
from mavis.store.models import OutboxMessage
from mavis.store.repo import attention as repo
from mavis.timers.service import WakeupService
from tests.attention.test_speaker import MONEY, make_obs
from tests.memory.fakes import HashEmbedder

NOON = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)


@pytest.fixture
async def handler(recording_bus):
    client = AsyncQdrantClient(location=":memory:")
    index = AttentionIndex(client, HashEmbedder())
    h = FeedbackHandler(
        loops=LoopService(recording_bus),
        wakeups=WakeupService(),
        baselines=Baselines(),
        index=index,
        thresholds=Thresholds(),
    )
    h.index = index
    yield h
    await client.close()


def press(user_id: int, data: str, update: int) -> Event:
    return Event(
        id=f"tg:update:{update}",
        user_id=user_id,
        type=EventType.BUTTON_PRESSED,
        occurred_at=NOON,
        source="telegram",
        payload={"data": data},
        trust=Trust.USER,
    )


async def texts() -> list[str]:
    async with Session() as s:
        return list(await s.scalars(select(OutboxMessage.text).order_by(OutboxMessage.id)))


async def ask_obs(user_id: int):
    return await make_obs(
        user_id,
        "m1",
        kind="money_movement",
        verdict="ask",
        urgency=5,
        received_at=NOON,
        summary="money movement from examplebank: debit alert",
        facts={"money": MONEY, "baselined": False},
        reasons=["x"],
    )


async def test_no_opens_trusted_loop_followup_and_replies(user, clock, handler):
    clock.set(NOON)
    obs = await ask_obs(user.id)
    event = press(user.id, f"at:n:{obs.id}", 50)
    await handler.on_button(event, event.payload["data"])
    await handler.on_button(event, event.payload["data"])  # bus retry: nothing doubles
    loops = [lp for lp in await handler._loops.active(user.id) if lp.kind is LoopKind.CONCERN]
    assert len(loops) == 1 and loops[0].importance == 5 and loops[0].source == "tg:update:50"
    assert "₹48,000" in loops[0].title and "ramesh" not in loops[0].title.lower()
    wakeups = await WakeupService().pending(user.id, WakeupKind.AGENT)
    assert len(wakeups) == 1 and wakeups[0].loop_id == loops[0].id
    assert wakeups[0].due_at == NOON + timedelta(hours=2)
    sent = await texts()
    assert len(sent) == 2 and "banking or payment app directly" in sent[0] and "2 hours" in sent[1]
    assert (await repo.get(obs.id)).feedback == "disputed"
    assert await handler._thresholds.offset(user.id, "money_movement") == -0.05
    assert (await Baselines().snapshot(user.id, "INR", "ramesh kumar", "upi")).overall.count == 0


async def test_yes_records_held_debit_once_and_remembers(user, clock, handler):
    clock.set(NOON)
    obs = await ask_obs(user.id)
    for update in (60, 61):
        event = press(user.id, f"at:y:{obs.id}", update)
        await handler.on_button(event, event.payload["data"])
    snap = await Baselines().snapshot(user.id, "INR", "ramesh kumar", "upi")
    assert snap.counterparty.count == 1
    row = await repo.get(obs.id)
    assert row.feedback == "confirmed" and row.facts["baselined"] is True
    hits = await handler.index.prefs_near(user.id, await handler.index.embed(row.summary), 0.8)
    assert hits and hits[0].sentiment == "confirmed"
    assert "noted" in (await texts())[0].lower()


async def test_mute_stores_preference_and_offset(user, clock, handler):
    clock.set(NOON)
    obs = await make_obs(
        user.id,
        "m2",
        kind="account_update",
        verdict="notify",
        urgency=3,
        received_at=NOON,
        summary="account update from exampleshop: your plan renews",
    )
    event = press(user.id, f"at:m:{obs.id}", 70)
    await handler.on_button(event, event.payload["data"])
    hits = await handler.index.prefs_near(user.id, await handler.index.embed(obs.summary), 0.8)
    assert [h.sentiment for h in hits] == ["mute"]
    assert await handler._thresholds.offset(user.id, "account_update") == 0.1
    assert (await repo.get(obs.id)).feedback == "mute"


async def test_foreign_and_malformed_buttons_are_ignored(user, clock, handler):
    clock.set(NOON)
    obs = await ask_obs(user.id)
    other = press(user.id + 999, f"at:n:{obs.id}", 80)
    await handler.on_button(other, other.payload["data"])
    bad = press(user.id, "at:zz", 81)
    await handler.on_button(bad, "at:zz")
    await handler.on_button(bad, "at:q:1")
    assert await texts() == [] and (await repo.get(obs.id)).feedback is None


async def test_concurrent_taps_with_different_update_ids_count_once(user, clock, handler):
    clock.set(NOON)
    obs = await ask_obs(user.id)
    events = [press(user.id, f"at:y:{obs.id}", 90 + i) for i in range(4)]
    await asyncio.gather(*(handler.on_button(e, e.payload["data"]) for e in events))
    snap = await Baselines().snapshot(user.id, "INR", "ramesh kumar", "upi")
    assert snap.counterparty.count == 1
    assert await handler._thresholds.offset(user.id, "money_movement") == 0.03


async def test_dispute_twice_with_new_update_id_keeps_one_loop_and_one_followup(user, clock, handler):
    clock.set(NOON)
    obs = await ask_obs(user.id)
    for update in (100, 101):
        event = press(user.id, f"at:n:{obs.id}", update)
        await handler.on_button(event, event.payload["data"])
    loops = [lp for lp in await handler._loops.active(user.id) if lp.kind is LoopKind.CONCERN]
    assert len(loops) == 1
    assert len(await WakeupService().pending(user.id, WakeupKind.AGENT)) == 1
    assert await handler._thresholds.offset(user.id, "money_movement") == -0.05


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "security", "verdict": "ask", "urgency": 4, "summary": "security alert from examplebank"},
        {"kind": "account_update", "verdict": "notify", "facts": {"risk_flags": ["credential_change"]}},
        {"kind": "money_movement", "verdict": "ask", "urgency": 5, "facts": {"money": MONEY}},
        {"kind": "money_movement", "verdict": "notify", "facts": {"money": MONEY, "anomaly": 0.5}},
        {"kind": "money_movement", "verdict": "notify", "facts": {"money": MONEY, "codes": ["new_payee"]}},
    ],
)
async def test_mute_is_neither_offered_nor_honoured_for_protected_items(user, clock, handler, fields):
    clock.set(NOON)
    obs = await make_obs(user.id, "p1", received_at=NOON, **{"summary": "alert from examplebank", **fields})
    assert can_mute(await repo.get(obs.id)) is False
    event = press(user.id, f"at:m:{obs.id}", 110)
    await handler.on_button(event, event.payload["data"])
    row = await repo.get(obs.id)
    assert row.feedback is None
    hits = await handler.index.prefs_near(user.id, await handler.index.embed(row.summary), 0.8)
    assert hits == []
    assert await handler._thresholds.offset(user.id, row.kind) == 0.0
    assert "keep telling you" in (await texts())[0]


async def test_mute_is_offered_for_ordinary_mail(user, clock, handler):
    obs = await make_obs(
        user.id, "o1", kind="account_update", verdict="notify", received_at=NOON, summary="plan renews"
    )
    assert can_mute(await repo.get(obs.id)) is True


async def test_threshold_writes_do_not_lose_updates(user, handler):
    t = handler._thresholds
    await asyncio.gather(
        *(t.learn(user.id, "account_update", Feedback.MUTE) for _ in range(3)),
        t.mark_urgent(user.id, "2026-10-03"),
    )
    state = await t.load(user.id)
    assert state["offsets"]["account_update"] == 0.3 and state["urgent_day"] == "2026-10-03"

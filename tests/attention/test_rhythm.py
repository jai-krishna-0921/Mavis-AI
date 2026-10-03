from datetime import UTC, datetime, timedelta

from mavis.attention.rhythm import NOTHING, AttentionBrief, EveningWrap, FirstLook, next_evening, purge
from mavis.domain.decisions import ComposedMessage
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.routines import BriefItem
from mavis.store.repo import attention as repo
from tests.attention.helpers import outbox_texts

MORNING = datetime(2026, 10, 3, 2, 30, tzinfo=UTC)  # 08:00 IST
EVENING = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 20:30 IST


async def add(user_id: int, mid: str, *, at: datetime, origin: str = repo.ORIGIN_LIVE, **fields):
    obs, _ = await repo.insert_pending(
        user_id,
        mid,
        thread_id="",
        origin=origin,
        sender_domain="x.in",
        sender_name="",
        received_at=at,
        payload={},
    )
    await repo.finish(obs.id, **fields)
    return obs


def test_next_evening():
    assert next_evening(MORNING, "Asia/Kolkata", "20:30") == EVENING
    assert next_evening(EVENING, "Asia/Kolkata", "20:30") == EVENING + timedelta(days=1)


async def test_brief_lists_waiting_overnight_items_untrusted(user, clock):
    clock.set(MORNING)
    night = MORNING - timedelta(hours=6)
    brief = AttentionBrief()
    assert await brief.items(user.id, MORNING, MORNING) == []
    await add(
        user.id,
        "b1",
        at=night,
        verdict="brief",
        summary="deadline or bill from examplepower: bill due",
        action="pay the bill",
    )
    await add(
        user.id, "a1", at=night, verdict="ask", feedback="confirmed", summary="money movement from x: debit"
    )
    await add(user.id, "l1", at=night, verdict="log", summary="receipt or order from x: shipped")
    await add(user.id, "l2", at=night, verdict="dropped", summary="newsletter from x: sale")
    await add(user.id, "bf", at=night, origin=repo.ORIGIN_BACKFILL, verdict="brief", summary="old thing")
    assert await brief.items(user.id, MORNING, MORNING) == [
        BriefItem("Email: deadline or bill from examplepower: bill due (asks: pay the bill)", False),
        BriefItem("Inbox: 2 routine emails handled quietly overnight.", True),
    ]


async def test_brief_says_nothing_needs_you(user, clock):
    clock.set(MORNING)
    await add(user.id, "l1", at=MORNING - timedelta(hours=2), verdict="log", summary="x")
    assert await AttentionBrief().items(user.id, MORNING, MORNING) == [
        BriefItem(NOTHING, True),
        BriefItem("Inbox: 1 routine email handled quietly overnight.", True),
    ]


async def test_evening_skips_when_nothing_notable_and_reschedules(user, clock, stack):
    clock.set(EVENING)
    await add(user.id, "l1", at=EVENING - timedelta(hours=3), verdict="log", summary="x")
    wrap = EveningWrap(lambda: stack.init.executor, stack.init.wakeups)
    await wrap.run(user.id)
    assert await outbox_texts() == []
    [nxt] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)
    assert nxt.due_at == EVENING + timedelta(days=1)


async def test_evening_summarizes_waiting_once(user, clock, stack, fake_llm):
    clock.set(EVENING)
    await add(
        user.id, "b1", at=EVENING - timedelta(hours=3), verdict="brief", summary="request from person: review"
    )
    await add(user.id, "n1", at=EVENING - timedelta(hours=5), verdict="notify", feedback="mute", summary="y")
    fake_llm.push_structured(
        ComposedMessage(send=True, messages=["Quiet day. One thing still waits: a review."])
    )
    wrap = EveningWrap(lambda: stack.init.executor, stack.init.wakeups)
    await wrap.run(user.id)
    await wrap.run(user.id)  # same local day: deduped before any LLM call
    assert await outbox_texts() == ["Quiet day. One thing still waits: a review."]
    prompt = str(fake_llm.structured_calls[-1]["user"])
    assert "Still waiting on them" in prompt and "<untrusted" in prompt and "flagged 1" in prompt


async def test_evening_ensure_is_idempotent_and_can_be_disabled(user, clock, stack, settings, monkeypatch):
    clock.set(MORNING)
    wrap = EveningWrap(lambda: stack.init.executor, stack.init.wakeups)
    monkeypatch.setattr(settings, "attention_evening_enabled", False)
    await wrap.ensure(user.id)
    assert await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP) == []
    monkeypatch.setattr(settings, "attention_evening_enabled", True)
    await wrap.ensure(user.id)
    await wrap.ensure(user.id)
    [w] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)
    assert w.due_at == EVENING


async def test_first_look_sends_once_after_backfill(user, clock, stack, fake_llm):
    clock.set(EVENING - timedelta(hours=6))
    first = FirstLook(lambda: stack.init.executor, stack.thresholds)
    assert not await first.maybe_send(user)  # no backfill yet
    await stack.thresholds.patch(user.id, backfilled_at=clock.t.isoformat())
    for i in range(3):
        await add(
            user.id,
            f"p{i}",
            at=clock.t - timedelta(days=3),
            origin=repo.ORIGIN_BACKFILL,
            verdict="log",
            summary="receipt",
            facts={"money": {"direction": "debit", "amount": 300.0}},
        )
    await add(
        user.id,
        "w1",
        at=clock.t - timedelta(days=2),
        origin=repo.ORIGIN_BACKFILL,
        verdict="brief",
        summary="request from person: send the form",
    )
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Done reading your last two weeks."]))
    assert await first.maybe_send(user)
    assert not await first.maybe_send(user)
    assert await outbox_texts() == ["Done reading your last two weeks."]
    assert "3 payments" in str(fake_llm.structured_calls[-1]["user"])


async def test_purge_deletes_old_rows_and_points(user, clock, stack):
    clock.set(EVENING - timedelta(days=100))
    old = await add(user.id, "old", at=clock.t, verdict="log", summary="old")
    vector = await stack.index.embed("old summary text")
    await repo.set_fields(
        old.id,
        point_id=await stack.index.add_observation(user.id, old.id, "other", vector, clock.t.isoformat()),
    )
    clock.set(EVENING)
    assert await purge(stack.index) == 1
    assert await repo.get(old.id) is None and await stack.index.novelty(user.id, vector) == 1.0


async def test_brief_email_lines_are_untrusted(user, clock):
    clock.set(MORNING)
    await add(user.id, "b1", at=MORNING - timedelta(hours=1), verdict="brief", summary="request from x: sign")
    items = await AttentionBrief().items(user.id, MORNING, MORNING)
    assert [i.trusted for i in items if i.text.startswith("Email:")] == [False]


class _SpyExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify(self, user, intent, **kw):
        self.calls.append({"intent": intent, **kw})
        return True


async def test_evening_wrap_is_untrusted_and_expires_tonight_if_deferred(user, clock, stack):
    clock.set(EVENING)
    await add(user.id, "b1", at=EVENING - timedelta(hours=1), verdict="brief", summary="request: review")
    spy = _SpyExecutor()
    await EveningWrap(lambda: spy, stack.init.wakeups).run(user.id)
    [call] = spy.calls
    assert call["untrusted"] is True and call["intent"].urgency == 2
    # 23:00 IST is 17:30 UTC: quiet hours may defer it, but a deferred wrap-up never becomes a morning ping
    assert call["origin"]["valid_until"] == datetime(2026, 10, 3, 17, 30, tzinfo=UTC).isoformat()


async def test_evening_outside_window_is_skipped_but_rebooked(user, clock, stack):
    clock.set(MORNING)  # 08:00 IST: a late-fired or misfired wrap
    await add(user.id, "b1", at=MORNING - timedelta(hours=1), verdict="brief", summary="x")
    spy = _SpyExecutor()
    await EveningWrap(lambda: spy, stack.init.wakeups).run(user.id)
    assert spy.calls == []
    [w] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_EVENING_WRAP)
    assert w.due_at == EVENING


async def test_retention_chain_purges_only_its_user_and_rebooks(user, clock, stack):
    from mavis.attention.rhythm import Retention
    from mavis.store.repo import users

    other, _ = await users.get_or_create_by_chat(777, "Other")
    clock.set(EVENING - timedelta(days=100))
    mine = await add(user.id, "old", at=clock.t, verdict="log", summary="old")
    theirs = await add(other.id, "old", at=clock.t, verdict="log", summary="old")
    stale, _ = await repo.insert_pending(
        user.id,
        "stale",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="x.in",
        sender_name="",
        received_at=clock.t,
        payload={"snippet": "body"},
    )
    clock.set(EVENING)
    fresh_pending, _ = await repo.insert_pending(
        user.id,
        "fresh",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="x.in",
        sender_name="",
        received_at=clock.t,
        payload={"snippet": "body"},
    )
    retention = Retention(lambda: stack.index, stack.init.wakeups)
    await retention.run(user.id)
    assert await repo.get(mine.id) is None and await repo.get(stale.id) is None
    assert await repo.get(theirs.id) is not None
    assert (await repo.get(fresh_pending.id)).status == repo.PENDING
    [w] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)
    assert w.due_at == datetime(2026, 10, 3, 22, 0, tzinfo=UTC)  # 03:30 IST tomorrow
    await retention.ensure(user.id)  # idempotent
    assert len(await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_RETENTION)) == 1


async def test_expire_pending_closes_old_unread_mail_and_drops_the_snippet(user, clock, stack):
    clock.set(EVENING - timedelta(days=3))
    obs, _ = await repo.insert_pending(
        user.id,
        "p",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="x.in",
        sender_name="",
        received_at=clock.t,
        payload={"snippet": "body"},
    )
    clock.set(EVENING)
    await purge(stack.index, user.id)
    row = await repo.get(obs.id)
    assert row.status == repo.DONE and row.method == "expired" and row.pending_payload is None

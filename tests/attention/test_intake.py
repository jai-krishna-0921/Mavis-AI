import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from mavis.attention.baselines import Baselines
from mavis.attention.schema import Direction, EmailKind, EmailUnderstanding, Money, PayMethod, RiskFlag
from mavis.attention.speaker import is_bulk
from mavis.domain.decisions import ComposedMessage
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType
from mavis.domain.integrations import ToolResult
from mavis.domain.loops import LoopKind, LoopUpsert, WatchSpec
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.llm import models as llm
from mavis.store.repo import attention as repo
from mavis.store.repo import messages, users
from mavis.tools.integrations.normalize import email_event
from tests.attention.helpers import EPOCH, email, outbox_texts, raw_email

ACCOUNT = EmailUnderstanding(kind=EmailKind.ACCOUNT_UPDATE)


async def drains(stack, user_id):
    return await stack.init.wakeups.pending(user_id, WakeupKind.SYSTEM_ATTENTION_DRAIN)


async def only(user_id):
    rows = await repo.recent(user_id, EPOCH)
    assert len(rows) == 1
    return rows[0]


async def backlog(stack, user, fake_llm, understood: int, total: int):
    for _ in range(understood):
        fake_llm.push_structured(ACCOUNT)
    for i in range(total):
        await stack.intake.on_email(email(user.id, f"b{i}", subject=f"Notice {i}"))


async def test_updates_category_with_unsubscribe_is_understood(user, stack, fake_llm):
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.on_email(
        email(
            user.id,
            "m1",
            sender="Bank <alerts@examplebank.in>",
            subject="Debit alert",
            labels=("INBOX", "CATEGORY_UPDATES"),
            unsubscribe=True,
        )
    )
    assert len(fake_llm.structured_calls) == 1
    obs = await only(user.id)
    assert obs.method == "llm" and obs.kind == "account_update" and obs.status == "done"
    assert obs.pending_payload is None and obs.summary.startswith("account update from examplebank:")
    assert obs.point_id is not None


async def test_promotions_logged_without_llm(user, stack, fake_llm):
    await stack.intake.on_email(
        email(user.id, "p1", sender="Shop <deals@exampleshop.com>", labels=("INBOX", "CATEGORY_PROMOTIONS"))
    )
    assert fake_llm.structured_calls == []
    obs = await only(user.id)
    assert (obs.verdict, obs.kind, obs.method, obs.point_id) == ("dropped", "newsletter", "labels", None)
    assert (await Baselines().sender(user.id, "deals@exampleshop.com")).count == 1


async def test_sent_mail_is_ignored(user, stack, fake_llm):
    await stack.intake.on_email(email(user.id, "s1", labels=("SENT",)))
    assert not await repo.has_any(user.id) and fake_llm.structured_calls == []


async def test_watched_loop_match_is_forwarded(user, stack, fake_llm):
    await stack.init.loops.upsert(
        user.id,
        LoopUpsert(
            kind=LoopKind.WAITING_ON,
            title="Reply from Priya",
            watch=WatchSpec(from_contains="priya@example.com"),
            source="tg:update:1",
        ),
    )
    ev = email(user.id, "w1", sender="Priya <priya@example.com>", subject="Re: plan")
    await stack.intake.on_email(ev)
    assert [e.id for e in stack.forwarded] == [ev.id] and fake_llm.structured_calls == []
    assert (await only(user.id)).verdict == "forwarded"


async def test_budget_leaves_rest_pending_and_arms_drain(user, stack, fake_llm, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "attention_understand_per_window", 2)
    await backlog(stack, user, fake_llm, understood=2, total=3)
    assert len(fake_llm.structured_calls) == 2 and await repo.pending_count(user.id) == 1
    pending = await drains(stack, user.id)
    assert len(pending) == 1 and pending[0].due_at == clock.t + timedelta(seconds=120)


async def test_drain_processes_backlog_then_reports_empty(
    user, stack, fake_llm, settings, monkeypatch, clock
):
    monkeypatch.setattr(settings, "attention_understand_per_window", 2)
    await backlog(stack, user, fake_llm, understood=2, total=3)
    clock.advance(minutes=3)
    fake_llm.push_structured(ACCOUNT)
    assert await stack.intake.drain(user.id) == 1
    assert await repo.pending_count(user.id) == 0 and stack.first_looks == [user.id]


async def test_drain_yields_to_recent_chat(user, stack, fake_llm, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "attention_understand_per_window", 1)
    await backlog(stack, user, fake_llm, understood=1, total=2)
    clock.advance(minutes=3)
    await messages.log(user.id, Role.USER, "hi there")
    assert await stack.intake.drain(user.id) == 0
    assert await repo.pending_count(user.id) == 1
    assert any(w.due_at > clock.t for w in await drains(stack, user.id))


async def test_drain_stops_when_llm_unavailable(user, stack, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "unavailable_s", lambda: 5.0)
    await stack.intake.on_email(email(user.id, "u1"))
    assert fake_llm.structured_calls == [] and await repo.pending_count(user.id) == 1
    assert await stack.intake.drain(user.id) == 0


async def test_llm_failure_retries_then_falls_back(user, stack, fake_llm, clock):
    fake_llm.push_error(LLMError("down"), structured=True)
    await stack.intake.on_email(email(user.id, "f1", subject="Hello"))
    [obs] = await repo.pending(user.id)
    assert obs.attempts == 1 and len(await drains(stack, user.id)) == 1
    clock.advance(minutes=3)
    fake_llm.push_error(LLMError("down"), structured=True)
    await stack.intake.drain(user.id)
    done = await repo.get(obs.id)
    assert (done.status, done.method, done.verdict) == ("done", "heuristic", "log")


async def test_duplicate_event_does_not_respeak(user, stack, fake_llm):
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    fake_llm.push_structured(
        ComposedMessage(send=True, messages=["New sign-in on your account. Was that you?"])
    )
    ev = email(user.id, "s1", sender="Accounts <no-reply@accounts.example.com>", subject="New sign-in")
    await stack.intake.on_email(ev)
    await stack.intake.on_email(ev)  # an unexpected extra LLM call would fail inside FakeLLM
    assert await outbox_texts() == ["New sign-in on your account. Was that you?"]
    obs = await only(user.id)
    assert (obs.verdict, obs.delivery, obs.urgency) == ("notify", "sent", 4)


async def test_drain_redelivers_queued(user, stack, fake_llm, clock):
    obs, _ = await repo.insert_pending(
        user.id,
        "q1",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="examplepower.in",
        sender_name="Power",
        received_at=clock.t,
        payload={},
    )
    await repo.finish(
        obs.id,
        kind="deadline_or_bill",
        verdict="notify",
        urgency=3,
        delivery=repo.QUEUED,
        summary="deadline or bill from examplepower: bill due",
        facts={"codes": []},
    )
    clock.advance(minutes=2)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Your power bill is due tomorrow."]))
    await stack.intake.drain(user.id)
    assert await outbox_texts() == ["Your power bill is due tomorrow."]
    assert (await repo.get(obs.id)).delivery == "sent"


async def test_backfill_queues_old_mail_and_never_speaks(user, stack, fake_llm, provider, clock):
    old = clock.t - timedelta(days=3)
    for days in (12, 11, 10):  # poisoning ruling: only an established sender feeds the money baseline
        await stack.baselines.touch_sender(
            user.id, "orders@exampleshop.com", "exampleshop.com", clock.t - timedelta(days=days)
        )
    provider.results["mail.search"] = ToolResult(
        ok=True,
        data={
            "messages": [
                raw_email("bf1", sender="Shop <orders@exampleshop.com>", subject="Payment received", at=old),
                raw_email(
                    "bf2", sender="Shop <deals@exampleshop.com>", labels=("CATEGORY_PROMOTIONS",), at=old
                ),
            ]
        },
    )
    assert await stack.intake.backfill(user.id) == 2
    assert await stack.intake.backfill(user.id) == 0
    assert await repo.pending_count(user.id) == 1
    fake_llm.push_structured(
        EmailUnderstanding(
            kind=EmailKind.RECEIPT_OR_ORDER,
            money=Money(
                amount=500,
                currency="INR",
                direction=Direction.DEBIT,
                counterparty="Exampleshop",
                method=PayMethod.CARD,
            ),
        )
    )
    await stack.intake.drain(user.id)
    assert await outbox_texts() == []
    assert (await Baselines().snapshot(user.id, "INR", "exampleshop", "card")).counterparty.count == 1
    assert stack.first_looks == [user.id]


async def test_first_sync_schedules_backfill_and_heal_rearms(user, stack, clock):
    ev = Event(
        id=f"first_sync:{user.id}:gmail",
        user_id=user.id,
        type=EventType.TASK_COMPLETED,
        occurred_at=clock.t,
        source="integrations",
        payload={"kind": "first_sync", "capability": "gmail"},
    )
    await stack.intake.on_task_completed(ev)
    await stack.intake.on_task_completed(ev)
    assert len(await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_BACKFILL)) == 1
    await repo.insert_pending(
        user.id,
        "h1",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="example.com",
        sender_name="",
        received_at=clock.t,
        payload={},
    )
    await users.update_state(user.id, {"polling": {"gmail": True}})
    await stack.intake.heal_all()
    assert len(await drains(stack, user.id)) == 1


async def test_enrich_reports_sender_facts(user, stack, clock):
    for days in (12, 11, 10, 9):
        await stack.baselines.touch_sender(
            user.id, "priya@example.com", "example.com", clock.t - timedelta(days=days)
        )
    text = await stack.pipeline.enrich(email(user.id, "e1", sender="Priya <priya@example.com>"))
    assert "emails_seen_from_sender=4" in text and "established_sender=yes" in text
    wake = Event(id="w", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer")
    assert await stack.pipeline.enrich(wake) == ""


async def test_speak_deferred_revalidates(user, stack, clock):
    answered, _ = await repo.insert_pending(
        user.id,
        "d1",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="examplebank.in",
        sender_name="",
        received_at=clock.t,
        payload={},
    )
    await repo.finish(
        answered.id,
        kind="money_movement",
        verdict="ask",
        urgency=4,
        feedback="confirmed",
        delivery="deferred",
    )
    stale, _ = await repo.insert_pending(
        user.id,
        "d2",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="examplebank.in",
        sender_name="",
        received_at=clock.t - timedelta(days=2),
        payload={},
    )
    await repo.finish(stale.id, kind="money_movement", verdict="ask", urgency=4, delivery="deferred")
    await stack.pipeline.speak_deferred(user.id, str(answered.id))
    await stack.pipeline.speak_deferred(user.id, str(stale.id))
    await stack.pipeline.speak_deferred(user.id, "not-a-number")
    assert await outbox_texts() == []
    assert (await repo.get(stale.id)).delivery == "expired"


# --- binding carries (progress.md) -------------------------------------------------------------------

BANK = "alerts@examplebank.in"
ACCOUNTS = "no-reply@accounts.example.com"


def debit(amount: float, payee: str = "Exampleshop", kind: EmailKind = EmailKind.RECEIPT_OR_ORDER):
    return EmailUnderstanding(
        kind=kind,
        money=Money(
            amount=amount,
            currency="INR",
            direction=Direction.DEBIT,
            counterparty=payee,
            method=PayMethod.CARD,
        ),
    )


def authed(user_id: int, mid: str, sender: str, *, passing: bool = True, **kw) -> Event:
    d = raw_email(mid, sender=sender, **kw)
    domain = sender.rpartition("@")[2].rstrip(">")
    verdict = "pass" if passing else "fail"
    d.setdefault("payload", {}).setdefault("headers", []).append(
        {
            "name": "Authentication-Results",
            "value": f"mx.google.com; dkim={verdict} header.i=@{domain} header.s=s1 header.b=AbC12",
        }
    )
    event = email_event(user_id, d, source="poller")
    assert event is not None
    return event


async def establish(stack, user_id: int, address: str, clock) -> None:
    domain = address.rpartition("@")[2]
    for days in (12, 11, 10):
        await stack.baselines.touch_sender(user_id, address, domain, clock.t - timedelta(days=days))


async def by_mid(user_id: int) -> dict:
    return {r.message_id: r for r in await repo.recent(user_id, EPOCH)}


async def queued_ask(user_id: int, mid: str, clock, urgency: int = 5, delivery: str = repo.QUEUED):
    obs, _ = await repo.insert_pending(
        user_id,
        mid,
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="example.com",
        sender_name="",
        received_at=clock.t,
        payload={},
    )
    await repo.finish(
        obs.id,
        kind="security",
        verdict="ask",
        urgency=urgency,
        facts={"risk_flags": ["credential_change"], "codes": []},
        delivery=delivery,
    )
    return await repo.get(obs.id)


async def test_llm_error_uses_heuristic_and_still_logs(user, stack, fake_llm, settings, monkeypatch):
    monkeypatch.setattr(settings, "attention_max_attempts", 1)
    fake_llm.push_error(LLMError("down"), structured=True)
    with capture_logs() as logs:
        await stack.intake.on_email(email(user.id, "h1", subject="Hello"))
    obs = await only(user.id)
    assert (obs.status, obs.method) == ("done", "heuristic")
    events = [e["event"] for e in logs]
    assert "attention.understand_failed" in events and "attention.observed" in events


async def test_ingest_stores_only_the_auth_bool(user, stack, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "unavailable_s", lambda: 5.0)
    await stack.intake.on_email(authed(user.id, "a1", f"Accounts <{ACCOUNTS}>"))
    [obs] = await repo.pending(user.id)
    assert obs.pending_payload["sender_authenticated"] is True
    assert "dkim" not in repr(obs.pending_payload)
    monkeypatch.setattr(llm, "unavailable_s", lambda: 0.0)
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.drain(user.id)
    assert (await repo.get(obs.id)).facts["authenticated"] is True


async def test_bulk_markers_recorded_from_labels_and_unsubscribe(user, stack, fake_llm):
    await stack.intake.on_email(
        email(
            user.id,
            "p1",
            sender="Shop <deals@exampleshop.com>",
            labels=("INBOX", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"),
        )
    )
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.on_email(email(user.id, "u1", sender="News <news@examplenews.com>", unsubscribe=True))
    rows = await by_mid(user.id)
    assert rows["p1"].facts["bulk"] == ["promotional", "social"]
    assert rows["u1"].facts["bulk"] == ["list_unsubscribe"]
    assert is_bulk(rows["p1"]) and is_bulk(rows["u1"])


def test_is_bulk_reads_the_dropped_verdict():
    assert is_bulk(SimpleNamespace(verdict="dropped", status="done", facts={}))
    assert not is_bulk(SimpleNamespace(verdict="notify", status="done", facts={}))


async def test_raw_anomaly_and_codes_stored_and_ask_never_baselined(user, stack, fake_llm):
    fake_llm.push_structured(debit(48000, kind=EmailKind.MONEY_MOVEMENT))
    await stack.intake.on_email(email(user.id, "big", sender=f"Bank <{BANK}>", subject="Debit alert"))
    obs = await only(user.id)
    assert obs.verdict == "ask" and obs.facts["anomaly"] == obs.score >= 0.6
    assert "large_amount" in obs.facts["codes"] and obs.facts["baselined"] is False
    assert (await Baselines().snapshot(user.id, "INR", "exampleshop", "card")).overall.count == 0


async def test_unestablished_sender_never_feeds_money_baseline(user, stack, fake_llm):
    fake_llm.push_structured(debit(500))
    await stack.intake.on_email(email(user.id, "n1", sender="Shop <orders@exampleshop.com>"))
    obs = await only(user.id)
    assert obs.verdict != "ask" and obs.facts["baselined"] is False
    assert (await Baselines().snapshot(user.id, "INR", "exampleshop", "card")).overall.count == 0


async def test_baseline_records_at_most_three_per_payee_per_day(user, stack, fake_llm, clock):
    await establish(stack, user.id, "orders@exampleshop.com", clock)
    for i in range(4):
        fake_llm.push_structured(debit(500))
        await stack.intake.on_email(email(user.id, f"d{i}", sender="Shop <orders@exampleshop.com>"))
    snap = await Baselines().snapshot(user.id, "INR", "exampleshop", "card")
    assert snap.counterparty.count == 3
    assert sorted(r.facts["baselined"] for r in (await by_mid(user.id)).values()) == [False, True, True, True]
    clock.advance(days=1)
    fake_llm.push_structured(debit(500))
    await stack.intake.on_email(email(user.id, "next", sender="Shop <orders@exampleshop.com>"))
    assert (await Baselines().snapshot(user.id, "INR", "exampleshop", "card")).counterparty.count == 4


async def test_lookalike_sender_never_becomes_established(user, stack, fake_llm, clock):
    await establish(stack, user.id, BANK, clock)
    imitation = "alerts@examp1ebank.in"
    await stack.intake.on_email(
        email(user.id, "l1", sender=f"Bank <{imitation}>", labels=("INBOX", "CATEGORY_PROMOTIONS"))
    )
    fake_llm.push_structured(debit(500, kind=EmailKind.MONEY_MOVEMENT))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["That email imitates your bank."]))
    await stack.intake.on_email(email(user.id, "l2", sender=f"Bank <{imitation}>", subject="Debit"))
    assert (await Baselines().sender(user.id, imitation)).count == 0
    l2 = (await by_mid(user.id))["l2"]
    assert "lookalike_domain" in l2.facts["codes"] and l2.facts["baselined"] is False


@pytest.mark.parametrize(
    ("passing", "prior", "expected"), [(True, True, 5), (False, True, 4), (True, False, 4)]
)
async def test_urgency_five_needs_auth_and_prior_security(
    user, stack, fake_llm, clock, passing, prior, expected
):
    await establish(stack, user.id, ACCOUNTS, clock)
    if prior:
        old, _ = await repo.insert_pending(
            user.id,
            "prior",
            thread_id="",
            origin=repo.ORIGIN_LIVE,
            sender_domain="example.com",
            sender_name="",
            received_at=clock.t - timedelta(days=5),
            payload={},
        )
        await repo.finish(old.id, kind="security", verdict="brief", facts={"authenticated": True})
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.CREDENTIAL_CHANGE])
    )
    await stack.intake.on_email(authed(user.id, "c1", f"Accounts <{ACCOUNTS}>", passing=passing))
    obs = (await by_mid(user.id))["c1"]
    assert (obs.verdict, obs.urgency, obs.delivery) == ("ask", expected, "sent")
    urgent_day = (await stack.thresholds.load(user.id)).get("urgent_day")
    assert (urgent_day is not None) is (expected == 5)


async def test_one_urgent_ask_per_day_even_when_concurrent(user, stack, clock):
    a, b = await queued_ask(user.id, "u-a", clock), await queued_ask(user.id, "u-b", clock)
    results = await asyncio.gather(
        stack.pipeline.deliver_queued(user, a), stack.pipeline.deliver_queued(user, b)
    )
    assert results == ["sent", "sent"]
    urgencies = sorted([(await repo.get(a.id)).urgency, (await repo.get(b.id)).urgency])
    assert urgencies == [4, 5]
    assert (await stack.thresholds.load(user.id))["urgent_day"] == "2026-09-27"


async def test_deferred_urgent_ask_does_not_use_up_the_day(user, stack, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    await use_up_bypasses(user, clock)  # the capped over-budget bypass is gone too: the ask must wait
    obs = await queued_ask(user.id, "u-d", clock)
    assert await stack.pipeline.deliver_queued(user, obs) == "deferred"
    assert "urgent_day" not in await stack.thresholds.load(user.id)
    assert (await repo.get(obs.id)).urgency == 5


async def test_bulk_security_notice_gets_no_budget_bypass(user, stack, fake_llm, settings, monkeypatch):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    signin = EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    fake_llm.push_structured(signin)
    await stack.intake.on_email(email(user.id, "b1", sender=f"Accounts <{ACCOUNTS}>", unsubscribe=True))
    fake_llm.push_structured(signin)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["New sign-in on your account."]))
    await stack.intake.on_email(email(user.id, "b2", sender=f"Accounts <{ACCOUNTS}>"))
    rows = await by_mid(user.id)
    assert (rows["b1"].delivery, rows["b2"].delivery) == ("dropped", "sent")
    assert await outbox_texts() == ["New sign-in on your account."]


async def test_dropped_notify_is_never_retried(user, stack, fake_llm, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    fake_llm.push_structured(
        EmailUnderstanding(
            kind=EmailKind.DEADLINE_OR_BILL, needs_user=True, deadline=clock.t + timedelta(days=1)
        )
    )
    await stack.intake.on_email(email(user.id, "bill", sender="Power <bills@examplepower.in>"))
    obs = await only(user.id)
    assert (obs.verdict, obs.delivery) == ("notify", "dropped")
    clock.advance(minutes=5)
    await stack.intake.drain(user.id)  # an unexpected compose call would fail inside FakeLLM
    await stack.intake.on_email(email(user.id, "bill", sender="Power <bills@examplepower.in>"))
    assert (await repo.get(obs.id)).delivery == "dropped" and await outbox_texts() == []
    assert await repo.undelivered(user.id, before=clock.t) == []


async def test_speak_deferred_sends_only_a_fresh_undelivered_item(user, stack, clock):
    fresh = await queued_ask(user.id, "s-fresh", clock, urgency=4, delivery="deferred")
    sent = await queued_ask(user.id, "s-sent", clock, urgency=4, delivery="sent")
    dropped = await queued_ask(user.id, "s-dropped", clock, urgency=4, delivery="dropped")
    for obs in (sent, dropped, fresh):
        await stack.pipeline.speak_deferred(user.id, str(obs.id))
    texts = await outbox_texts()
    assert len(texts) == 2 and texts[1] == "Was this you?"
    assert (await repo.get(fresh.id)).delivery == "sent"
    assert (await repo.get(dropped.id)).delivery == "dropped"


async def test_only_the_sanitized_summary_is_embedded(user, stack, fake_llm, monkeypatch):
    seen: list[str] = []
    original = stack.index.embed

    async def spy(text: str):
        seen.append(text)
        return await original(text)

    monkeypatch.setattr(stack.index, "embed", spy)
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.on_email(
        email(
            user.id,
            "z1",
            sender="Shop <orders@exampleshop.com>",
            subject="Call 9876543210 or visit http://evil.example.net/login",
            text="private body words",
        )
    )
    obs = await only(user.id)
    assert seen == [obs.summary]
    assert not any(bad in seen[0] for bad in ("9876543210", "http", "evil", "private"))


async def test_backfill_stays_within_retention(user, stack, provider, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "attention_retention_days", 2)
    provider.results["mail.search"] = ToolResult(
        ok=True,
        data={
            "messages": [
                raw_email("old", sender="Shop <orders@exampleshop.com>", at=clock.t - timedelta(days=3)),
                raw_email("new", sender="Shop <orders@exampleshop.com>", at=clock.t - timedelta(days=1)),
            ]
        },
    )
    assert await stack.intake.backfill(user.id) == 1
    [(_, _, args)] = provider.executed
    assert args["query"].startswith("newer_than:2d ")
    assert [o.message_id for o in await repo.pending(user.id)] == ["new"]


# --- fix round 1 ---------------------------------------------------------------------------------------


async def queued_notify(user_id: int, mid: str, clock):
    obs, _ = await repo.insert_pending(
        user_id,
        mid,
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="examplepower.in",
        sender_name="Power",
        received_at=clock.t,
        payload={},
    )
    await repo.finish(
        obs.id,
        kind="deadline_or_bill",
        verdict="notify",
        urgency=3,
        delivery=repo.QUEUED,
        summary="deadline or bill from examplepower: bill due",
        facts={"codes": []},
    )
    return obs


async def test_failing_compose_does_not_stall_the_drain(user, stack, fake_llm, monkeypatch, clock):
    q = await queued_notify(user.id, "q-fail", clock)
    monkeypatch.setattr(llm, "unavailable_s", lambda: 5.0)
    await stack.intake.on_email(email(user.id, "p-1", subject="Notice"))  # stays pending
    monkeypatch.setattr(llm, "unavailable_s", lambda: 0.0)
    clock.advance(minutes=3)
    fake_llm.push_error(LLMError("compose down"), structured=True)  # the queued notify's compose
    fake_llm.push_structured(ACCOUNT)  # the pending email is still understood
    assert await stack.intake.drain(user.id) == 1
    assert await repo.pending_count(user.id) == 0
    assert (await repo.get(q.id)).delivery == repo.QUEUED
    [nxt] = await drains(stack, user.id)  # the earlier future drain absorbs the re-arm
    assert nxt.due_at > clock.t
    assert await outbox_texts() == []


async def test_drain_skips_redelivery_while_llm_backs_off(user, stack, fake_llm, monkeypatch, clock):
    q = await queued_notify(user.id, "q-wait", clock)
    clock.advance(minutes=3)
    monkeypatch.setattr(llm, "unavailable_s", lambda: 600.0)
    assert await stack.intake.drain(user.id) == 0  # an unexpected compose call would fail inside FakeLLM
    assert (await repo.get(q.id)).delivery == repo.QUEUED
    [nxt] = await drains(stack, user.id)
    assert nxt.due_at == clock.t + timedelta(seconds=600)


async def test_live_compose_failure_keeps_row_queued_and_arms_drain(user, stack, fake_llm, clock):
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    fake_llm.push_error(LLMError("compose down"), structured=True)
    await stack.intake.on_email(email(user.id, "live-f", sender=f"Accounts <{ACCOUNTS}>"))
    obs = await only(user.id)
    assert (obs.verdict, obs.delivery) == ("notify", repo.QUEUED)
    assert len(await drains(stack, user.id)) == 1
    clock.advance(minutes=5)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["New sign-in on your account."]))
    await stack.intake.drain(user.id)
    assert (await repo.get(obs.id)).delivery == "sent"


async def test_security_alert_in_social_tab_is_understood_but_stays_bulk(
    user, stack, fake_llm, settings, monkeypatch
):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    await stack.intake.on_email(
        email(
            user.id,
            "soc",
            sender="Social <security@examplesocial.com>",
            subject="New sign-in to your account",
            labels=("INBOX", "CATEGORY_SOCIAL"),
        )
    )
    obs = await only(user.id)
    assert (obs.method, obs.verdict) == ("llm", "notify")
    assert obs.facts["bulk"] == ["social"] and is_bulk(obs)
    assert obs.delivery == "dropped"  # bulk: no security budget bypass


async def test_spam_is_dropped_even_when_it_looks_like_security(user, stack, fake_llm):
    await stack.intake.on_email(
        email(user.id, "sp", subject="Security alert: new sign-in", labels=("SPAM", "CATEGORY_SOCIAL"))
    )
    assert fake_llm.structured_calls == []
    assert (await only(user.id)).verdict == "dropped"


async def test_backfill_keeps_security_looking_bulk_mail_and_drops_spam(user, stack, provider, clock):
    old = clock.t - timedelta(days=2)
    provider.results["mail.search"] = ToolResult(
        ok=True,
        data={
            "messages": [
                raw_email("f1", subject="Password reset requested", labels=("CATEGORY_FORUMS",), at=old),
                raw_email("f2", subject="Weekly digest", labels=("CATEGORY_FORUMS",), at=old),
                raw_email("f3", subject="Unusual activity", labels=("SPAM",), at=old),
            ]
        },
    )
    assert await stack.intake.backfill(user.id) == 3
    assert [o.message_id for o in await repo.pending(user.id)] == ["f1"]


async def test_live_copy_reclaims_a_row_backfill_created(user, stack, fake_llm, provider, clock):
    provider.results["mail.search"] = ToolResult(
        ok=True,
        data={"messages": [raw_email("race", sender=f"Accounts <{ACCOUNTS}>", at=clock.t)]},
    )
    assert await stack.intake.backfill(user.id) == 1
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    fake_llm.push_structured(ComposedMessage(send=True, messages=["New sign-in on your account."]))
    await stack.intake.on_email(email(user.id, "race", sender=f"Accounts <{ACCOUNTS}>", at=clock.t))
    obs = await only(user.id)
    assert (obs.origin, obs.delivery) == ("live", "sent")


async def test_baseline_cap_counts_by_the_mail_day(user, stack, fake_llm, clock):
    await establish(stack, user.id, "orders@exampleshop.com", clock)
    for i, days in enumerate((3, 3, 2, 2)):
        fake_llm.push_structured(debit(500))
        await stack.intake.on_email(
            email(
                user.id, f"md{i}", sender="Shop <orders@exampleshop.com>", at=clock.t - timedelta(days=days)
            )
        )
    assert (await Baselines().snapshot(user.id, "INR", "exampleshop", "card")).counterparty.count == 4


# --- final review fixes -----------------------------------------------------------------------------


async def use_up_bypasses(user, clock) -> None:
    from mavis.policy.pings import SECURITY_BYPASS_PREFIX, PingPolicy

    for i in range(2):
        await PingPolicy().record(
            user, f"other:{i}", 4, clock.t, extra_keys=[f"{SECURITY_BYPASS_PREFIX}x{i}"]
        )


async def test_date_only_deadline_does_not_crash(user, stack, fake_llm):
    raw = {"kind": "deadline_or_bill", "needs_user": True, "deadline": "2026-10-05"}
    fake_llm.push_structured(EmailUnderstanding.model_validate(raw))
    await stack.intake.on_email(email(user.id, "bill-d", sender="Power <bills@examplepower.in>"))
    obs = await only(user.id)
    assert obs.status == "done" and obs.method == "llm"
    assert obs.facts["deadline"] == "2026-10-05T18:29:59+00:00"  # end of that day, user-local (IST)


def test_bare_date_occurred_at_is_unknown_not_midnight():
    m = Money.model_validate({"amount": "500", "occurred_at": "2026-10-05"})
    assert m.occurred_at is None


async def test_a_raising_row_does_not_block_the_next_email(user, stack, fake_llm, monkeypatch, clock):
    real = stack.pipeline.finalize

    async def finalize(user_, obs, payload, u, method):
        if obs.message_id == "bad":
            raise RuntimeError("boom")
        return await real(user_, obs, payload, u, method)

    monkeypatch.setattr(stack.pipeline, "finalize", finalize)
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.on_email(email(user.id, "bad", subject="Bad"))  # must not raise to the bus
    fake_llm.push_structured(ACCOUNT)
    await stack.intake.on_email(email(user.id, "good", subject="Good"))
    assert (await repo.get((await repo.pending(user.id))[0].id)).message_id == "bad"
    clock.advance(minutes=3)
    fake_llm.push_structured(ACCOUNT)  # the bad row's second attempt: it raises again and is closed
    await stack.intake.drain(user.id)
    rows = await by_mid(user.id)
    assert rows["good"].method == "llm"
    assert (rows["bad"].status, rows["bad"].method, rows["bad"].verdict) == ("done", "error", "log")
    assert await repo.pending_count(user.id) == 0


async def test_a_raising_row_is_skipped_in_the_drain(user, stack, fake_llm, monkeypatch, clock):
    monkeypatch.setattr(llm, "unavailable_s", lambda: 5.0)
    await stack.intake.on_email(email(user.id, "bad", subject="Bad"))
    await stack.intake.on_email(email(user.id, "good", subject="Good"))
    monkeypatch.setattr(llm, "unavailable_s", lambda: 0.0)
    real = stack.pipeline.finalize

    async def finalize(user_, obs, payload, u, method):
        if obs.message_id == "bad":
            raise RuntimeError("boom")
        return await real(user_, obs, payload, u, method)

    monkeypatch.setattr(stack.pipeline, "finalize", finalize)
    fake_llm.push_structured(ACCOUNT)
    fake_llm.push_structured(ACCOUNT)
    assert await stack.intake.drain(user.id) == 1
    assert (await by_mid(user.id))["good"].method == "llm"
    [bad] = await repo.pending(user.id)
    assert bad.message_id == "bad" and bad.attempts == 1
    assert len(await drains(stack, user.id)) == 1


async def test_naive_occurred_at_is_user_local(user, stack, fake_llm):
    raw = {
        "kind": "money_movement",
        "money": {
            "amount": "48,000",
            "currency": "Rs.",
            "direction": "debit",
            "counterparty": "Ramesh",
            "method": "upi",
            "occurred_at": "2026-09-26T20:10:00",  # 8:10pm IST, as a bank alert writes it
        },
    }
    fake_llm.push_structured(EmailUnderstanding.model_validate(raw))
    await stack.intake.on_email(email(user.id, "upi", sender=f"Bank <{BANK}>", subject="Debit alert"))
    obs = await only(user.id)
    assert obs.facts["money"]["local_hour"] == 20
    assert obs.facts["money"]["occurred_at"] == "2026-09-26T14:40:00+00:00"
    assert "odd_hour" not in obs.facts["codes"]
    assert obs.verdict == "ask"
    text = (await outbox_texts())[0]
    assert "20:10" in text and "01:40" not in text


async def test_asks_share_the_capped_budget_bypass(user, stack, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    asks = [await queued_ask(user.id, f"cap{i}", clock, urgency=4) for i in range(3)]
    results = [await stack.pipeline.deliver_queued(user, a) for a in asks]
    assert results == ["sent", "sent", "deferred"]  # at most 2 over-budget pings a day, shared


async def test_asks_and_security_notices_share_one_counter(user, stack, settings, monkeypatch, clock):
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    await use_up_bypasses(user, clock)
    assert await stack.pipeline.deliver_queued(user, await queued_ask(user.id, "late", clock, urgency=4)) == (
        "deferred"
    )


async def test_heuristic_never_asks_or_reaches_urgency_five(
    user, stack, fake_llm, settings, monkeypatch, clock
):
    monkeypatch.setattr(settings, "attention_max_attempts", 1)
    await establish(stack, user.id, ACCOUNTS, clock)
    prior, _ = await repo.insert_pending(
        user.id,
        "prior",
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="example.com",
        sender_name="",
        received_at=clock.t - timedelta(days=5),
        payload={},
    )
    await repo.finish(prior.id, kind="security", verdict="brief", facts={"authenticated": True})
    fake_llm.push_error(LLMError("bad output"), structured=True)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Your password was changed."]))
    await stack.intake.on_email(
        authed(user.id, "pw", f"Accounts <{ACCOUNTS}>", subject="Your password was changed")
    )
    obs = (await by_mid(user.id))["pw"]
    assert obs.method == "heuristic" and obs.kind == "security"
    assert (obs.verdict, obs.urgency) == ("notify", 4)
    assert "urgent_day" not in await stack.thresholds.load(user.id)


async def test_long_sender_address_is_truncated(user, stack, clock):
    address = "a" * 240 + "@example.com"
    await stack.baselines.touch_sender(user.id, address, "example.com", clock.t)
    await stack.baselines.touch_sender(user.id, address.upper(), "example.com", clock.t)
    assert (await stack.baselines.sender(user.id, address)).count == 2


async def test_stale_or_retried_queued_ping_is_given_up(user, stack, fake_llm, clock):
    old = await queued_notify(user.id, "q-old", clock)
    await repo.set_fields(old.id, received_at=clock.t - timedelta(hours=25))
    tried = await queued_notify(user.id, "q-tried", clock)
    await repo.set_fields(tried.id, facts={"codes": [], "resends": 3})
    clock.advance(minutes=3)
    await stack.intake.drain(user.id)  # an unexpected compose call would fail inside FakeLLM
    assert (await repo.get(old.id)).delivery == "expired"
    assert (await repo.get(tried.id)).delivery == "expired"
    assert await outbox_texts() == []


async def test_resends_are_counted_until_the_cap(user, stack, fake_llm, clock):
    q = await queued_notify(user.id, "q-count", clock)
    for _ in range(3):
        clock.advance(minutes=3)
        fake_llm.push_error(LLMError("compose down"), structured=True)
        await stack.intake.drain(user.id)
    assert (await repo.get(q.id)).facts["resends"] == 3
    clock.advance(minutes=3)
    await stack.intake.drain(user.id)
    assert (await repo.get(q.id)).delivery == "expired"


async def test_deferred_attention_ping_is_released_when_user_is_awake(user, stack, clock):
    from mavis.attention.scheduling import schedule_once

    clock.set(clock.t.replace(hour=19, minute=30))  # 01:00 IST, quiet hours
    morning = clock.t + timedelta(hours=6)
    await schedule_once(stack.init.wakeups, user.id, WakeupKind.SYSTEM_ATTENTION_SPEAK, "1", morning)
    assert await stack.init.executor.release_deferred(user) == 1
    [w] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_SPEAK)
    assert w.due_at < clock.t + timedelta(minutes=1)


async def test_backfill_pages_back_until_the_cap(user, stack, fake_llm, provider, clock, monkeypatch):
    calls: list[dict] = []

    async def execute(user_ref, action, args):
        calls.append(args)
        page = len(calls) - 1
        start = clock.t - timedelta(hours=1 + page * 50)
        msgs = [
            raw_email(f"pg{page}-{i}", sender="Shop <orders@exampleshop.com>", at=start - timedelta(hours=i))
            for i in range(args["max_results"])
        ]
        return ToolResult(ok=True, data={"messages": msgs})

    monkeypatch.setattr(provider, "execute", execute)
    assert await stack.intake.backfill(user.id) == 120
    assert [c["max_results"] for c in calls] == [50, 50, 20]
    assert "before:" not in calls[0]["query"] and "before:" in calls[1]["query"]
    assert fake_llm.structured_calls == []  # queueing costs no LLM call; the drain holds the budget

"""End to end (spec attention section 16): the real pipeline, SQLite, in-memory Qdrant, scripted LLM.

1. A large first-time UPI debit at 02:10 from an established sender: urgency 5 ask with buttons, no links.
2. A routine receipt at the same hour: silent.
3. A new sign-in alert: notify, deferred out of quiet hours to 07:00, then sent.
4. A promotions newsletter: logged, no LLM call.
5. "No, help me": next steps, a trusted loop, a follow-up, the fraud kept out of the baseline.
6. "Any Gmail updates?" in chat: a real answer from the log, and the turn is tainted.

Separate tests: a security notice survives an exhausted daily budget (within the cap), and a Social-tab
login alert is understood instead of dropped."""

from datetime import UTC, datetime, timedelta

from mavis.agents import context_hooks, simple_turn
from mavis.attention.digest import Digest
from mavis.attention.schema import Direction, EmailKind, EmailUnderstanding, Money, PayMethod, RiskFlag
from mavis.domain.decisions import ComposedMessage
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind
from mavis.domain.wakeups import WakeupKind
from mavis.store.repo import attention as repo
from mavis.tools.integrations.normalize import email_event
from tests.attention.helpers import EPOCH, email, outbox_rows, raw_email

T0 = datetime(2026, 10, 2, 20, 40, tzinfo=UTC)  # Sat 03 Oct 02:10 IST, inside quiet hours
BANK = "Example Bank <alerts@examplebank.in>"
SHOP = "Exampleshop <orders@exampleshop.com>"


def authed_email(user_id: int, mid: str, sender: str, **kw):
    """A message the receiving server (mx.google.com) recorded as DKIM-aligned with the From domain."""
    d = raw_email(mid, sender=sender, **kw)
    domain = sender.rpartition("@")[2].rstrip(">")
    d.setdefault("payload", {}).setdefault("headers", []).append(
        {
            "name": "Authentication-Results",
            "value": f"mx.google.com; dkim=pass header.i=@{domain} header.s=s1 header.b=AbC12",
        }
    )
    event = email_event(user_id, d, source="poller")
    assert event is not None
    return event


async def obs_for(user_id: int, mid: str):
    return next(r for r in await repo.recent(user_id, EPOCH, limit=100) if r.message_id == mid)


async def seed_history(stack, user_id: int) -> None:
    """Two weeks of normal life: an established bank sender and a regular food-delivery payee."""
    for days in (13, 11, 9, 7):
        for address, domain in (
            ("alerts@examplebank.in", "examplebank.in"),
            ("orders@exampleshop.com", "exampleshop.com"),
        ):
            await stack.baselines.touch_sender(user_id, address, domain, T0 - timedelta(days=days))
    for days, amount in ((13, 320), (11, 410), (9, 380), (8, 450), (7, 399)):
        await stack.baselines.record_money(
            user_id, "INR", "exampleshop", "upi", amount, 13, T0 - timedelta(days=days)
        )


async def test_attention_end_to_end(user, clock, stack, fake_llm):
    clock.set(T0)
    await seed_history(stack, user.id)

    # 1. The reference case. CATEGORY_UPDATES + List-Unsubscribe used to be dropped as a newsletter.
    fake_llm.push_structured(
        EmailUnderstanding(
            kind=EmailKind.MONEY_MOVEMENT,
            needs_user=True,
            money=Money(
                amount=48000,
                currency="INR",
                direction=Direction.DEBIT,
                counterparty="RAMESH KUMAR",
                method=PayMethod.UPI,
            ),
        )
    )
    await stack.intake.on_email(
        authed_email(
            user.id,
            "upi1",
            sender=BANK,
            subject="Debit alert: INR 48,000.00",
            at=T0,
            unsubscribe=True,
            labels=("INBOX", "CATEGORY_UPDATES"),
            text="Rs.48,000.00 debited via UPI to RAMESH KUMAR. Not you? Call 1800 000 0000 or visit "
            "http://examplebank-secure.example/verify",
        )
    )
    rows = await outbox_rows()
    assert len(rows) == 2 and rows[1].text == "Was this you?"
    assert [[b["label"] for b in row] for row in rows[1].buttons] == [["Yes, that was me", "No, help me"]]
    assert "₹48,000" in rows[0].text and "Ramesh Kumar" in rows[0].text and "02:10" in rows[0].text
    assert "far above your usual UPI payment" in rows[0].text  # ratio text is capped, no raw multiple
    for r in rows:
        assert "http" not in r.text and "1800" not in r.text and "examplebank-secure" not in r.text
    ask = await obs_for(user.id, "upi1")
    assert (ask.verdict, ask.urgency, ask.delivery) == ("ask", 5, "sent")
    assert (
        await stack.baselines.snapshot(user.id, "INR", "ramesh kumar", "upi")
    ).overall.count == 5  # held out

    # 2. Routine receipt, same hour, known payee, typical amount: silent.
    fake_llm.push_structured(
        EmailUnderstanding(
            kind=EmailKind.RECEIPT_OR_ORDER,
            money=Money(
                amount=349,
                currency="INR",
                direction=Direction.DEBIT,
                counterparty="Exampleshop Pvt Ltd",
                method=PayMethod.UPI,
            ),
        )
    )
    await stack.intake.on_email(
        email(user.id, "rcpt1", sender=SHOP, subject="Your order is on its way", at=T0)
    )
    receipt = await obs_for(user.id, "rcpt1")
    assert (receipt.verdict, receipt.delivery) == ("log", "none") and len(await outbox_rows()) == 2

    # 3. New sign-in: notify, but quiet hours defer it to 07:00 IST; then it goes out.
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    await stack.intake.on_email(
        email(
            user.id,
            "sec1",
            sender="Accounts <no-reply@accounts.example.com>",
            subject="New sign-in to your account",
            at=T0,
        )
    )
    sec = await obs_for(user.id, "sec1")
    assert (sec.verdict, sec.urgency, sec.delivery) == ("notify", 4, "deferred")
    [speak] = await stack.init.wakeups.pending(user.id, WakeupKind.SYSTEM_ATTENTION_SPEAK)
    assert speak.due_at == datetime(2026, 10, 3, 1, 30, tzinfo=UTC)
    clock.set(speak.due_at)
    fake_llm.push_structured(
        ComposedMessage(send=True, messages=["Someone signed in to your account overnight. Was that you?"])
    )
    await stack.pipeline.speak_deferred(user.id, speak.reason)
    assert (await outbox_rows())[-1].text.startswith("Someone signed in to your account overnight")
    assert (await obs_for(user.id, "sec1")).delivery == "sent"

    # 4. Promotions newsletter: logged without an LLM call.
    calls = len(fake_llm.structured_calls)
    await stack.intake.on_email(
        email(
            user.id,
            "news1",
            sender="Deals <deals@exampledeals.com>",
            subject="50% off",
            labels=("INBOX", "CATEGORY_PROMOTIONS"),
            unsubscribe=True,
        )
    )
    assert len(fake_llm.structured_calls) == calls
    assert (await obs_for(user.id, "news1")).verdict == "dropped" and len(await outbox_rows()) == 3

    # 5. "No, help me".
    tap = Event(
        id="tg:update:900",
        user_id=user.id,
        type=EventType.BUTTON_PRESSED,
        occurred_at=clock.t,
        source="telegram",
        payload={"data": f"at:n:{ask.id}"},
        trust=Trust.USER,
    )
    await stack.feedback.on_button(tap, tap.payload["data"])
    concern = [lp for lp in await stack.init.loops.active(user.id) if lp.kind is LoopKind.CONCERN]
    assert len(concern) == 1 and concern[0].source == "tg:update:900" and concern[0].importance == 5
    [follow] = await stack.init.wakeups.pending(user.id, WakeupKind.AGENT)
    assert follow.loop_id == concern[0].id and follow.due_at == clock.t + timedelta(hours=2)
    assert "banking or payment app directly" in (await outbox_rows())[-2].text
    assert (await stack.baselines.snapshot(user.id, "INR", "ramesh kumar", "upi")).counterparty.count == 0

    # 6. Chat: "any Gmail updates?" gets a real answer, and the turn is tainted.
    digest = await Digest(stack.index).context(user.id, "any Gmail updates?")
    assert "Emails seen in the last 24h: 4. Needing attention: 2." in digest
    assert "they said it was NOT them" in digest and "<untrusted" in digest
    context_hooks.clear_context_providers()
    context_hooks.register_context_provider(Digest(stack.index).context)
    try:
        context, hooked = await simple_turn.build_context_ex(user.id, "any Gmail updates?")
        _, small_talk_hooked = await simple_turn.build_context_ex(user.id, "how are you?")
    finally:
        context_hooks.clear_context_providers()
    assert hooked is True and "Needing attention" in context  # digest in context: learn as untrusted
    assert small_talk_hooked is False


async def test_security_notice_survives_an_exhausted_budget_within_the_cap(
    user, clock, stack, fake_llm, settings, monkeypatch
):
    clock.set(T0 + timedelta(hours=12))  # 14:10 IST, awake hours
    await seed_history(stack, user.id)
    monkeypatch.setattr(settings, "ping_daily_budget", 0)
    for n in range(3):
        fake_llm.push_structured(
            EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
        )
        fake_llm.push_structured(ComposedMessage(send=True, messages=[f"A new sign-in was seen ({n})."]))
        await stack.intake.on_email(
            email(
                user.id,
                f"sec{n}",
                sender=f"Accounts <no-reply@accounts{n}.example.com>",
                subject="New sign-in to your account",
                at=clock.t,
            )
        )
    delivered = [(await obs_for(user.id, f"sec{n}")).delivery for n in range(3)]
    assert delivered[:2] == ["sent", "sent"]  # over budget, inside the two a day security cap
    assert delivered[2] != "sent" and len(await outbox_rows()) == 2


async def test_social_tab_login_alert_is_understood_not_dropped(user, clock, stack, fake_llm):
    clock.set(T0 + timedelta(hours=12))
    fake_llm.push_structured(
        EmailUnderstanding(kind=EmailKind.SECURITY, needs_user=True, risk_flags=[RiskFlag.NEW_SIGNIN])
    )
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Someone signed in to your account."]))
    calls = len(fake_llm.structured_calls)
    await stack.intake.on_email(
        email(
            user.id,
            "soc1",
            sender="Social <security@social.example.com>",
            subject="Security alert: new sign-in from a new device",
            text="We noticed a new login to your account.",
            at=clock.t,
            labels=("INBOX", "CATEGORY_SOCIAL"),
        )
    )
    obs = await obs_for(user.id, "soc1")
    assert len(fake_llm.structured_calls) > calls  # it went to understanding
    assert obs.verdict == "notify" and obs.kind == "security"


async def test_the_users_own_draft_is_never_news(user, clock, stack, fake_llm):
    """A draft the user (or Mavis for them) saved in Gmail reached attention as "new mail from an unknown
    sender" (2026-10-10 evals). Drafts, like sent mail, are skipped before any model call."""
    clock.set(T0)
    await stack.intake.on_email(email(user.id, "draft1", sender="Me <me@example.com>",
                                      subject="Project Falcon: updated Q4 budget", labels=("DRAFT",), at=T0))
    assert not [r for r in await repo.recent(user.id, EPOCH, limit=100) if r.message_id == "draft1"]
    assert fake_llm.calls == []

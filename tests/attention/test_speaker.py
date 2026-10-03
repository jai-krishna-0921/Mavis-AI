import re
from datetime import UTC, datetime, timedelta

from mavis.attention.schema import AttentionDecision, Verdict
from mavis.attention.speaker import Speaker, ask_text, format_amount, join_reasons, notify_intent
from mavis.domain.wakeups import WakeupKind
from mavis.policy.pings import PingPolicy
from mavis.store.repo import attention as repo
from mavis.timers.service import WakeupService

T0 = datetime(2026, 10, 2, 20, 40, tzinfo=UTC)  # Sat 03 Oct 02:10 IST (quiet hours)
NOON = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)  # Sat 03 Oct 12:00 IST
MONEY = {
    "amount": 48000.0,
    "currency": "INR",
    "direction": "debit",
    "method": "upi",
    "counterparty_key": "ramesh kumar",
    "occurred_at": T0.isoformat(),
    "local_hour": 2,
}
REASONS = [
    "a large amount, and I don't know your usual spending yet",
    "a payee I haven't seen before",
    "at 02:00, in the middle of the night",
]


class FakeExecutor:
    def __init__(self) -> None:
        self.delivered: list[tuple] = []
        self.notified: list[tuple] = []

    async def deliver(
        self,
        user,
        bubbles,
        dedupe_key=None,
        urgency=3,
        quiet_streak=0,
        extra_keys=None,
        buttons=None,
        tainted=False,
    ):
        self.delivered.append((bubbles, dedupe_key, urgency, buttons))

    async def notify(
        self,
        user,
        intent,
        context="",
        quiet_streak=0,
        untrusted=False,
        original_due=None,
        origin=None,
        buttons=None,
    ):
        self.notified.append((intent, context, untrusted, buttons))
        return True


async def make_obs(user_id: int, mid: str, **fields):
    obs, _ = await repo.insert_pending(
        user_id,
        mid,
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain=fields.pop("domain", "examplebank.in"),
        sender_name="Bank",
        received_at=fields.pop("received_at", T0),
        payload={},
    )
    await repo.finish(obs.id, **fields)
    return await repo.get(obs.id)


def speaker(executor: FakeExecutor) -> Speaker:
    return Speaker(lambda: executor, PingPolicy(), WakeupService())


async def test_ask_is_deterministic_with_buttons_and_no_links(user, clock):
    clock.set(T0)
    obs = await make_obs(
        user.id,
        "m1",
        kind="money_movement",
        verdict="ask",
        urgency=5,
        facts={"money": MONEY, "risk_flags": []},
        reasons=REASONS,
    )
    ex = FakeExecutor()
    assert (
        await speaker(ex).speak(user, obs, AttentionDecision(Verdict.ASK, 5, 0.9, tuple(REASONS))) == "sent"
    )
    bubbles, key, urgency, buttons = ex.delivered[0]
    assert key == "attn:m1" and urgency == 5
    assert bubbles[1] == "Was this you?"
    assert "₹48,000" in bubbles[0] and "Ramesh Kumar" in bubbles[0] and "via UPI" in bubbles[0]
    assert "at 02:10" in bubbles[0] and "It stood out because it's a large amount" in bubbles[0]
    assert [[b.label for b in row] for row in buttons] == [["Yes, that was me", "No, help me"]]
    assert [b.data for b in buttons[0]] == [f"at:y:{obs.id}", f"at:n:{obs.id}"]
    for text in bubbles:
        assert "http" not in text and re.search(r"\d{6,}", text) is None and "examplebank.in" not in text
        assert "\u2014" not in text and "\u2013" not in text


async def test_quiet_hours_defer_non_urgent_ask_once(user, clock):
    clock.set(T0)
    obs = await make_obs(
        user.id,
        "m2",
        kind="money_movement",
        verdict="ask",
        urgency=4,
        facts={"money": MONEY},
        reasons=REASONS,
    )
    ex = FakeExecutor()
    decision = AttentionDecision(Verdict.ASK, 4, 0.8, tuple(REASONS))
    assert await speaker(ex).speak(user, obs, decision) == "deferred"
    assert await speaker(ex).speak(user, obs, decision) == "deferred"
    pending = await WakeupService().pending(user.id, WakeupKind.SYSTEM_ATTENTION_SPEAK)
    assert len(pending) == 1 and pending[0].reason == str(obs.id)
    assert pending[0].due_at == datetime(2026, 10, 3, 1, 30, tzinfo=UTC)  # 07:00 IST
    assert ex.delivered == []


async def test_notify_goes_through_executor_untrusted_with_mute_button(user, clock):
    clock.set(NOON)
    obs = await make_obs(
        user.id,
        "m3",
        kind="deadline_or_bill",
        verdict="notify",
        urgency=3,
        summary="deadline or bill from examplepower: bill due",
        action="pay the bill",
        facts={"codes": []},
        received_at=NOON,
    )
    ex = FakeExecutor()
    assert (
        await speaker(ex).speak(user, obs, AttentionDecision(Verdict.NOTIFY, 3, 0.85, ("due soon",)))
        == "sent"
    )
    intent, context, untrusted, buttons = ex.notified[0]
    assert untrusted is True and intent.urgency == 3 and intent.dedupe_key == "attn:m3"
    assert (
        "pay the bill" in intent.intent and "Gmail" in intent.intent and "examplebank.in" not in intent.intent
    )
    assert context == obs.summary
    assert [[b.data for b in row] for row in buttons] == [[f"at:m:{obs.id}"]]


async def test_security_notify_has_no_mute_button(user, clock):
    clock.set(NOON)
    obs = await make_obs(
        user.id,
        "m4",
        kind="security",
        verdict="notify",
        urgency=4,
        received_at=NOON,
        facts={"risk_flags": ["new_signin"], "codes": ["risk:new_signin"]},
    )
    ex = FakeExecutor()
    await speaker(ex).speak(
        user, obs, AttentionDecision(Verdict.NOTIFY, 4, 0.8, ("it mentions a new sign-in",))
    )
    assert ex.notified[0][3] is None and "was them" in ex.notified[0][0].intent


async def test_duplicate_counts_as_sent(user, clock):
    clock.set(NOON)
    obs = await make_obs(
        user.id, "m5", kind="money_movement", verdict="ask", urgency=4, facts={"money": MONEY}
    )
    await PingPolicy().record(user, "attn:m5", 4, NOON)
    ex = FakeExecutor()
    assert await speaker(ex).speak(user, obs, AttentionDecision(Verdict.ASK, 4, 0.8)) == "sent"
    assert ex.delivered == []


def test_security_ask_text_and_lookalike_intent(clock):
    clock.set(T0)

    class Obs:
        id, kind, sender_domain, received_at = 7, "security", "examplemail.com", T0
        facts = {"risk_flags": ["credential_change"]}
        reasons = ["it mentions a password or recovery change"]
        summary, action = "security from examplemail: password changed", ""

    bubbles = ask_text(Obs, "Asia/Kolkata")
    assert bubbles[0].startswith("Quick check: an email about your examplemail account reports a password")
    Obs.kind, Obs.facts = "account_update", {"codes": ["lookalike_domain"]}
    assert "imitates" in notify_intent(Obs, AttentionDecision(Verdict.NOTIFY, 4, 0.7))


def test_format_amount_and_reasons():
    assert format_amount(48000, "INR") == "₹48,000"
    assert format_amount(200000, "INR") == "₹2,00,000"
    assert format_amount(1234.5, "INR") == "₹1,234.50"
    assert format_amount(1500, "USD") == "USD 1,500"
    assert join_reasons(["a"]) == "a" and join_reasons(["a", "b", "c", "d"]) == "a, b and c"
    assert join_reasons([]) == ""


def test_ask_text_uses_day_when_not_today(clock):
    clock.set(T0)

    class Obs:
        id, kind, sender_domain = 8, "money_movement", "examplebank.in"
        received_at = T0 - timedelta(days=2)
        facts = {"money": {**MONEY, "occurred_at": (T0 - timedelta(days=2)).isoformat()}}
        reasons: list[str] = []

    first = ask_text(Obs, "Asia/Kolkata")[0]
    assert "Thu 01 Oct" in first and "It stood out" not in first


def test_ask_text_scrubs_payee_and_rounds_amount(clock):
    clock.set(T0)

    class Obs:
        id, kind, sender_domain, received_at = 9, "money_movement", "examplebank.in", T0
        facts = {"money": {**MONEY, "amount": 99.999, "counterparty_key": "pay.evil.com 9876543210"}}
        reasons: list[str] = []

    first = ask_text(Obs, "Asia/Kolkata")[0]
    assert "an unknown payee" in first and "evil" not in first and "9876543210" not in first
    assert "₹100" in first
    assert len(f"at:n:{10**12}".encode()) <= 64


async def _spent_budget(user, settings, monkeypatch):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    monkeypatch.setattr(settings, "ping_daily_budget", 1)
    await messages.log(user.id, Role.ASSISTANT, "earlier ping", proactive=True)


async def test_security_notify_survives_exhausted_budget(
    user, clock, settings, monkeypatch, recording_bus, fake_memory, fake_llm
):
    from sqlalchemy import select

    from mavis.domain.decisions import ComposedMessage
    from mavis.initiative.wiring import build_initiative
    from mavis.store.db import Session
    from mavis.store.models import OutboxMessage

    async def no_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    clock.set(NOON)
    await _spent_budget(user, settings, monkeypatch)
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    sp = Speaker(lambda: init.executor, PingPolicy(), WakeupService())
    decision = AttentionDecision(Verdict.NOTIFY, 4, 0.8, ("it mentions a new sign-in",))

    plain = await make_obs(
        user.id, "b1", kind="deadline_or_bill", verdict="notify", urgency=3, received_at=NOON
    )
    assert await sp.speak(user, plain, AttentionDecision(Verdict.NOTIFY, 3, 0.8)) == "dropped"

    sec = await make_obs(
        user.id,
        "b2",
        kind="security",
        verdict="notify",
        urgency=4,
        received_at=NOON,
        facts={"risk_flags": ["new_signin"]},
    )
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Was that sign-in you?"]))
    assert await sp.speak(user, sec, decision) == "sent"
    async with Session() as s:
        assert [o.text for o in await s.scalars(select(OutboxMessage))] == ["Was that sign-in you?"]

    bulk = await make_obs(
        user.id,
        "b3",
        kind="security",
        verdict="notify",
        urgency=4,
        received_at=NOON,
        facts={"risk_flags": ["new_signin"], "bulk": ["list_unsubscribe"]},
    )
    assert await sp.speak(user, bulk, decision) == "dropped"


async def test_notify_flags_and_caps(user, clock):
    clock.set(NOON)
    ex = FakeExecutor()
    sec = await make_obs(
        user.id, "c1", kind="account_update", received_at=NOON, facts={"codes": ["lookalike_domain"]}
    )
    await speaker(ex).speak(user, sec, AttentionDecision(Verdict.NOTIFY, 5, 0.9))
    assert ex.notified[0][0].security is True and ex.notified[0][0].urgency == 4
    plain = await make_obs(
        user.id, "c2", kind="deadline_or_bill", received_at=NOON, action="see pay.evil.com now"
    )
    await speaker(ex).speak(user, plain, AttentionDecision(Verdict.NOTIFY, 3, 0.9))
    assert ex.notified[1][0].security is False and "evil" not in ex.notified[1][0].intent


def test_currency_allowlist_and_dedupe_length(clock):
    clock.set(T0)

    class Obs:
        id, kind, sender_domain, received_at = 11, "money_movement", "examplebank.in", T0
        facts = {"money": {**MONEY, "currency": "x.com", "amount": 5}}
        reasons: list[str] = []
        message_id = "m" * 400

    assert "5 was debited" in ask_text(Obs, "Asia/Kolkata")[0] and "[removed]" not in ask_text(Obs, "UTC")[0]
    from mavis.attention.speaker import dedupe_key

    assert len(dedupe_key(Obs)) == 150

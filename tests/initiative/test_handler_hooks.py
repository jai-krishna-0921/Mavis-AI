"""The initiative handler's hook pipeline: prefilters, enrichers, decision policies."""

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import Loop, LoopKind
from mavis.initiative import hooks
from mavis.initiative.filters import FilterResult
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.wiring import build_initiative
from mavis.llm import models as llm


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def event(user) -> Event:
    return Event(
        id="gmail:msg:h1",
        user_id=user.id,
        type=EventType.EMAIL_RECEIVED,
        occurred_at=timeutil.now(),
        source="composio",
        trust=Trust.UNTRUSTED,
        payload={"from": "a@b.com", "subject": "hello", "labels": ["INBOX"]},
    )


def capture_reasoner(monkeypatch) -> dict:
    seen = {"calls": 0}

    async def decide(self, user, ev, result):
        seen["calls"] += 1
        seen["extra"] = result.extra
        return InitiativeDecision(notify=NotifyIntent(urgency=3, intent="x"))

    monkeypatch.setattr(Reasoner, "decide", decide)
    return seen


def capture_executor(init, monkeypatch) -> list:
    applied: list = []

    async def apply(user, decision, ev, **kwargs):
        applied.append(decision)

    monkeypatch.setattr(init.handler._executor, "apply", apply)
    return applied


async def test_prefilters_skipped_when_watch_loop_matched(
    user, clock, recording_bus, fake_memory, monkeypatch
):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    seen = capture_reasoner(monkeypatch)
    capture_executor(init, monkeypatch)
    ran = {"n": 0}

    async def drop(ev):
        ran["n"] += 1
        return "dropped by test"

    monkeypatch.setattr(hooks, "PREFILTERS", [drop])
    monkeypatch.setattr(hooks, "ENRICHERS", [])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [])
    ev = event(user)

    async def no_match(e, loops, tz="UTC"):
        return FilterResult(drop=False, summary="s")

    async def matched(e, loops, tz="UTC"):
        loop = Loop(id=1, user_id=user.id, kind=LoopKind.WATCH, title="referral reply")
        return FilterResult(drop=False, summary="s", matched_loops=[loop])

    monkeypatch.setattr(init.handler._filter, "apply", no_match)
    await init.handler.handle(ev)
    assert ran["n"] == 1 and seen["calls"] == 0  # prefilter dropped it

    monkeypatch.setattr(init.handler._filter, "apply", matched)
    await init.handler.handle(ev)
    assert ran["n"] == 1 and seen["calls"] == 1  # prefilter not consulted, reasoner reached


async def test_policies_run_before_default_dedupe(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    capture_reasoner(monkeypatch)
    applied = capture_executor(init, monkeypatch)
    order: list = []

    async def policy(ev, decision):
        order.append(decision.notify.dedupe_key)  # still unset: dedupe is applied after policies
        return decision.model_copy(update={"notify": decision.notify.model_copy(update={"urgency": 5})})

    monkeypatch.setattr(hooks, "PREFILTERS", [])
    monkeypatch.setattr(hooks, "ENRICHERS", [])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [policy])
    await init.handler.handle(event(user))
    assert order == [None]
    assert applied[0].notify.urgency == 5
    assert applied[0].notify.dedupe_key == "email_received:gmail:msg:h1"


async def test_enricher_extra_reaches_reasoner_prompt(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    prompts: dict = {}

    async def fake_structured(schema, system, user_msg, tier=llm.Tier.FAST, priority="interactive", **_):
        prompts["user"] = user_msg
        return InitiativeDecision(ignore_reason="test")

    monkeypatch.setattr(llm, "structured", fake_structured)
    capture_executor(init, monkeypatch)

    async def enrich(ev):
        return "known_sender=yes; categories=none"

    monkeypatch.setattr(hooks, "PREFILTERS", [])
    monkeypatch.setattr(hooks, "ENRICHERS", [enrich])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [])
    await init.handler.handle(event(user))
    assert "## Mavis signals (computed, trusted)\nknown_sender=yes; categories=none" in prompts["user"]


async def test_raising_hooks_are_skipped_not_fatal(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    seen = capture_reasoner(monkeypatch)
    applied = capture_executor(init, monkeypatch)

    async def bad_prefilter(ev):
        raise RuntimeError("prefilter broke")

    async def bad_policy(ev, decision):
        raise RuntimeError("policy broke")

    async def good_policy(ev, decision):
        return decision.model_copy(update={"notify": decision.notify.model_copy(update={"urgency": 4})})

    monkeypatch.setattr(hooks, "PREFILTERS", [bad_prefilter])
    monkeypatch.setattr(hooks, "ENRICHERS", [])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [bad_policy, good_policy])
    await init.handler.handle(event(user))
    assert seen["calls"] == 1
    assert applied[0].notify.urgency == 4  # later policy still ran, event not failed


async def test_hook_runners_survive_raising_hooks(monkeypatch):
    async def boom(ev):
        raise RuntimeError("x")

    async def boom_policy(ev, decision):
        raise RuntimeError("x")

    monkeypatch.setattr(hooks, "PREFILTERS", [boom])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [boom_policy])
    ev = Event(id="e", user_id=1, type=EventType.EMAIL_RECEIVED, occurred_at=timeutil.now(), source="t")
    assert await hooks.run_prefilters(ev) is None
    decision = InitiativeDecision(ignore_reason="keep")
    assert await hooks.apply_decision_policies(ev, decision) == decision

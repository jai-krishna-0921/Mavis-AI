from datetime import UTC, datetime

from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType
from mavis.initiative import hooks

NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
EV = Event(id="e1", user_id=1, type=EventType.EMAIL_RECEIVED, occurred_at=NOW, source="test")


async def test_hook_runners(monkeypatch):
    async def drop(event):
        return "newsletter"

    async def boom(event):
        raise RuntimeError("enricher broke")

    async def ctx(event):
        return "known_sender=yes"

    async def bump(event, decision):
        return decision.model_copy(update={"notify": NotifyIntent(urgency=4, intent="x")})

    monkeypatch.setattr(hooks, "PREFILTERS", [drop])
    monkeypatch.setattr(hooks, "ENRICHERS", [boom, ctx])
    monkeypatch.setattr(hooks, "DECISION_POLICIES", [bump])
    assert await hooks.run_prefilters(EV) == "newsletter"
    assert await hooks.gather_enrichments(EV) == "known_sender=yes"
    out = await hooks.apply_decision_policies(EV, InitiativeDecision())
    assert out.notify.urgency == 4

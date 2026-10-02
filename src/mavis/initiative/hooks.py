"""Extension points for the initiative pipeline. Integrations plug in here; the core stays generic."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.domain.decisions import InitiativeDecision
from mavis.domain.events import Event

log = structlog.get_logger()

Prefilter = Callable[[Event], Awaitable[str | None]]  # returns a drop reason, or None to keep
Enricher = Callable[[Event], Awaitable[str]]  # returns extra context text ("" if none)
DecisionPolicy = Callable[[Event, InitiativeDecision], Awaitable[InitiativeDecision]]

PREFILTERS: list[Prefilter] = []
ENRICHERS: list[Enricher] = []
DECISION_POLICIES: list[DecisionPolicy] = []


async def run_prefilters(event: Event) -> str | None:
    for fn in PREFILTERS:
        reason = await fn(event)
        if reason:
            return reason
    return None


async def gather_enrichments(event: Event) -> str:
    parts: list[str] = []
    for fn in ENRICHERS:
        try:
            text = await fn(event)
        except Exception as exc:  # noqa: BLE001 - one broken enricher must not block initiative
            log.warning(
                "initiative.enricher_failed",
                enricher=getattr(fn, "__qualname__", str(fn)),
                error=str(exc),
            )
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


async def apply_decision_policies(event: Event, decision: InitiativeDecision) -> InitiativeDecision:
    for fn in DECISION_POLICIES:
        decision = await fn(event, decision)
    return decision

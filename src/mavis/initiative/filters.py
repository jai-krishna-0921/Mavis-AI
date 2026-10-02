"""Cheap, no-LLM first pass over every event (spec §4.3 step 1)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType
from mavis.domain.loops import Loop
from mavis.memory import embeddings

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

EXTERNAL_TYPES = frozenset(
    {EventType.EMAIL_RECEIVED, EventType.SLACK_MESSAGE, EventType.NOTION_CHANGED, EventType.CALENDAR_CHANGED}
)
PROMO_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"})
URGENT = re.compile(
    r"security alert|new sign-?in|password (reset|changed)|suspicious|unusual activity|"
    r"payment (failed|declined)|overdue|interview|offer letter",
    re.IGNORECASE,
)
URGENT_RELEVANCE = 0.9
MAX_SUMMARY = 500


@dataclass
class FilterResult:
    drop: bool
    reason: str = ""
    matched_loops: list[Loop] = field(default_factory=list)
    relevance: float = 0.0
    summary: str = ""


def summarize_event(event: Event) -> str:
    p = event.payload
    match event.type:
        case EventType.EMAIL_RECEIVED:
            text = f"Email from {p.get('from', '?')}: {p.get('subject', '')}: {p.get('snippet', '')}"
        case EventType.SLACK_MESSAGE:
            text = f"Slack message from {p.get('from', '?')} in {p.get('channel', '?')}: {p.get('text', '')}"
        case EventType.CALENDAR_CHANGED:
            text = f"Calendar event '{p.get('title', '')}' at {p.get('starts_at', '?')}"
        case EventType.NOTION_CHANGED:
            text = f"Notion page changed: {p.get('title', '')}"
        case EventType.CONNECTION_CHANGED:
            text = f"Connection {p.get('toolkit', '?')} is now {p.get('state', '?')}"
        case EventType.TASK_COMPLETED | EventType.TASK_PROGRESS:
            text = f"Task '{p.get('goal', '')}': {p.get('summary', '')}"
        case EventType.LOOP_CREATED | EventType.LOOP_UPDATED:
            text = f"Open loop {p.get('kind', '')} '{p.get('title', '')}' due {p.get('due_at') or 'unknown'}"
        case _:
            text = f"{event.type.value}: {p.get('reason', '')}"
    return text[:MAX_SUMMARY]


def watch_matches(loop: Loop, event: Event) -> bool:
    w = loop.watch
    if w is None or (w.deadline is not None and w.deadline < timeutil.now()):
        return False
    p = event.payload
    sender = str(p.get("from", "")).casefold()
    body = f"{p.get('subject', '')} {p.get('snippet', '')} {p.get('text', '')}".casefold()
    checks: list[bool] = []
    if w.from_contains:
        checks.append(w.from_contains.casefold() in sender)
    if w.thread_id:
        checks.append(p.get("thread_id") == w.thread_id)
    if w.keywords:
        checks.append(any(k.casefold() in body for k in w.keywords))
    return bool(checks) and all(checks)


async def _default_embed(texts: list[str]) -> list[list[float]]:
    """Resolved at call time so `set_embedder` overrides (tests) take effect."""
    return await embeddings.get_embedder().embed(texts)


class EventFilter:
    def __init__(self, embed: Embed | None = None) -> None:
        if embed is None:
            embed = _default_embed
        self._embed = embed

    async def apply(self, event: Event, open_loops: list[Loop]) -> FilterResult:
        summary = summarize_event(event)
        if event.type not in EXTERNAL_TYPES:
            loop_id = event.payload.get("loop_id") or event.payload.get("id")
            matched = [lp for lp in open_loops if loop_id is not None and lp.id == int(loop_id)]
            return FilterResult(drop=False, matched_loops=matched, relevance=1.0, summary=summary)

        p = event.payload
        if p.get("from_me"):
            return FilterResult(drop=True, reason="own message", summary=summary)
        matched = [lp for lp in open_loops if watch_matches(lp, event)]
        headers = {str(k).casefold() for k in (p.get("headers") or {})}
        promo = bool(PROMO_LABELS & set(p.get("labels") or [])) or "list-unsubscribe" in headers
        urgent = URGENT.search(summary) is not None
        if promo and not matched and not urgent:
            return FilterResult(drop=True, reason="promotional", summary=summary)
        if matched:
            return FilterResult(drop=False, matched_loops=matched, relevance=1.0, summary=summary)
        similarity = await self._similarity(summary, [lp for lp in open_loops if not lp.watch])
        relevance = max(URGENT_RELEVANCE if urgent else 0.0, similarity)
        return FilterResult(drop=False, relevance=relevance, summary=summary)

    async def _similarity(self, summary: str, loops: list[Loop]) -> float:
        if not loops:
            return 0.0
        vectors = await self._embed([summary] + [lp.title for lp in loops])
        return max(embeddings.cosine(vectors[0], v) for v in vectors[1:])

"""What Mavis has seen in the inbox, injected into a chat turn that asks about it (spec attention 12).

A cheap regex decides whether the turn is about email, updates or money; no extra LLM call. Lines come from
sanitized summaries but still derive from third-party subjects, so they are wrapped untrusted."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

import structlog

from mavis.attention.index import AttentionIndex
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.initiative.untrusted import wrap_untrusted
from mavis.store.repo import attention as repo
from mavis.store.repo import users

log = structlog.get_logger()

EMAIL_INTENT = re.compile(
    r"\b(e-?mails?|inbox|gmail|mails?|updates?|anything (?:new|from)|what did i miss|missed|heard from|"
    r"bank|debit(?:ed)?|credit(?:ed)?|payments?|pay|paid|transactions?|money|spen[dt]|spending|bills?|"
    r"invoices?|alerts?)\b",
    re.IGNORECASE,
)
NOTABLE = frozenset({"ask", "notify", "brief", "forwarded"})
ROUTINE = frozenset({"log", "dropped"})
SEARCH_TIMEOUT_S = 0.4  # inside the context provider's own budget, so the counts still render
MAX_LINES = 8
VERDICT_LABEL = {
    "ask": "you asked them about it",
    "notify": "you told them",
    "brief": "saved for their brief",
    "forwarded": "matches something they are waiting on",
    "log": "routine",
    "dropped": "promotion",
}
FEEDBACK_LABEL = {
    "confirmed": "they said it was them",
    "disputed": "they said it was NOT them",
    "mute": "they asked not to hear about these",
    "always": "they want to hear about these",
}
HEADER = "## What you've seen in their inbox (computed by you from their Gmail)"
GUIDE = (
    "Use this to answer questions about email, updates or money. Never read out links, phone numbers or "
    "addresses; suggest opening Gmail for details. This lists only what was classified as needing them, "
    "and routine mail exists too: to answer about a specific email, read it with mail_read (a listed "
    "message_id works directly) or find it with mail_search before saying it is not there."
)
EMPTY = "No new emails were classified as needing them in this window."


def wants_inbox(text: str) -> bool:
    return EMAIL_INTENT.search(text or "") is not None


def _line(r: Any, tz: str) -> str:
    when = timeutil.to_local(timeutil.ensure_utc(r.received_at), tz)
    label = VERDICT_LABEL.get(r.verdict, r.verdict)
    if r.feedback in FEEDBACK_LABEL:
        label += f"; {FEEDBACK_LABEL[r.feedback]}"
    asks = f" (asks: {r.action})" if r.action else ""
    # the source id, so "read that one" is a mail_read call, not a search on a paraphrase
    ref = f" message_id={r.message_id}" if getattr(r, "source", repo.SOURCE_MAIL) == repo.SOURCE_MAIL else ""
    return f"- {when:%a %d %b %H:%M} {r.summary}{asks} [{label}]{ref}"


def render_digest(rows: list[Any], hits: list[Any], pending: int, tz: str, hours: int) -> str:
    notable = [r for r in rows if r.verdict in NOTABLE]
    routine = sum(1 for r in rows if r.verdict in ROUTINE)
    seen: set[int] = set()
    lines: list[str] = []
    for r in [*notable, *hits]:
        if r.id in seen:
            continue
        seen.add(r.id)
        lines.append(_line(r, tz))
        if len(lines) >= MAX_LINES:
            break
    counts = (
        f"Emails seen in the last {hours}h: {len(rows)}. Needing attention: {len(notable)}. "
        f"Routine, handled quietly: {routine}."
    )
    if pending:
        counts += f" Arrived but not read yet: {pending}."
    body = wrap_untrusted("\n".join(lines), "inbox_digest") if lines else EMPTY  # a fact, not a verdict
    return f"{HEADER}\n{counts}\n{body}\n{GUIDE}"


class Digest:
    def __init__(self, index: AttentionIndex) -> None:
        self._index = index

    async def context(self, user_id: int, text: str) -> str:
        if not wants_inbox(text) or not await repo.has_any(user_id):
            return ""  # never connected: the persona's connection lines handle it
        hours = get_settings().attention_digest_hours
        user = await users.get(user_id)
        rows = await repo.recent(user_id, timeutil.now() - timedelta(hours=hours), limit=60)
        try:
            # its own short budget: a slow embed or Qdrant must not cancel the database-only counts
            ids = await asyncio.wait_for(
                self._index.search(user_id, text, k=3, min_score=0.5), SEARCH_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 - semantic lookup is optional
            log.warning("attention.digest_search_failed", error=type(exc).__name__)
            ids = []
        hits = await repo.by_ids(user_id, ids)
        return render_digest(rows, hits, await repo.pending_count(user_id), user.timezone, hours)

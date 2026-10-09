"""Gmail and Slack records on their way into the knowledge graph.

One entry per source. Each decides from structure only (native.guard filters and the user's mute list),
redacts, and queues one LEARN job per kept record (memory.records). Mail for the bulk lane (promotional
labels, List-Unsubscribe, Precedence: bulk) is left to the existing attention path and never reaches the
graph. Nothing here raises into the caller: a failure to ingest must not stop triage or the poller.

The user's own identifiers (their addresses and Slack ids) are kept in users.state["identities"]; the
connect flow records them (remember_identity), and an address seen on a message the user sent is learned
as theirs.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog

from mavis.domain.events import Event, EventType
from mavis.memory import records
from mavis.store.repo import profile as profile_repo
from mavis.store.repo import users
from mavis.tools.integrations.native import guard
from mavis.tools.integrations.native.guard import IngestDecision

log = structlog.get_logger()
IDENTITY_KEY = "identities"
# (user id, team id, Slack ids to name) -> {slack id: {"name", "email"}}; names are optional context
Directory = Callable[[int, str, frozenset[str]], Awaitable[Mapping[str, Mapping[str, str]]]]


async def load_identities(user_id: int) -> dict[str, Any]:
    raw = (await users.get_state(user_id)).get(IDENTITY_KEY) or {}
    return {"emails": [str(x).lower() for x in raw.get("emails", [])],
            "slack_ids": [str(x) for x in raw.get("slack_ids", [])], "team": str(raw.get("team", ""))}


async def remember_identity(user_id: int, *, emails: tuple[str, ...] = (), slack_ids: tuple[str, ...] = (),
                            team: str = "") -> None:
    def change(cur: dict) -> dict:
        out = dict(cur)
        out["emails"] = sorted({*cur.get("emails", []), *(e.lower() for e in emails if e)})[:20]
        out["slack_ids"] = sorted({*cur.get("slack_ids", []), *(s for s in slack_ids if s)})[:20]
        if team:
            out["team"] = team
        return out

    await users.modify_nested(user_id, IDENTITY_KEY, change)


class ConnectorIngest:
    def __init__(self, submit: Callable[[records.RecordJob], Awaitable[bool]] = records.submit,
                 directory: Directory | None = None) -> None:
        self._submit, self._directory = submit, directory

    async def _names(self, user_id: int) -> list[str]:
        user = await users.get(user_id)
        card = await profile_repo.get(user_id)
        return [n for n in (user.name, card.name) if n]

    async def email(self, user_id: int, n: dict) -> IngestDecision:
        """Queue one normalized email for learning. Returns the decision (graph, bulk or drop)."""
        try:
            ident = await load_identities(user_id)
            if n.get("from_me") or "SENT" in {str(x).upper() for x in n.get("labels") or []}:
                if n.get("from_address"):
                    await remember_identity(user_id, emails=(str(n["from_address"]),))
                return IngestDecision("drop", "own_mail")
            decision = guard.should_ingest_email(n, await guard.load_mute(user_id))
            if decision:
                job = records.email_record(user_id, n, self_ids=ident, self_names=await self._names(user_id))
                if job is not None:
                    await self._submit(job)
            return decision
        except Exception as exc:  # noqa: BLE001 - ingest must never break triage
            log.warning("connector_ingest.email_failed", error=type(exc).__name__, exc_info=True)
            return IngestDecision("drop", "error")

    async def slack(self, user_id: int, n: dict) -> IngestDecision:
        try:
            ident = await load_identities(user_id)
            decision = guard.should_ingest_slack(n, await guard.load_mute(user_id))
            if decision:
                team = str(n.get("team") or ident["team"])
                directory = await self._names_for(user_id, team, n)
                job = records.slack_record(user_id, n, team=team, self_ids=ident, directory=directory,
                                           self_names=await self._names(user_id))
                if job is not None:
                    await self._submit(job)
            return decision
        except Exception as exc:  # noqa: BLE001
            log.warning("connector_ingest.slack_failed", error=type(exc).__name__, exc_info=True)
            return IngestDecision("drop", "error")

    async def _names_for(self, user_id: int, team: str, n: dict) -> Mapping[str, Mapping[str, str]] | None:
        """Display names for the author and the people mentioned, when the directory can say. A failure only
        costs the names: the record is still learned."""
        if self._directory is None:
            return None
        ids = {str(n.get("user") or ""), *(m.group(1) for m in records.MENTION.finditer(str(n.get("text") or "")))}
        ids = frozenset(i for i in ids if i and not (i == str(n.get("user") or "") and n.get("user_name")))
        if not ids:
            return None
        try:
            return await self._directory(user_id, team, ids)
        except Exception as exc:  # noqa: BLE001
            log.info("connector_ingest.directory_failed", error=type(exc).__name__)
            return None

    async def on_email_event(self, event: Event) -> None:
        if event.type is EventType.EMAIL_RECEIVED:
            await self.email(event.user_id, event.payload)

    async def on_slack_event(self, event: Event) -> None:
        if event.type is EventType.SLACK_MESSAGE:
            await self.slack(event.user_id, event.payload)


def register(ingest: ConnectorIngest | None = None) -> ConnectorIngest:
    """Worker wiring: every SLACK_MESSAGE event is also offered to the graph (Gmail goes through
    Intake.on_email, which takes this object as `connectors`)."""
    from mavis.worker.runner import register_event_handler

    ingest = ingest or ConnectorIngest()
    register_event_handler(EventType.SLACK_MESSAGE, ingest.on_slack_event)
    return ingest

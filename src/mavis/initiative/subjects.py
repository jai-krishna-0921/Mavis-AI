"""What a proactive wakeup or ping is about: an explicit subject id, never a guess from words.

A subject is one row Mavis can check by id: a loop, an approval, a task or an attention observation.
Code derives it from the triggering event or from the id the reasoner names, validates that it exists
and belongs to the user, and reads its computed state and source record from the row itself.

Phase B (commitments ledger) replaces loops with ledger items; this module then resolves item ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus
from mavis.domain.tasks import OPEN_APPROVAL_STATUSES, TaskStatus
from mavis.domain.timefmt import due_label, message_stamp
from mavis.initiative.untrusted import wrap_untrusted

LIVE_LOOP_STATUSES = (LoopStatus.OPEN, LoopStatus.AWAITING_REPLY)
LIVE_TASK_STATUSES = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL})
OPEN_APPROVALS = frozenset(s.value for s in OPEN_APPROVAL_STATUSES)


class SubjectKind(StrEnum):
    LOOP = "loop"
    APPROVAL = "approval"
    TASK = "task"
    OBSERVATION = "observation"


@dataclass(frozen=True)
class Subject:
    kind: SubjectKind
    id: int

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.id}"

    @classmethod
    def parse(cls, raw: Any) -> Subject | None:
        if not isinstance(raw, str) or ":" not in raw:
            return None
        kind, _, ident = raw.partition(":")
        try:
            return cls(SubjectKind(kind), int(ident))
        except ValueError:
            return None

    @classmethod
    def of(cls, kind: str | None, ident: Any) -> Subject | None:
        if kind is None or ident is None:
            return None
        try:
            return cls(SubjectKind(kind), int(ident))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class SubjectState:
    subject: Subject
    live: bool        # open, and something Mavis may follow up on (not its own belief or a routine)
    fingerprint: str  # the state that matters for re-checking: changes only when the subject changes
    record: str       # the source record, for grounding composed text (third-party parts wrapped)
    untrusted: bool = False
    loop_id: int | None = None


def _int(raw: Any) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def event_subject(event: Event) -> Subject | None:
    """The subject an event is about, read from explicit ids in its payload (never from its text)."""
    p = event.payload
    if (bound := Subject.parse(p.get("subject"))) is not None:
        return bound
    loop_id = _int(p.get("loop_id"))
    if loop_id is None and event.type in (EventType.LOOP_CREATED, EventType.LOOP_UPDATED):
        loop_id = _int(p.get("id"))
    if loop_id is not None:
        return Subject(SubjectKind.LOOP, loop_id)
    for field, kind in (("task_id", SubjectKind.TASK), ("approval_id", SubjectKind.APPROVAL),
                        ("observation_id", SubjectKind.OBSERVATION)):
        if (ident := _int(p.get(field))) is not None:
            return Subject(kind, ident)
    return None


async def resolve(user_id: int, subject: Subject) -> SubjectState | None:
    """The subject's row as seen now, or None when it does not exist or is not this user's."""
    match subject.kind:
        case SubjectKind.LOOP:
            return await _loop_state(user_id, subject)
        case SubjectKind.APPROVAL:
            return await _approval_state(user_id, subject)
        case SubjectKind.TASK:
            return await _task_state(user_id, subject)
        case SubjectKind.OBSERVATION:
            return await _observation_state(user_id, subject)
    return None


async def _tz(user_id: int) -> str:
    from mavis.store.repo import users

    return (await users.get(user_id)).timezone


async def _loop_state(user_id: int, subject: Subject) -> SubjectState | None:
    from mavis.store.repo import loops

    lp = await loops.get(subject.id)
    if lp is None or lp.user_id != user_id:
        return None
    now, tz = timeutil.now(), await _tz(user_id)
    title = lp.title if lp.trusted else wrap_untrusted(lp.title, "loop")
    created = f", written {message_stamp(lp.created_at, now, tz)}" if lp.created_at else ""
    record = (f"Item [{lp.id}] ({lp.kind.value.lower()}): '{title}'. Status: {lp.status.value}. "
              f"{due_label(lp.due_at, now, tz)}{created}.")
    # Mavis's own beliefs (reasoner origin) and routines are never subjects to chase
    live = (lp.status in LIVE_LOOP_STATUSES and lp.kind is not LoopKind.ROUTINE
            and lp.origin is not LoopOrigin.REASONER)
    due = timeutil.ensure_utc(lp.due_at).isoformat() if lp.due_at else "-"
    return SubjectState(subject, live, f"{lp.status.value}|{due}", record, untrusted=not lp.trusted,
                        loop_id=lp.id)


async def _approval_state(user_id: int, subject: Subject) -> SubjectState | None:
    from mavis.store.repo import approvals

    row = await approvals.get(subject.id)
    if row is None or row.user_id != user_id:
        return None
    record = f"Action needing approval #{row.id} ({row.tool}): status {row.status}. Preview: {row.preview}"
    return SubjectState(subject, row.status in OPEN_APPROVALS, row.status, record,
                        untrusted=bool(getattr(row, "tainted", False)))


async def _task_state(user_id: int, subject: Subject) -> SubjectState | None:
    from mavis.store.repo import tasks

    row = await tasks.get(subject.id)
    if row is None or row.user_id != user_id:
        return None
    record = f"Background task #{row.id}: '{row.goal}'. Status: {row.status}."
    return SubjectState(subject, row.status in LIVE_TASK_STATUSES, row.status, record,
                        untrusted=bool(row.tainted))


async def _observation_state(user_id: int, subject: Subject) -> SubjectState | None:
    from mavis.store.repo import attention

    obs = await attention.get(subject.id)
    if obs is None or obs.user_id != user_id:
        return None
    return SubjectState(subject, obs.feedback is None, f"{obs.verdict}|{obs.feedback or '-'}",
                        observation_record(obs, await _tz(user_id)), untrusted=True)


def observation_record(obs: Any, tz: str) -> str:
    """One email/workspace observation as a source record. Sender and summary are third-party data."""
    received = message_stamp(timeutil.ensure_utc(obs.received_at), timeutil.now(), tz) \
        if obs.received_at else "unknown time"
    domain = f"({obs.sender_domain})" if obs.sender_domain else ""
    sender = " ".join(p for p in (obs.sender_name, domain) if p)
    parts = [f"{(obs.source or 'mail')} observation #{obs.id}, kind {obs.kind}, received {received}",
             f"sender: {sender or 'unknown'}"]
    if obs.summary:
        parts.append(f"what it says: {obs.summary}")
    return wrap_untrusted("; ".join(parts), "subject")

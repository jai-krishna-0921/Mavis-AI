"""Data access for open loops."""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from mavis.domain import timeutil
from mavis.domain.events import Trust
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus, LoopUpsert, WatchSpec, least_trusted
from mavis.store.db import Session
from mavis.store.models import LoopRow

STALE_AFTER = timedelta(days=2)
AWAITING_FOR = timedelta(hours=24)  # a follow-up nobody answered closes its loop after this
EXPIRING_KINDS = (LoopKind.COMMITMENT.value, LoopKind.WAITING_ON.value, LoopKind.WATCH.value)


def to_domain(r: LoopRow) -> Loop:
    return Loop(
        id=r.id,
        user_id=r.user_id,
        kind=LoopKind(r.kind),
        title=r.title,
        due_at=timeutil.ensure_utc(r.due_at),
        entities=list(r.entities or []),
        status=LoopStatus(r.status),
        importance=r.importance,
        watch=WatchSpec.model_validate(r.watch) if r.watch else None,
        source=r.source,
        version=r.version or 1,
        trust=_trust(r.trust),
        origin=_origin(r.origin),
        created_at=timeutil.ensure_utc(r.created_at),
        created_ref=r.created_ref,
        blocked_by=r.blocked_by,
    )


def _trust(raw: str | None) -> Trust:
    try:
        return Trust(raw) if raw else Trust.UNTRUSTED
    except ValueError:
        return Trust.UNTRUSTED


def _origin(raw: str | None) -> LoopOrigin:
    try:
        return LoopOrigin(raw) if raw else LoopOrigin.UNKNOWN
    except ValueError:
        return LoopOrigin.UNKNOWN


def _content(loop: Loop) -> tuple:
    """What a loop says (as opposed to where it is in its lifecycle): a write that changes this carries
    its writer's trust into the loop."""
    return loop.kind, loop.title, loop.due_at, loop.importance, tuple(loop.entities), loop.watch


def _watch_json(data: LoopUpsert) -> dict | None:
    return data.watch.model_dump(mode="json") if data.watch else None


async def insert(user_id: int, data: LoopUpsert) -> Loop:
    now = timeutil.now()
    async with Session() as s:
        row = LoopRow(
            user_id=user_id,
            kind=data.kind.value,
            title=data.title,
            due_at=timeutil.ensure_utc(data.due_at),
            entities=list(data.entities),
            status=data.status.value,
            importance=data.importance,
            watch=_watch_json(data),
            source=data.source,
            created_ref=data.source or None,
            trust=data.trust.value,
            origin=data.origin.value,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return to_domain(row)


async def update(user_id: int, loop_id: int, data: LoopUpsert) -> tuple[Loop, bool] | None:
    """Merge `data` into the loop; returns (loop, changed). Version/updated_at move only on change."""
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        if row is None or row.user_id != user_id:
            return None
        before = to_domain(row)
        given = data.model_fields_set  # partial update: what the writer did not set stays unchanged
        if "kind" in given and data.kind is not None:
            row.kind = data.kind.value
        if "title" in given and data.title:
            row.title = data.title
        if "status" in given:
            row.status = data.status.value
        if "importance" in given:
            row.importance = data.importance
        if "due_at" in given and data.due_at is not None:
            row.due_at = timeutil.ensure_utc(data.due_at)
        if "entities" in given:
            merged = list(row.entities or [])
            merged += [e for e in data.entities if e.casefold() not in {m.casefold() for m in merged}]
            row.entities = merged
        if "watch" in given and data.watch is not None:
            row.watch = _watch_json(data)
        if data.source:
            row.source = data.source
        if _content(to_domain(row)) != _content(before):  # status-only writes keep the loop's trust
            row.trust = least_trusted(before.trust, data.trust).value
        changed = to_domain(row) != before
        if changed:
            row.updated_at = timeutil.now()
            row.version = (row.version or 1) + 1
            await s.commit()
            await s.refresh(row)
        return to_domain(row), changed


async def get(loop_id: int) -> Loop | None:
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        return to_domain(row) if row else None


async def list_open(user_id: int) -> list[Loop]:
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status == LoopStatus.OPEN.value)
        )
        return [to_domain(r) for r in rows]


DUPLICATE_DUE_WINDOW = timedelta(hours=2)
DUPLICATE_SIMILARITY = 0.8
_TIME_TOKEN = re.compile(r"^\d+(?:[:.]\d+)?(?:am|pm|h|hrs?|st|nd|rd|th)?$")
_DUP_STOPWORDS = frozenset(
    "a an the at on by in to for of with and or my me i is it this that next coming about from "
    "am pm today tomorrow tonight tmrw tmr morning afternoon evening night noon midnight "
    "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun "
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)


def title_tokens(title: str) -> list[str]:
    """Title words that identify the thing: no times, dates, weekdays or filler."""
    return [t for t in normalise_title(title).split() if t not in _DUP_STOPWORDS and not _TIME_TOKEN.match(t)]


TYPO_MIN_LEN = 6
TYPO_RATIO = 0.9


def _names(title: str, entities: list[str] | None) -> set[str]:
    """Words that look like names: capitalised after the first word, or listed as entities."""
    words = re.findall(r"[^\W\d_]+", title)
    names = {w.casefold() for w in words[1:] if w[:1].isupper()}
    for e in entities or []:
        names |= set(normalise_title(e).split())
    return names


def similar_titles(a: str, b: str, entities_a: list[str] | None = None,
                   entities_b: list[str] | None = None) -> bool:
    """Same thing said differently: token Jaccard >= 0.8, or one token set (of 2+) inside the other.
    Typos are forgiven only on long, non-name words ("appointmnet"), never on short words or names,
    so "call mom"/"call tom" and "flight to Delhi"/"flight to Dubai" stay apart."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return normalise_title(a) == normalise_title(b)
    sa, sb = set(ta), set(tb)
    names = _names(a, entities_a) | _names(b, entities_b)
    only_a, only_b = sa - sb, sb - sa
    for x in sorted(only_a):
        if len(x) < TYPO_MIN_LEN or x in names:
            continue
        for y in sorted(only_b):
            if len(y) >= TYPO_MIN_LEN and y not in names and \
                    difflib.SequenceMatcher(None, x, y).ratio() >= TYPO_RATIO:
                sb = (sb - {y}) | {x}  # treat the typo as the same word
                only_b = only_b - {y}
                break
    shared = sa & sb
    if len(shared) / len(sa | sb) >= DUPLICATE_SIMILARITY:
        return True
    smaller, larger = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    # a subset merges only when the extra words add no person or name ("Email Raj and Priya" is not
    # "Email Raj"); the caller keeps the more specific title
    return len(smaller) >= 2 and smaller <= larger and not ((larger - smaller) & names)


MATTER_STOPWORDS = frozenset("remind reminder user asked set scheduled schedule please keep time".split())
# words too common to identify a matter on their own
MATTER_GENERIC = frozenset("meeting message email appointment session reminder task thing".split())
MATTER_STEM = 5  # "block" == "blocked", "stretch" == "stretching"
MATTER_DISTINCTIVE = 7  # a single shared word this long identifies the matter ("plumber", "stretch")
SAME_MOMENT_WINDOW = timedelta(minutes=10)


def _stems(text: str) -> dict[str, int]:
    """stem -> length of the longest word with that stem"""
    out: dict[str, int] = {}
    for tok in title_tokens(text):
        if tok not in MATTER_STOPWORDS:
            out[tok[:MATTER_STEM]] = max(out.get(tok[:MATTER_STEM], 0), len(tok))
    return out


def _people(text: str, entities: list[str] | None) -> set[str]:
    """Stems of capitalised words that can be a person (not a date word, an acronym or the user)."""
    words = re.findall(r"[^\W\d_]+", text)
    names = {w.casefold() for w in words[1:] if w[:1].isupper() and not w.isupper()}
    for e in entities or []:
        names |= set(normalise_title(e).split())
    return {n[:MATTER_STEM] for n in names if n not in _DUP_STOPWORDS and n != "user"}


def same_matter(a: str, b: str, entities_a: list[str] | None = None,
                entities_b: list[str] | None = None, *, one_word: bool = True) -> bool:
    """Two texts (loop titles, reminder reasons) that name the same thing in different words, for use when
    they are ALSO at the same moment: they share two content words (or one distinctive word) and neither
    names a different person than the other. "Remind Arjun to drink water" / "Remind User to drink water
    (asked Thu...)", "Keep 2 to 3 PM blocked for solo focus" / "Focus block scheduled". Not "Call mom" /
    "Call Tom". `one_word=False` (two loops): one shared word is never enough ("Dentist appointment" /
    "Call the dentist")."""
    sa, sb = _stems(a), _stems(b)
    shared = set(sa) & set(sb)
    if not shared:
        return False
    # two different people named (Raj vs Priya) are two matters; a name on one side only is usually the
    # user's own ("Remind Arjun to drink water" / "Remind User to drink water")
    names_a = _people(a, entities_a) - shared
    names_b = _people(b, entities_b) - shared
    if names_a and names_b:
        return False
    if len(shared) >= 2:
        return True
    if not one_word:
        return False
    [stem] = shared
    generic = {g[:MATTER_STEM] for g in MATTER_GENERIC}
    return max(sa[stem], sb[stem]) >= MATTER_DISTINCTIVE and stem not in generic


def _due_close(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True
    return abs(timeutil.ensure_utc(a) - timeutil.ensure_utc(b)) <= DUPLICATE_DUE_WINDOW


async def find_open_duplicate(user_id: int, data: LoopUpsert) -> Loop | None:
    """An open loop of the same kind that is the same thing said differently (one utterance often
    yields "Dentist appointment" and "Dentist appointment at 4pm"): similar title and due within 2h,
    or either due missing. An exact match wins over a fuzzy one."""
    due = timeutil.ensure_utc(data.due_at)
    fuzzy: Loop | None = None
    for loop in await list_open(user_id):
        if loop.kind is not data.kind:
            continue
        if loop.title.casefold() == data.title.casefold() and loop.due_at == due:
            return loop
        if fuzzy is None and _due_close(loop.due_at, due) and similar_titles(
                loop.title, data.title, loop.entities, data.entities):
            fuzzy = loop
        # the same moment written by two writers (the tool and LEARN) in different words
        if fuzzy is None and loop.due_at is not None and due is not None and \
                abs(timeutil.ensure_utc(loop.due_at) - due) <= SAME_MOMENT_WINDOW and \
                same_matter(loop.title, data.title, loop.entities, data.entities, one_word=False):
            fuzzy = loop
    return fuzzy


def normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


async def find_recently_closed(user_id: int, title: str, since: datetime) -> Loop | None:
    """A DONE/DROPPED (or awaiting-reply) loop with the same normalised title closed after `since`."""
    wanted = normalise_title(title)
    closed = (LoopStatus.DONE.value, LoopStatus.DROPPED.value, LoopStatus.AWAITING_REPLY.value,
              LoopStatus.BLOCKED.value)  # re-extraction must not open a copy of a blocked loop
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(
                LoopRow.user_id == user_id, LoopRow.status.in_(closed), LoopRow.updated_at >= since
            )
        )
        for r in rows:
            if normalise_title(r.title) == wanted:
                return to_domain(r)
    return None


async def set_status(user_id: int, loop_id: int, status: LoopStatus,
                     blocked_by: str | None = None) -> tuple[Loop, bool] | None:
    """`blocked_by` is kept only with BLOCKED (the failed approval's ref); any other status clears it."""
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status == status.value:
            return to_domain(row), False
        row.status = status.value
        row.blocked_by = blocked_by if status is LoopStatus.BLOCKED else None
        row.updated_at = timeutil.now()
        row.version = (row.version or 1) + 1
        await s.commit()
        await s.refresh(row)
        return to_domain(row), True


_EXPIRY_STATUSES = (LoopStatus.OPEN.value, LoopStatus.AWAITING_REPLY.value, LoopStatus.BLOCKED.value)
# A blocked loop does not wait forever: once its failure has left "recently failed" (48 h) undecided, it
# expires (hotfix4 H1). The user can also drop it (acknowledging the failure) or a later success reopens it.
BLOCKED_FOR = timedelta(hours=48)


async def open_user_ids() -> list[int]:
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow.user_id).where(LoopRow.status.in_(_EXPIRY_STATUSES)).distinct()
        )
        return list(rows)


async def list_live(user_id: int) -> list[Loop]:
    """OPEN and AWAITING loops: everything still live."""
    live = (LoopStatus.OPEN.value, LoopStatus.AWAITING_REPLY.value)
    async with Session() as s:
        rows = await s.scalars(select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status.in_(live)))
        return [to_domain(r) for r in rows]


async def list_done_since(user_id: int, since: datetime) -> list[Loop]:
    async with Session() as s:
        rows = await s.scalars(select(LoopRow).where(
            LoopRow.user_id == user_id, LoopRow.status == LoopStatus.DONE.value, LoopRow.updated_at >= since))
        return [to_domain(r) for r in rows]


async def list_awaiting(user_id: int, since: datetime) -> list[tuple[Loop, datetime]]:
    """Loops waiting on the user's reply to a follow-up sent after `since`, with when they started waiting."""
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id,
                                  LoopRow.status == LoopStatus.AWAITING_REPLY.value,
                                  LoopRow.updated_at >= since)
        )
        return [(to_domain(r), timeutil.ensure_utc(r.updated_at)) for r in rows]


async def expire(user_id: int, now) -> list[Loop]:
    """Mark one user's stale OPEN loops EXPIRED, and expire loops whose follow-up got no reply within
    AWAITING_FOR (silence is not completion); returns the loops that changed."""
    expired: list[Loop] = []
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status.in_(_EXPIRY_STATUSES))
        )
        for row in rows:
            if row.status == LoopStatus.BLOCKED.value:
                if timeutil.ensure_utc(row.updated_at) < now - BLOCKED_FOR:
                    row.status = LoopStatus.EXPIRED.value
                    row.blocked_by = None
                    row.updated_at = now
                    row.version = (row.version or 1) + 1
                    expired.append(to_domain(row))
                continue
            if row.status == LoopStatus.AWAITING_REPLY.value:
                if timeutil.ensure_utc(row.updated_at) < now - AWAITING_FOR:
                    row.status = LoopStatus.EXPIRED.value  # no answer is not an outcome: never DONE
                    row.updated_at = now
                    row.version = (row.version or 1) + 1
                    expired.append(to_domain(row))
                continue
            due = timeutil.ensure_utc(row.due_at)
            deadline = (
                timeutil.ensure_utc(WatchSpec.model_validate(row.watch).deadline) if row.watch else None
            )
            stale_due = row.kind in EXPIRING_KINDS and due is not None and due < now - STALE_AFTER
            stale_watch = deadline is not None and deadline < now
            if stale_due or stale_watch:
                row.status = LoopStatus.EXPIRED.value
                row.updated_at = now
                row.version = (row.version or 1) + 1
                expired.append(to_domain(row))
        await s.commit()
    return expired


async def list_created_in(user_id: int, refs: list[str], statuses: tuple[LoopStatus, ...]) -> list[Loop]:
    """Loops CREATED from one of `refs` (a chat turn's event id, for LEARN loops) in `statuses`. A loop
    that a later turn merely merged into keeps its original `created_ref` and is not returned."""
    if not refs:
        return []
    async with Session() as s:
        rows = await s.scalars(select(LoopRow).where(
            LoopRow.user_id == user_id, LoopRow.created_ref.in_(refs),
            LoopRow.status.in_([x.value for x in statuses])))
        return [to_domain(r) for r in rows]


async def list_blocked_by(user_id: int, refs: list[str]) -> list[Loop]:
    if not refs:
        return []
    async with Session() as s:
        rows = await s.scalars(select(LoopRow).where(
            LoopRow.user_id == user_id, LoopRow.status == LoopStatus.BLOCKED.value,
            LoopRow.blocked_by.in_(refs)))
        return [to_domain(r) for r in rows]

"""Evidence for the personal layer: what is known about one user, computed in code, with its source.

Two halves.

Signals (written when a record is ingested, deterministic): who the user talks to and how often, when their
calendar repeats, how they write. They go to `personal_signals`, one row per (kind, key, source), so the same
record twice counts once and forgetting a source removes exactly what it contributed.

Evidence (read when the layer is built): graph facts, the profile card, open loops and the aggregated
signals, each as an `Evidence` item with an id from `itemids`, a section, a trust tier and its source refs.
Frequencies, recency and calendar patterns are arithmetic here; the model never counts. Conflicts are
settled here too: a user-trust fact beats the user's own records, which beat third-party records.

Tiers: `user` (what they told Mavis or corrected), `self` (their own mail, messages and calendar) and `third`
(only other people's words support it: unconfirmed).
"""

# ruff: noqa: E501

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from mavis.domain import timeutil
from mavis.domain.loops import LoopKind
from mavis.memory import itemids
from mavis.memory.graph import is_derived, source_rank
from mavis.memory.names import is_user, normalize_name
from mavis.store.repo import loops as loops_repo
from mavis.store.repo import personal as personal_repo
from mavis.store.repo import profile as profile_repo

if TYPE_CHECKING:
    from mavis.memory.records import RecordJob
    from mavis.memory.service import MemoryService

log = structlog.get_logger()

SECTIONS = ("identity", "people", "projects", "routines", "preferences", "style", "commitments")
SINGLE_SECTIONS = frozenset({"identity", "style"})  # one answer each: a correction replaces derived lines
LIMITS = {"identity": 5, "people": 6, "projects": 6, "routines": 5, "preferences": 5, "style": 1,
          "commitments": 6}
TIER_USER, TIER_SELF, TIER_THIRD = "user", "self", "third"
TIER_RANK = {TIER_THIRD: 0, TIER_SELF: 1, TIER_USER: 2}
CORRECTION_PREFIX = "vault:correction:"  # source_ref of a user's correction: vault:correction:<section>:<n>
INTERACTION_DAYS = 90
HALF_LIFE_DAYS = 30.0
DIRECTION_WEIGHT = {"sent": 1.5, "received": 1.0, "cc": 0.4, "mention": 0.5, "meeting": 0.8}
MIN_STYLE_SAMPLES = 5
STYLE_WINDOW = 200
EVIDENCE_TEXT_CAP = 220
AUTOMATED_LOCALS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "support", "billing", "info", "hello", "team", "admin",
    "sales", "accounts", "account", "notifications", "notification", "help", "contact", "service", "mail",
    "office", "hr", "finance", "invoice", "invoices", "orders", "alerts", "security", "careers", "press",
    "mailer-daemon", "postmaster",
})
_REL_SECTION = {
    "WORKS_AT": "identity", "STUDIES_AT": "identity", "LOCATED_IN": "identity", "SKILLED_AT": "identity",
    "PREFERS": "preferences", "DISLIKES": "preferences", "STRUGGLES_WITH": "preferences",
}
_SLOT = {"WORKS_AT": "org", "STUDIES_AT": "org", "LOCATED_IN": "place"}  # single-valued: one answer
_PERSON_RELS = frozenset({"COLLEAGUE_OF", "FAMILY_OF", "FRIEND_OF", "KNOWS", "WORKS_AT", "STUDIES_AT",
                          "SUPPORTS", "WITH"})


@dataclass
class Evidence:
    id: str
    kind: str
    section: str
    text: str
    tier: str
    sources: list[str] = field(default_factory=list)
    weight: float = 0.0
    verbatim: bool = False  # a user's correction: shown exactly as written, never rephrased
    free_text: bool = False  # the text is a statement extracted from a record (may quote other people)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.tier != TIER_THIRD


def tier_of(source_ref: str | None) -> str:
    return {2: TIER_USER, 1: TIER_SELF}.get(source_rank(source_ref), TIER_THIRD)


def source_label(source_ref: str | None) -> str:
    """The record a graph edge was learned from ("gmail:abc"), or "" for what the user said themselves."""
    ref = str(source_ref or "")
    if is_derived(ref):
        return ref.split(":", 1)[1]
    return ref if ref.startswith("vault:") else ""


def _clip(text: str, n: int = EVIDENCE_TEXT_CAP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- ingesting signals -------------------------------------------------------------------------------


def script_mix(text: str) -> dict[str, int]:
    """Letters per writing system, by Unicode name ("LATIN", "DEVANAGARI"...)."""
    counts: Counter[str] = Counter()
    for ch in text:
        if ch.isalpha():
            counts[unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0].title()] += 1
    return dict(counts)


def style_meta(body: str) -> dict[str, Any] | None:
    """Measured features of one message the user wrote, or None when there is nothing to measure."""
    from mavis.agents import register  # lazy: the register module pulls in the LLM layer

    words = len(body.split())
    if words == 0:
        return None
    reg = register.measure([body])
    return {"words": words, "formal": reg.formal, "casual": reg.casual, "scripts": script_mix(body)}


async def note_record(job: RecordJob) -> None:
    """Record what a kept record says about the user's relationships and (for their own words) their style.
    Deterministic and idempotent; never raises into the caller."""
    try:
        suppressed = await personal_repo.suppressed_keys(job.user_id)
        for p in job.people:
            if p.is_user:
                continue
            key = itemids.person_key(p.email, p.slack_id, p.canonical())
            if itemids.person_id(key) in suppressed or itemids.entity_id(p.canonical()) in suppressed:
                continue
            if job.authored:
                direction = "mention" if p.role == "mention" else "sent"
            elif p.role == "sender":
                direction = "received"
            else:
                direction = "cc" if p.role == "recipient" else "mention"
            await personal_repo.record_signal(
                job.user_id, "interaction", key, job.source_ref, p.canonical(), job.anchor_at,
                {"dir": direction, "name": p.canonical(), "email": p.email})
        if job.authored and (meta := style_meta(job.text.partition("\n\n")[2].strip())) is not None:
            await personal_repo.record_signal(job.user_id, "style", "msg", job.source_ref, "", job.anchor_at, meta)
    except Exception:  # noqa: BLE001 - evidence gathering must never stop ingest
        log.warning("personal.note_record_failed", exc_info=True)


async def note_meeting(user_id: int, *, title: str, start: datetime, attendees: list[str], tz: str,
                       event_id: str = "") -> None:
    """One occurrence of a calendar event: the evidence for recurring meetings and the people in them."""
    try:
        key = normalize_name(title)
        if not key or key == "no title":
            return
        local = timeutil.to_local(timeutil.ensure_utc(start), tz)
        ref = f"calendar:{event_id or start.isoformat()}"
        await personal_repo.record_signal(user_id, "meeting", key, ref, title.strip(), start,
                                          {"wd": local.weekday(), "hm": f"{local:%H:%M}"})
        suppressed = await personal_repo.suppressed_keys(user_id)
        for a in attendees:
            a = a.strip()
            if not a or "@" not in a:
                continue
            pk = itemids.person_key(email=a)
            if itemids.person_id(pk) in suppressed:
                continue
            await personal_repo.record_signal(user_id, "interaction", pk, ref, a, start,
                                              {"dir": "meeting", "name": "", "email": a.lower()})
    except Exception:  # noqa: BLE001
        log.warning("personal.note_meeting_failed", exc_info=True)


# --- reading evidence --------------------------------------------------------------------------------


@dataclass
class PersonAgg:
    key: str
    name: str = ""
    email: str = ""
    counts: Counter[str] = field(default_factory=Counter)
    score: float = 0.0
    last: datetime | None = None
    refs: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.name or self.email or self.key

    @property
    def automated(self) -> bool:
        local = self.email.partition("@")[0].lower()
        return bool(self.email) and local in AUTOMATED_LOCALS


async def aggregate_people(user_id: int, now: datetime) -> list[PersonAgg]:
    """Everyone the user interacted with in the window, ranked by frequency weighted by recency."""
    rows = await personal_repo.signals(user_id, "interaction", since=now - timedelta(days=INTERACTION_DAYS))
    agg: dict[str, PersonAgg] = {}
    for r in rows:
        meta = r.meta or {}
        a = agg.setdefault(r.key, PersonAgg(key=r.key))
        direction = str(meta.get("dir") or "mention")
        a.counts[direction] += 1
        age_days = max((now - r.at).total_seconds() / 86400, 0.0)
        a.score += DIRECTION_WEIGHT.get(direction, 0.4) * 0.5 ** (age_days / HALF_LIFE_DAYS)
        name = str(meta.get("name") or "")
        if name and "@" not in name and len(name) > len(a.name):
            a.name = name
        a.email = a.email or str(meta.get("email") or "")
        if a.last is None or r.at > a.last:
            a.last = r.at
        if r.source_ref and r.source_ref not in a.refs:
            a.refs.append(r.source_ref)
    out = [a for a in agg.values() if not a.automated]
    return sorted(out, key=lambda a: (-a.score, a.key))


_NAME_OK = re.compile(r"^[^\W\d_]+(?:[ '.\-]+[^\W\d_]+){0,4}\.?$", re.UNICODE)
_ADDR_BAD = re.compile(r"[^A-Za-z0-9._%+@\-]")
NAME_CAP, ADDR_CAP = 40, 64


def safe_address(email: str) -> str:
    """An address reduced to the characters an address has, capped; "" when nothing usable is left."""
    addr = _ADDR_BAD.sub("", email.strip())[:ADDR_CAP]
    return addr if "@" in addr else ""


def safe_label(a: PersonAgg, told: bool) -> str:
    """How the layer names a person. A display name comes from other people's mail, so it is used only when it
    is plain letters (short, no markup or newlines) AND something the user controls vouches for it: they
    named the person themselves, or it matches the address. Otherwise the address (reduced to address
    characters) stands in, and without one a neutral placeholder. Never free text from a third party."""
    name = " ".join(a.name.split())
    addr = safe_address(a.email or (a.key if "@" in a.key else ""))
    if name and len(name) <= NAME_CAP and _NAME_OK.match(name):
        words = [w.casefold().strip(".'-") for w in re.split(r"[ '.\-]+", name) if len(w.strip(".'-")) >= 3]
        local = re.sub(r"[^a-z0-9]", "", addr.partition("@")[0].lower())
        if told or (words and local and any(w in local or local in w for w in words)):
            return name
    return addr or "a contact"


def person_evidence(a: PersonAgg, told: bool) -> Evidence:
    parts = []
    if a.counts["sent"]:
        parts.append(f"{a.counts['sent']} sent")
    if a.counts["received"]:
        parts.append(f"{a.counts['received']} received")
    if a.counts["meeting"]:
        parts.append(f"{a.counts['meeting']} shared meetings")
    if a.counts["cc"] + a.counts["mention"]:
        parts.append(f"{a.counts['cc'] + a.counts['mention']} mentions")
    last = f"; last contact {a.last:%d %b %Y}" if a.last else ""
    own = a.counts["sent"] or a.counts["meeting"]
    tier = TIER_USER if told else TIER_SELF if own else TIER_THIRD
    return Evidence(
        id=itemids.person_id(a.key), kind=itemids.PERSON, section="people",
        text=_clip(f"{safe_label(a, told)}: {', '.join(parts)} in the last {INTERACTION_DAYS} days{last}"),
        tier=tier, sources=a.refs[-3:], weight=a.score,
        meta={"name": a.name, "email": a.email, "key": a.key})


async def meeting_evidence(user_id: int) -> list[Evidence]:
    """Recurring meetings: the same title at the same weekday and time seen more than once."""
    rows = await personal_repo.signals(user_id, "meeting")
    groups: dict[tuple[str, int, str], list[Any]] = defaultdict(list)
    for r in rows:
        meta = r.meta or {}
        groups[(r.key, int(meta.get("wd", 0)), str(meta.get("hm", "")))].append(r)
    out = []
    for (key, wd, hm), occ in groups.items():
        if len(occ) < 2:
            continue
        occ.sort(key=lambda r: r.at)
        gaps = [(b.at - a.at).days for a, b in zip(occ, occ[1:], strict=False)]
        cadence = "weekly" if gaps and 6 <= statistics.median(gaps) <= 8 else "recurring"
        day = timeutil.ensure_utc(occ[0].at).replace(hour=12)
        weekday = (day - timedelta(days=day.weekday()) + timedelta(days=wd)).strftime("%A")
        title = occ[-1].label or key
        out.append(Evidence(
            id=itemids.routine_id(key, wd, hm), kind=itemids.ROUTINE, section="routines",
            text=_clip(f"{title}: {cadence}, {weekday}s at {hm} ({len(occ)} occurrences seen)"),
            tier=TIER_SELF, sources=[o.source_ref for o in occ[-2:]], weight=float(len(occ)),
            meta={"title": title}))
    return out


def _median(values: list[int]) -> int:
    return int(statistics.median(values)) if values else 0


async def style_evidence(user_id: int) -> list[Evidence]:
    rows = (await personal_repo.signals(user_id, "style"))[-STYLE_WINDOW:]
    if len(rows) < MIN_STYLE_SAMPLES:
        return []
    metas = [r.meta or {} for r in rows]
    words = [int(m.get("words", 0)) for m in metas]
    n = len(metas)
    median = _median(words)
    length = "short" if median <= 20 else "medium length" if median <= 60 else "long"
    formal = sum(bool(m.get("formal")) for m in metas)
    casual = sum(bool(m.get("casual")) for m in metas)
    register = "formal" if formal * 2 >= n else "casual" if casual * 2 >= n else "neutral"
    scripts: Counter[str] = Counter()
    for m in metas:
        scripts.update({k: int(v) for k, v in (m.get("scripts") or {}).items()})
    total = sum(scripts.values()) or 1
    mix = ", ".join(f"{name} {round(100 * c / total)}%" for name, c in scripts.most_common(3)
                    if c / total >= 0.1) or "unknown"
    text = (f"Writes {length} messages (median {median} words), {register} register; "
            f"scripts used: {mix}. Based on {n} of their own messages.")
    return [Evidence(id=itemids.make(itemids.STYLE, "authored"), kind=itemids.STYLE, section="style",
                     text=text, tier=TIER_SELF, sources=[r.source_ref for r in rows[-2:]], weight=float(n))]


async def profile_evidence(user_id: int) -> list[Evidence]:
    card = await profile_repo.get(user_id)
    out: list[Evidence] = []

    def add(field_: str, value: str, section: str, text: str) -> None:
        out.append(Evidence(id=itemids.profile_id(field_, value if field_ in
                                                  ("goals", "key_people", "routines", "dislikes", "other") else ""),
                            kind=itemids.PROFILE, section=section, text=_clip(text), tier=TIER_USER,
                            sources=["profile"], weight=2.0, meta={"field": field_, "value": value}))

    if card.name:
        add("name", card.name, "identity", f"Their name is {card.name}")
    if card.tone:
        add("tone", card.tone, "preferences", f"Prefers a {card.tone} tone")
    if card.brevity == "short":
        add("brevity", "short", "preferences", "Prefers short messages")
    for v in card.goals:
        add("goals", v, "projects", f"Goal: {v}")
    for v in card.key_people:
        add("key_people", v, "people", f"Important to them: {v}")
    for v in card.routines:
        add("routines", v, "routines", f"Routine: {v}")
    for v in card.dislikes:
        add("dislikes", v, "preferences", f"Dislikes: {v}")
    for v in card.other:
        add("other", v, "preferences", v)
    return out


_LOOP_SECTION = {LoopKind.COMMITMENT: "commitments", LoopKind.WAITING_ON: "commitments",
                 LoopKind.GOAL: "projects", LoopKind.WATCH: "projects", LoopKind.ROUTINE: "routines"}


async def loop_evidence(user_id: int) -> list[Evidence]:
    out = []
    for lp in await loops_repo.list_open(user_id):
        section = _LOOP_SECTION.get(lp.kind)
        if section is None:
            continue
        kind = {"commitments": "Open commitment" if lp.kind is LoopKind.COMMITMENT else "Waiting on",
                "projects": "Tracking", "routines": "Routine"}[section]
        due = f", due {lp.due_at:%a %d %b %Y}" if lp.due_at else ""
        out.append(Evidence(
            id=itemids.make(itemids.LOOP, str(lp.id)), kind=itemids.LOOP, section=section,
            text=_clip(f"{kind}: {lp.title}{due}"), tier=TIER_USER if lp.trusted else TIER_THIRD,
            sources=[lp.created_ref or lp.source or "loop"], weight=float(lp.importance),
            meta={"loop_id": lp.id}))
    return out


@dataclass
class GraphItem:
    """One current graph edge, with the trust of where it came from."""

    id: str
    subject: str
    rel: str
    obj: str
    statement: str
    source_ref: str
    tier: str
    index: int  # position in the graph's own order (oldest first): recency among equals
    obj_label: str = "Topic"
    subj_label: str = "Topic"


async def graph_items(memory: MemoryService, user_id: int) -> list[GraphItem]:
    await memory.init()
    labels = {normalize_name(e.name): e.label for e in await memory.graph.entities(user_id)}
    out = []
    for i, d in enumerate(await memory.graph.dump(user_id)):
        ref = str(d.get("source_ref") or "")
        out.append(GraphItem(
            id=itemids.fact_id(d["subject"], d["relation"], d["object"]), subject=d["subject"],
            rel=str(d["relation"]).upper(), obj=d["object"], statement=str(d["statement"]),
            source_ref=ref, tier=tier_of(ref), index=i,
            obj_label="User" if is_user(d["object"]) else labels.get(normalize_name(d["object"]), "Topic"),
            subj_label="User" if is_user(d["subject"]) else labels.get(normalize_name(d["subject"]), "Topic")))
    return out


def correction_section(source_ref: str) -> str:
    """The layer section a user's correction was written for ("" when it is not a correction)."""
    if not source_ref.startswith(CORRECTION_PREFIX):
        return ""
    section = source_ref[len(CORRECTION_PREFIX):].split(":", 1)[0]
    return section if section in SECTIONS else ""


def _graph_evidence(items: list[GraphItem], top_people: dict[str, PersonAgg]) -> list[Evidence]:
    """Facts about the user (by relation) and facts that explain the people the user deals with most."""
    out: list[Evidence] = []
    for g in items:
        about_user = is_user(g.subject) or is_user(g.obj)
        correction = correction_section(g.source_ref)
        section = ""
        if correction:
            section = correction
        elif is_user(g.subject) and g.rel in _REL_SECTION:
            section = _REL_SECTION[g.rel]
        elif about_user:
            other_label = g.obj_label if is_user(g.subject) else g.subj_label
            if other_label == "Person":
                if g.rel not in _PERSON_RELS:
                    continue  # "X emailed you": already counted in the interaction signals
                section = "people"
            elif g.rel == "PURSUING" or other_label in ("Project", "Goal", "Topic", "Event", "Organization"):
                section = "projects"
            else:
                continue
        else:
            names = {normalize_name(a.display) for a in top_people.values()} | {
                normalize_name(a.email) for a in top_people.values() if a.email}
            if g.rel in _PERSON_RELS and (normalize_name(g.subject) in names or normalize_name(g.obj) in names):
                section = "people"
            else:
                continue
        out.append(Evidence(
            id=g.id, kind=itemids.FACT, section=section, text=_clip(g.statement), tier=g.tier,
            sources=[s] if (s := source_label(g.source_ref)) else ["told Mavis"],
            weight=float(g.index) / 1000 + (2.0 if g.tier == TIER_USER else 0.0), verbatim=bool(correction),
            free_text=g.tier != TIER_USER,
            meta={"rel": g.rel, "slot": _SLOT.get(g.rel, "") if is_user(g.subject) else "",
                  "subject": g.subject, "object": g.obj}))
    return out


def settle_conflicts(evidence: list[Evidence]) -> list[Evidence]:
    """Precedence, decided in code: for a single-valued slot (where they work, where they live) only the
    highest tier survives, and a user's correction in a one-answer section removes every derived line there."""
    best: dict[str, int] = {}
    for e in evidence:
        slot = e.meta.get("slot")
        if slot:
            best[slot] = max(best.get(slot, 0), TIER_RANK[e.tier])
    replaced = {e.section for e in evidence if e.verbatim and e.section in SINGLE_SECTIONS}
    out = []
    for e in evidence:
        slot = e.meta.get("slot")
        if slot and TIER_RANK[e.tier] < best[slot]:
            continue
        if e.section in replaced and not (e.verbatim or e.tier == TIER_USER):
            continue
        out.append(e)
    return out


def _rank_key(e: Evidence) -> tuple[int, bool, float]:
    return (-TIER_RANK[e.tier], not e.verbatim, -e.weight)


def bound(evidence: list[Evidence]) -> list[Evidence]:
    """At most LIMITS[section] items per section, confirmed and corrected first."""
    out: list[Evidence] = []
    for section in SECTIONS:
        mine = sorted((e for e in evidence if e.section == section), key=_rank_key)
        seen: set[str] = set()
        for e in mine:
            if e.id in seen:
                continue
            seen.add(e.id)
            if len(seen) > LIMITS[section]:
                break
            out.append(e)
    return out


async def gather(memory: MemoryService, user_id: int, now: datetime | None = None) -> list[Evidence]:
    """All evidence for one user's layer, settled and bounded. Reads only this user's rows."""
    now = timeutil.ensure_utc(now) or timeutil.now()
    suppressed = await personal_repo.suppressed_keys(user_id)
    items = [g for g in await graph_items(memory, user_id) if g.id not in suppressed]
    card = await profile_repo.get(user_id)
    told = " ".join(card.key_people).casefold()

    def named(a: PersonAgg) -> bool:
        return bool(a.name) and normalize_name(a.name) in normalize_name(told)

    people = [a for a in await aggregate_people(user_id, now)
              if itemids.person_id(a.key) not in suppressed
              and itemids.entity_id(a.display) not in suppressed
              and (a.counts["sent"] or a.counts["meeting"] or sum(a.counts.values()) >= 2)]
    top = people[: LIMITS["people"]]
    evidence = [person_evidence(a, named(a)) for a in top]
    evidence += _graph_evidence(items, {a.key: a for a in top})
    evidence += await profile_evidence(user_id)
    evidence += await meeting_evidence(user_id)
    evidence += await style_evidence(user_id)
    evidence += await loop_evidence(user_id)
    evidence = [e for e in evidence if e.id not in suppressed]
    return bound(settle_conflicts(evidence))


async def sender_suppressed(job: RecordJob) -> bool:
    """The record's sender is someone the user told Mavis to stop learning about."""
    suppressed = await personal_repo.suppressed_keys(job.user_id)
    if not suppressed:
        return False
    return any(itemids.person_id(itemids.person_key(p.email, p.slack_id, p.canonical())) in suppressed
               or itemids.entity_id(p.canonical()) in suppressed
               for p in job.people if p.role == "sender" and not p.is_user)

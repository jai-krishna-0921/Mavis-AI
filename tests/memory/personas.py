# ruff: noqa: E501
"""Synthetic people for the personal layer tests: a founder, an engineer and a student, each with a mailbox
and Slack history. The founder and the engineer work at the same company and share contacts, on purpose:
whatever one of them does must never show in the other's layer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mavis.attention.connector_ingest import ConnectorIngest, remember_identity
from mavis.domain.errors import LLMError
from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory import personal_layer, records
from mavis.memory.personal_layer import LayerDraft
from mavis.store.repo import users
from mavis.tools.integrations.normalize import normalize_email, normalize_slack


@dataclass
class Persona:
    key: str
    name: str
    email: str
    slack_id: str
    chat_id: int
    team: str = "T0NORTH01"
    user_id: int = 0
    jobs: list = field(default_factory=list)

    @property
    def address(self) -> str:
        return f"{self.name} <{self.email}>"


FOUNDER = Persona("founder", "Anita Rao", "anita@northwind.io", "U0ANITA01", 501)
ENGINEER = Persona("engineer", "Dev Menon", "dev@northwind.io", "U0DEVMEN01", 502)
STUDENT = Persona("student", "Tara Iyer", "tara@students.iitm.ac.in", "U0TARAIY01", 503, team="T0CAMPUS1")

VIKRAM = "Vikram Shah <vikram@northwind.io>"  # the co-founder both Northwind people write to
DANA = "Dana Whitfield <dana@brightcap.vc>"


async def create(p: Persona) -> Persona:
    user, _ = await users.get_or_create_by_chat(p.chat_id, p.name.split()[0])
    p.user_id = user.id
    await remember_identity(user.id, emails=(p.email,), slack_ids=(p.slack_id,), team=p.team)
    return p


def mail(mid: str, sender: str, to: str, subject: str, body: str, *, labels=("INBOX",), auth=False, at="2026-09-28T09:30:00Z"):
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if auth:
        headers.append({"name": "Authentication-Results",
                        "value": "mx.google.com; dkim=pass header.i=@" + sender.split("@")[1].rstrip(">")})
    return normalize_email({"messageId": mid, "threadId": "t-" + mid, "payload": {"headers": headers},
                            "messageText": body, "labelIds": list(labels), "messageTimestamp": at})


def sent(p: Persona, mid: str, to: str, subject: str, body: str, at: str = "2026-09-28T09:30:00Z"):
    return mail(mid, p.address, to, subject, body, labels=("SENT",), at=at)


def slack_msg(user: str, text: str, *, channel="C0PLATFORM1", ts="1790000000.000100", **extra):
    return normalize_slack({"channel": channel, "ts": ts, "user": user, "text": text, **extra})


class Sink:
    def __init__(self) -> None:
        self.jobs: list[records.RecordJob] = []

    async def __call__(self, job: records.RecordJob) -> bool:
        self.jobs.append(job)
        return True


async def ingest(p: Persona, *, mails=(), slacks=()) -> Sink:
    sink = Sink()
    ing = ConnectorIngest(sink)
    for m in mails:
        await ing.email(p.user_id, m)
    for s in slacks:
        await ing.slack(p.user_id, {**s, "team": p.team})
    return sink


def extraction(*, org: str | None = None, role_statement: str = "", project: str | None = None,
               people: tuple[tuple[str, str, str], ...] = ()) -> Extraction:
    """A scripted extraction. people: (name, relation, statement) about the user."""
    ents, rels = [], []
    if org:
        ents.append(Entity(name=org, label="Organization"))
        rels.append(Relation(subject="User", rel="WORKS_AT", object=org, statement=role_statement or f"The user works at {org}."))
    if project:
        ents.append(Entity(name=project, label="Project"))
        rels.append(Relation(subject="User", rel="ABOUT", object=project, statement=f"The user is working on {project}."))
    for name, rel, statement in people:
        ents.append(Entity(name=name, label="Person"))
        rels.append(Relation(subject=name, rel=rel, object="User", statement=statement))
    return Extraction(entities=ents, relations=rels)


# --- a scripted phrasing model ---------------------------------------------------------------------------

_ROW = re.compile(r"^(E\d+) \[(\w+), (\w+)\]: (.*?)(?=\nE\d+ \[|\Z)", re.MULTILINE | re.DOTALL)
_WRAP = re.compile(r"<untrusted[^>]*>\n?(.*?)\n?</untrusted>", re.DOTALL)


@dataclass
class Row:
    alias: str
    section: str
    tier: str
    text: str


def parse_rows(prompt: str) -> list[Row]:
    return [Row(m.group(1), m.group(2), m.group(3), _WRAP.sub(r"\1", m.group(4)).strip())
            for m in _ROW.finditer(prompt)]


class Phraser:
    """Stands in for llm.structured when the layer is built. `mode` decides how well it behaves."""

    def __init__(self, mode: str = "faithful", extractions: list[Extraction] | None = None) -> None:
        self.mode = mode
        self.prompts: list[str] = []
        self.extractions = list(extractions or [])

    async def structured(self, schema, system, user, tier=None, priority="interactive", fallback=None):
        if schema is LayerDraft:
            self.prompts.append(user)
            if self.mode == "error":
                raise LLMError("busy")
            return self.draft(parse_rows(user))
        return self.extractions.pop(0) if self.extractions else Extraction()

    def draft(self, rows: list[Row]) -> LayerDraft:
        D = personal_layer.DraftLine
        faithful = [D(section=r.section, text=r.text, evidence=[r.alias]) for r in rows]
        if self.mode == "faithful":
            return LayerDraft(lines=faithful)
        if self.mode == "empty":
            return LayerDraft(lines=[])
        if self.mode == "invent":
            return LayerDraft(lines=[*faithful[:1], D(section=rows[0].section, text="Works at Globex Corp as CTO",
                                                       evidence=[rows[0].alias])])
        if self.mode == "bad_ids":
            return LayerDraft(lines=[D(section=r.section, text=r.text, evidence=["E999"]) for r in rows])
        if self.mode == "cross_section":
            return LayerDraft(lines=[D(section="style", text=r.text, evidence=[r.alias]) for r in rows
                                     if r.section != "style"])
        if self.mode == "mixed":
            by: dict[str, list[Row]] = {}
            for r in rows:
                by.setdefault(r.section, []).append(r)
            out = []
            for section, rs in by.items():
                own = next((r for r in rs if r.tier != "third"), None)
                third = next((r for r in rs if r.tier == "third"), None)
                if own and third:
                    out.append(D(section=section, text=f"{own.text} and {third.text}", evidence=[own.alias, third.alias]))
            return LayerDraft(lines=out)
        raise AssertionError(self.mode)


def when(days_ago: int, hour: int = 9) -> datetime:
    return datetime(2026, 9, 29, hour, 0, tzinfo=UTC) - timedelta(days=days_ago)


BODY_SERIES_A = "Vikram, the Series A deck is ready for review before the Brightcap meeting on Friday, I will send the cap table tomorrow."


def founder_mailbox(p: Persona) -> list[dict]:
    return [
        sent(p, "f-s1", VIKRAM, "Series A deck", BODY_SERIES_A, "2026-09-26T09:00:00Z"),
        sent(p, "f-s2", VIKRAM, "Hiring plan", "Vikram, here is the hiring plan for the engineering team in Q4, please review it today.", "2026-09-27T09:00:00Z"),
        sent(p, "f-s3", VIKRAM, "Board prep", "Vikram, board prep notes are in the shared folder, can we walk through them on Monday morning?", "2026-09-28T09:00:00Z"),
        sent(p, "f-s4", DANA, "Term sheet", "Dana, thanks for the term sheet, we have a few questions on the liquidation preference and the board seats.", "2026-09-28T10:00:00Z"),
        mail("f-r1", DANA, p.address, "Re: Term sheet", "Happy to jump on a call about the liquidation preference this week.", auth=True, at="2026-09-28T12:00:00Z"),
        mail("f-r2", "Raj Kumar <raj@consultco.biz>", p.address, "Intro", "Hello, we help startups with compliance and would love to talk.", at="2026-09-25T12:00:00Z"),
        mail("f-r3", "Raj Kumar <raj@consultco.biz>", p.address, "Follow up", "Following up on my earlier note about compliance services.", at="2026-09-26T12:00:00Z"),
        mail("f-r4", "Raj Kumar <raj@consultco.biz>", p.address, "Last try", "Last note from me about compliance services for your team.", at="2026-09-27T12:00:00Z"),
    ]


def engineer_mailbox(p: Persona) -> list[dict]:
    return [
        sent(p, "d-s1", VIKRAM, "Atlas gateway", "Vikram, the Atlas gateway migration is on track for staging this week, I will post the rollout notes tonight.", "2026-09-27T09:00:00Z"),
        sent(p, "d-s2", VIKRAM, "On-call", "Vikram, I can take the on-call rotation for the platform team next sprint if that helps with the Atlas gateway work.", "2026-09-28T09:00:00Z"),
        sent(p, "d-s3", "Lena Fischer <lena@northwind.io>", "Review", "Lena, could you review the Atlas gateway pull request before standup tomorrow morning please?", "2026-09-28T11:00:00Z"),
        mail("d-r1", "Lena Fischer <lena@northwind.io>", p.address, "Re: Review", "Looks good, two small comments on the retry logic in the gateway.", auth=True, at="2026-09-28T13:00:00Z"),
    ]


def engineer_slack(p: Persona) -> list[dict]:
    return [slack_msg(p.slack_id, "I am rolling the Atlas gateway migration to staging tonight, ping me if you see errors in the platform channel", ts="1790000001.000100"),
            slack_msg("U0LENA0001", "Can you review the Atlas gateway migration plan before standup tomorrow?", ts="1790000002.000100", user_name="Lena Fischer")]


async def learn_all(memory, sink: Sink, per_job: Extraction | None = None) -> None:
    for job in sink.jobs:
        await records.learn_record(memory, job.user_id, job.payload())


async def build_founder(founder: Persona, memory, install, mode: str = "faithful"):
    ph = install(mode, extractions=[extraction(org="Northwind", role_statement="Anita is the CEO of Northwind.", project="Series A deck")] * 8)
    sink = await ingest(founder, mails=founder_mailbox(founder))
    await learn_all(memory, sink)
    return ph, await personal_layer.build(memory, founder.user_id)

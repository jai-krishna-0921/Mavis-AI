from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from zento.domain.decisions import (
    ComposedMessage,
    InitiativeDecision,
    NotifyIntent,
    Route,
    RouteDecision,
    TaskRequest,
    WakeupRequest,
)
from zento.domain.errors import ApprovalRequired, BudgetExceeded, ConnectionRequired, LLMError, ZentoError
from zento.domain.events import Event, EventType, Job, JobKind, Trust
from zento.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from zento.domain.loops import Loop, LoopKind, LoopStatus, LoopUpsert, WatchSpec
from zento.domain.memory import (
    REL_TYPES,
    SINGLE_VALUED_RELS,
    Entity,
    ExtractedEvent,
    Extraction,
    LoopDraft,
    ProfileUpdate,
    RecallContext,
    Relation,
)
from zento.domain.messages import Button, InboundFile, Outbound, Role
from zento.domain.plans import CriticVerdict, DeckOutline, DocOutline, DocSection, Plan, PlanStep, SlideSpec
from zento.domain.policy import Capability, PolicyVerdict, RiskClass

NOW = datetime(2026, 10, 2, 4, 30, tzinfo=UTC)

SAMPLES: list[BaseModel] = [
    Event(id="tg:update:1", user_id=1, type=EventType.USER_MESSAGE, occurred_at=NOW, source="telegram",
          payload={"text": "hi"}, trust=Trust.USER),
    Job(id="j1", user_id=1, kind=JobKind.LEARN, payload={"text": "x"}),
    Outbound(user_id=1, text="hey", buttons=[[Button(label="OK", data="ok")]], dedupe_key="k"),
    InboundFile(file_id="f", file_name="a.pdf", mime_type="application/pdf", size=10),
    Extraction(
        entities=[Entity(name="Jawahar", label="Person", aliases=["Jawa"])],
        relations=[Relation(subject="User", rel="FRIEND_OF", object="Jawahar",
                            statement="Jawahar is the user's friend.")],
        events=[ExtractedEvent(title="Interview prep", starts_at=NOW, with_people=["Jawahar"], importance=4)],
        loops=[LoopDraft(kind="COMMITMENT", title="Interview prep", due_at=NOW)],
        profile_updates=[ProfileUpdate(field="name", value="Jai")],
        mood="anxious",
    ),
    Loop(id=1, user_id=1, kind=LoopKind.WATCH, title="Reply from recruiter",
         watch=WatchSpec(from_contains="recruiter", keywords=["offer"], deadline=NOW)),
    InitiativeDecision(
        notify=NotifyIntent(urgency=3, intent="pep talk", dedupe_key="loop:1:pre"),
        act=[TaskRequest(goal="draft a reply")],
        track=[LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep", due_at=NOW)],
        wakeups=[WakeupRequest(at=NOW, reason="pep talk", loop_id=1)],
    ),
    RouteDecision(route=Route.TASK, reason="needs research"),
    ComposedMessage(send=True, messages=["a", "b"]),
    Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="i")], deliverable="pptx"),
    CriticVerdict(accept=False, revise_steps=["s1"], feedback="needs sources"),
    DeckOutline(title="t", slides=[SlideSpec(title="s", bullets=["b"])]),
    DocOutline(title="d", sections=[DocSection(heading="h", table=[["a", "b"]])]),
    PolicyVerdict(allow=False, defer_until=NOW, reason="quiet hours"),
    Toolkit(slug="gmail", name="Gmail", description="mail"),
    ToolResult(ok=True, data={"a": 1}),
]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_json_round_trip(obj: BaseModel) -> None:
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj


def test_enum_values_are_stable() -> None:
    assert Role.USER == "user"
    assert ConnectionState.ACTIVE == "ACTIVE"
    assert LoopStatus.OPEN == "OPEN"
    assert Capability.CALENDAR == "googlecalendar"
    assert "RELATED_TO" in REL_TYPES
    assert "WORKS_AT" in SINGLE_VALUED_RELS


def test_risk_classes_needing_approval() -> None:
    assert not RiskClass.READ.needs_approval
    assert not RiskClass.WRITE_SELF.needs_approval
    assert RiskClass.OUTWARD.needs_approval
    assert RiskClass.SPEND.needs_approval
    assert RiskClass.DESTRUCTIVE.needs_approval


def test_user_ref_provider_id() -> None:
    assert UserRef(user_id=7).provider_id == "zento-7"


def test_recall_render_sections() -> None:
    ctx = RecallContext(profile="Jai, job hunting", loops=["Interview Monday"], facts=["Jawahar is a friend"])
    text = ctx.render()
    assert "## About the user\nJai, job hunting" in text
    assert "## Open loops\n- Interview Monday" in text
    assert "## Known facts\n- Jawahar is a friend" in text
    assert "Related past moments" not in text
    assert RecallContext().render() == ""


def test_errors_carry_details() -> None:
    e = ConnectionRequired(Capability.GMAIL, "need inbox access")
    assert e.capability is Capability.GMAIL and e.reason == "need inbox access" and "gmail" in str(e)
    a = ApprovalRequired("mail.send", "To: x", {"to": "x"})
    assert a.action == "mail.send" and a.arguments == {"to": "x"}
    assert issubclass(LLMError, ZentoError) and issubclass(BudgetExceeded, ZentoError)


def test_button_data_limit() -> None:
    with pytest.raises(ValidationError):
        Button(label="x", data="a" * 65)

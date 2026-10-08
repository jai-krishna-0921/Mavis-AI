import asyncio
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from mavis.agents import checkpointing
from mavis.agents import orchestrator_graph as og
from mavis.channels.formatting import to_plain
from mavis.domain.decisions import ComposedMessage
from mavis.domain.errors import BudgetExceeded, ConnectionRequired, LLMError
from mavis.domain.events import EventType
from mavis.domain.plans import CriticVerdict, Plan, PlanStep
from mavis.domain.policy import Capability
from mavis.domain.tasks import ApprovalStatus, StepOutcome, TaskKind, TaskStatus
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from tests.conftest import SendNoteArgs

DASHES = ("\u2014", "\u2013")
CONNECTED = {"type": "connect", "capability": "gmail", "connected": True}
DECLINED = {"type": "connect", "capability": "gmail", "connected": False}


def _plan(*steps: PlanStep) -> Plan:
    return Plan(goal="g", steps=list(steps))


def _graph():
    return og.build_orchestrator().compile(checkpointer=InMemorySaver())


def _cfg(task_id: int, suffix: str = "") -> dict:
    return {"configurable": {"thread_id": f"task:{task_id}{suffix}"}}


async def _run(task_id: int):
    task = await tasks.get(task_id)
    return await _graph().ainvoke(og.initial_state(task), _cfg(task_id))


def test_validate_plan_rejects_cycles():
    with pytest.raises(ValueError, match="cycle"):
        og.validate_plan(_plan(PlanStep(id="s1", agent="research", instruction="a", depends_on=["s2"]),
                               PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"])))


@pytest.mark.parametrize("plan, msg", [
    (Plan(goal="g", steps=[]), "no steps"),
    (Plan(goal="g", steps=[PlanStep(id="s1", agent="wizard", instruction="a")]), "unknown agent"),
    (Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="a", depends_on=["s9"])]),
     "unknown step"),
    (Plan(goal="g", steps=[PlanStep(id="s1", agent="research", instruction="a"),
                          PlanStep(id="s1", agent="research", instruction="b")]), "duplicate"),
])
def test_validate_plan_rejects_bad_plans(plan, msg):
    with pytest.raises(ValueError, match=msg):
        og.validate_plan(plan)


async def _ok_step(step, user_id, context):
    return StepOutcome(ok=True, text="x")


def _timed_step(timeline: list[tuple[str, str]]):
    async def _fake_step(step, user_id, context):
        timeline.append(("start", step.id))
        await asyncio.sleep(0.05)
        timeline.append(("end", step.id))
        return StepOutcome(ok=True, text=f"result {step.id}")
    return _fake_step


async def test_parallel_steps_then_dependent(settings, user, fake_llm, rec_bus, monkeypatch):
    monkeypatch.setattr(settings, "task_step_parallelism", 2)
    timeline: list[tuple[str, str]] = []
    monkeypatch.setattr(og, "run_step_agent", _timed_step(timeline))
    fake_llm.push_structured(_plan(
        PlanStep(id="s1", agent="research", instruction="flights"),
        PlanStep(id="s2", agent="research", instruction="hotels"),
        PlanStep(id="s3", agent="research", instruction="combine", depends_on=["s1", "s2"]),
    ))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Here's your Goa plan."]))
    tid = await tasks.create(user.id, goal="plan a Goa trip")

    out = await _run(tid)

    idx = {e: i for i, e in enumerate(timeline)}
    assert max(idx[("start", "s1")], idx[("start", "s2")]) < min(idx[("end", "s1")], idx[("end", "s2")])
    assert idx[("start", "s3")] > max(idx[("end", "s1")], idx[("end", "s2")])
    assert set(out["results"]) == {"s1", "s2", "s3"}
    assert (await tasks.get(tid)).status == TaskStatus.DONE
    [ev] = [e for e in rec_bus.events if e.type == EventType.TASK_COMPLETED]
    assert ev.payload["messages"] == ["Here's your Goa plan."]
    assert ev.payload["task_id"] == tid


async def test_sequential_by_default(user, fake_llm, rec_bus, monkeypatch):
    timeline: list[tuple[str, str]] = []
    monkeypatch.setattr(og, "run_step_agent", _timed_step(timeline))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="flights"),
                                   PlanStep(id="s2", agent="research", instruction="hotels")))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    assert timeline == [("start", "s1"), ("end", "s1"), ("start", "s2"), ("end", "s2")]


async def test_task_llm_calls_run_at_background_priority(user, fake_llm, rec_bus, monkeypatch):
    seen: list[tuple[str, str, bool | None]] = []
    real = fake_llm.structured

    async def _spy(schema, system, user_msg, tier=None, priority="interactive", fallback=None):
        seen.append((schema.__name__, priority, fallback))
        return await real(schema, system, user_msg, tier=tier, priority=priority, fallback=fallback)

    monkeypatch.setattr(og.llm, "structured", _spy)
    monkeypatch.setattr(og, "run_step_agent", _ok_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b")))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    assert [s[0] for s in seen] == ["Plan", "CriticVerdict", "ComposedMessage"]
    assert all(p == "background" and f is True for _, p, f in seen)


async def test_dependent_step_receives_dependency_output(user, fake_llm, rec_bus, monkeypatch):
    contexts: dict[str, str] = {}

    async def _fake_step(step, user_id, context):
        contexts[step.id] = context
        return StepOutcome(ok=True, text=f"OUT-{step.id}")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"])))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    assert "OUT-s1" in contexts["s2"]
    assert "<untrusted" not in contexts["s2"]


async def test_critic_revise_reruns_only_flagged_step_and_caps_rounds(user, fake_llm, rec_bus, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_step(step, user_id, context):
        calls.append((step.id, context))
        return StepOutcome(ok=True, text=f"r-{step.id}")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b")))
    fake_llm.push_structured(CriticVerdict(accept=False, revise_steps=["s2"], feedback="add prices"))
    fake_llm.push_structured(CriticVerdict(accept=False, revise_steps=["s2"], feedback="still no prices"))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["best effort"]))
    tid = await tasks.create(user.id, goal="g")

    out = await _run(tid)

    assert [c[0] for c in calls].count("s1") == 1
    assert [c[0] for c in calls].count("s2") == 3
    assert "still no prices" in calls[-1][1]
    assert out["revision"] == 2


async def test_single_step_plan_skips_critic(user, fake_llm, rec_bus, monkeypatch):
    monkeypatch.setattr(og, "run_step_agent", _ok_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    assert [c["schema"] for c in fake_llm.structured_calls] == [Plan, ComposedMessage]


async def test_budget_exceeded_step_reports_failure(user, fake_llm, rec_bus, monkeypatch):
    async def _fake_step(step, user_id, context):
        raise BudgetExceeded("more than 12 tool rounds")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a")))
    # single-step plan: no critic call (preflight F26)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["I couldn't finish that part."]))
    tid = await tasks.create(user.id, goal="g")
    out = await _run(tid)
    assert out["results"]["s1"]["ok"] is False
    assert "budget" in out["results"]["s1"]["error"]
    assert (await tasks.get(tid)).status == TaskStatus.FAILED  # nothing got done: never "done" (hotfix4 H1)


async def test_crashing_step_is_reported_not_fatal(user, fake_llm, rec_bus, monkeypatch):
    async def _fake_step(step, user_id, context):
        raise RuntimeError("boom with secret-ish detail")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["That part broke."]))
    tid = await tasks.create(user.id, goal="g")
    out = await _run(tid)
    assert out["results"]["s1"]["error"] == "step failed (RuntimeError)"
    assert (await tasks.get(tid)).status == TaskStatus.FAILED


async def test_invalid_plan_falls_back_to_single_research_step(user, fake_llm, rec_bus, monkeypatch):
    agents_run: list[str] = []

    async def _fake_step(step, user_id, context):
        agents_run.append(step.agent)
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    cyclic = _plan(PlanStep(id="s1", agent="research", instruction="a", depends_on=["s1"]))
    fake_llm.push_structured(cyclic)
    fake_llm.push_structured(cyclic)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    tid = await tasks.create(user.id, goal="what is teamcenter")
    await _run(tid)
    assert agents_run == ["research"]
    assert (await tasks.get(tid)).plan["steps"][0]["instruction"] == "what is teamcenter"


async def test_cancelled_task_stops_before_next_step(user, fake_llm, rec_bus, monkeypatch):
    ran: list[str] = []
    tid = await tasks.create(user.id, goal="g")

    async def _fake_step(step, user_id, context):
        ran.append(step.id)
        await tasks.cancel(user_id, tid)
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"])))
    await _run(tid)
    assert ran == ["s1"]
    assert rec_bus.events == []


async def test_finish_never_overwrites_a_cancel(user, fake_llm, rec_bus, monkeypatch):
    monkeypatch.setattr(og, "run_step_agent", _ok_step)

    async def _cancelling_responder(state):
        await tasks.cancel(state["user_id"], state["task_id"])
        return {"final_messages": ["late"]}

    monkeypatch.setattr(og, "responder", _cancelling_responder)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a")))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    t = await tasks.get(tid)
    assert t.status == TaskStatus.CANCELLED and t.result_text is None
    assert rec_bus.events == []


# --- taint ---------------------------------------------------------------------------


def _taint_recorder(seen: dict[str, bool], contexts: dict[str, str], tainted_outputs: set[str]):
    async def _fake_step(step, user_id, context):
        seen[step.id] = og.step_tainted.get()
        contexts[step.id] = context
        return StepOutcome(ok=True, text=f"OUT-{step.id}", tainted=step.id in tainted_outputs)
    return _fake_step


async def test_tainted_outcome_taints_dependents_only(user, fake_llm, rec_bus, monkeypatch):
    seen: dict[str, bool] = {}
    contexts: dict[str, str] = {}
    monkeypatch.setattr(og, "run_step_agent", _taint_recorder(seen, contexts, {"s1"}))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="inbox", instruction="read mail"),
                                   PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"]),
                                   PlanStep(id="s3", agent="research", instruction="c")))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    out = await _run(tid)
    assert seen == {"s1": False, "s2": True, "s3": False}
    assert '<untrusted source="step_s1">' in contexts["s2"]
    assert out["results"]["s2"]["tainted"] is True  # ran tainted, so its output is tainted too
    responder_prompt = fake_llm.structured_calls[-1]["user"]
    assert '<untrusted source="step_s1">' in responder_prompt


async def test_tainted_task_runs_every_step_tainted(user, fake_llm, rec_bus, monkeypatch):
    seen: dict[str, bool] = {}
    monkeypatch.setattr(og, "run_step_agent", _taint_recorder(seen, {}, set()))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b")))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g", context="from an email", tainted=True)
    await _run(tid)
    assert seen == {"s1": True, "s2": True}
    assert '<untrusted source="task_context">' in fake_llm.structured_calls[0]["user"]


async def test_critic_feedback_after_tainted_output_reruns_tainted(user, fake_llm, rec_bus, monkeypatch):
    runs: list[tuple[str, bool]] = []

    async def _fake_step(step, user_id, context):
        runs.append((step.id, og.step_tainted.get()))
        return StepOutcome(ok=True, text="x", tainted=step.id == "s1")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="inbox", instruction="a"),
                                   PlanStep(id="s2", agent="research", instruction="b")))
    fake_llm.push_structured(CriticVerdict(accept=False, revise_steps=["s2"], feedback="use the email"))
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    tid = await tasks.create(user.id, goal="g")
    await _run(tid)
    assert runs == [("s1", False), ("s2", False), ("s2", True)]


async def test_run_step_agent_passes_taint_to_workers(monkeypatch):
    got: list[tuple[str, bool]] = []

    async def _spec(spec, user_id, instruction, context="", *, tainted=False):
        got.append((spec.name, tainted))
        return StepOutcome(ok=True, text="x")

    async def _spawn(user_id, role, goal, tools, budget=None, context="", *, tainted=False):
        got.append(("spawn", tainted))
        return StepOutcome(ok=True, text="x")

    monkeypatch.setattr(og, "run_specialist", _spec)
    monkeypatch.setattr(og, "spawn_agent", _spawn)
    token = og.step_tainted.set(True)
    try:
        await og.run_step_agent(PlanStep(id="s1", agent="research", instruction="a"), 1, "")
        await og.run_step_agent(PlanStep(id="s2", agent="spawn", instruction="b"), 1, "")
    finally:
        og.step_tainted.reset(token)
    await og.run_step_agent(PlanStep(id="s3", agent="research", instruction="c"), 1, "")
    assert got == [("research", True), ("spawn", True), ("research", False)]


# --- connect gate --------------------------------------------------------------------


def _connect_step(attempts: list[str], connected_after: int):
    async def _fake_step(step, user_id, context):
        attempts.append(step.id)
        if len(attempts) <= connected_after:
            raise ConnectionRequired(Capability.GMAIL, "check and handle your email")
        return StepOutcome(ok=True, text="3 unread from Jawahar")
    return _fake_step


async def test_connect_gate_interrupts_then_reruns_step_when_connected(user, fake_llm, rec_bus, monkeypatch):
    attempts: list[str] = []
    monkeypatch.setattr(og, "run_step_agent", _connect_step(attempts, connected_after=1))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="check my inbox")))
    tid = await tasks.create(user.id, goal="anything from Jawahar?")
    task = await tasks.get(tid)
    graph = _graph()
    cfg = _cfg(tid)
    first = await graph.ainvoke(og.initial_state(task), cfg)
    payload = first["__interrupt__"][0].value
    assert payload == {"type": "connect", "capability": "gmail", "reason": "check and handle your email",
                       "step_ids": ["s1"], "revoked": False}
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Yes: 3 unread from Jawahar."]))
    final = await graph.ainvoke(Command(resume=CONNECTED), cfg)
    assert attempts == ["s1", "s1"]
    assert final["results"]["s1"]["ok"] is True


async def test_connect_gate_declined_continues_without(user, fake_llm, rec_bus, monkeypatch):
    attempts: list[str] = []
    monkeypatch.setattr(og, "run_step_agent", _connect_step(attempts, connected_after=99))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="check my inbox")))
    tid = await tasks.create(user.id, goal="anything from Jawahar?")
    task = await tasks.get(tid)
    graph = _graph()
    cfg = _cfg(tid)
    await graph.ainvoke(og.initial_state(task), cfg)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["I couldn't check Gmail."]))
    final = await graph.ainvoke(Command(resume=DECLINED), cfg)
    assert attempts == ["s1"]
    assert any("chose not to connect gmail" in a for a in final["action_results"])
    note = next(a for a in final["action_results"] if "chose not to connect" in a)
    assert "check my inbox" in note and "s1" not in note  # no internal step ids for the responder
    assert (await tasks.get(tid)).status == TaskStatus.FAILED  # the only step never ran


async def test_dependents_wait_for_the_connect_answer(user, fake_llm, rec_bus, monkeypatch):
    attempts: list[str] = []
    contexts: dict[str, str] = {}

    async def _fake_step(step, user_id, context):
        attempts.append(step.id)
        contexts[step.id] = context
        if step.id == "s1" and attempts.count("s1") == 1:
            raise ConnectionRequired(Capability.GMAIL, "read your email", revoked=True)
        return StepOutcome(ok=True, text=f"OUT-{step.id}")

    monkeypatch.setattr(og, "run_step_agent", _fake_step)
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="inbox", instruction="mail"),
                                   PlanStep(id="s2", agent="research", instruction="b", depends_on=["s1"])))
    tid = await tasks.create(user.id, goal="g")
    graph = _graph()
    first = await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    assert attempts == ["s1"]
    assert first["__interrupt__"][0].value["revoked"] is True
    fake_llm.push_structured(CriticVerdict(accept=True))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    await graph.ainvoke(Command(resume=CONNECTED), _cfg(tid))
    assert attempts == ["s1", "s1", "s2"]
    assert "OUT-s1" in contexts["s2"]


async def test_sqlite_checkpointer_survives_a_restart(settings, user, fake_llm, rec_bus, monkeypatch):
    """The dev checkpointer is a SQLite file: an interrupted task resumes from a fresh saver."""
    attempts: list[str] = []
    monkeypatch.setattr(og, "run_step_agent", _connect_step(attempts, connected_after=1))
    fake_llm.push_structured(_plan(PlanStep(id="s1", agent="research", instruction="check my inbox")))
    tid = await tasks.create(user.id, goal="g")
    async with checkpointing.open_checkpointer() as saver:
        graph = og.build_orchestrator().compile(checkpointer=saver)
        first = await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
        assert first["__interrupt__"][0].value["type"] == "connect"
    assert (settings.data_dir / "checkpoints.db").exists()
    fake_llm.push_structured(ComposedMessage(send=True, messages=["done"]))
    async with checkpointing.open_checkpointer() as saver:
        graph = og.build_orchestrator().compile(checkpointer=saver)
        final = await graph.ainvoke(Command(resume={"type": "connect", "connected": True}), _cfg(tid))
    assert final["results"]["s1"]["ok"] is True
    assert (await tasks.get(tid)).status == TaskStatus.DONE


def test_libpq_url_strips_sqlalchemy_driver():
    assert checkpointing.libpq_url("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert checkpointing.libpq_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# --- approval gate -------------------------------------------------------------------


async def _approval_task(user_id: int, text: str = "hi") -> tuple[int, int]:
    tid = await tasks.create(user_id, goal="approve", kind=TaskKind.APPROVAL)
    aid = await approvals.create(user_id, tid, "send_note", {"text": text}, f"Send note: {text}",
                                 utcnow() + timedelta(hours=48))
    return tid, aid


async def test_ok_is_at_most_once(user, fake_llm, rec_bus, note_tool):
    tid, aid = await _approval_task(user.id)
    task = await tasks.get(tid)
    graph_a, graph_b = _graph(), _graph()
    # Two runs of the same task (e.g. a redelivered resume) both reach the same open approval.
    first_a = await graph_a.ainvoke(og.initial_state(task), _cfg(tid, ":a"))
    first_b = await graph_b.ainvoke(og.initial_state(task), _cfg(tid, ":b"))
    assert first_a["__interrupt__"][0].value == {"type": "approval", "approval_id": aid, "tool": "send_note",
                                                 "preview": "Send note: hi"}
    assert first_b["__interrupt__"][0].value["approval_id"] == aid
    assert await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    ok = Command(resume={"approval_id": aid, "decision": "ok", "instructions": ""})
    out_a = await graph_a.ainvoke(ok, _cfg(tid, ":a"))
    out_b = await graph_b.ainvoke(ok, _cfg(tid, ":b"))
    assert note_tool == ["hi"]
    a = await approvals.get(aid)
    assert a.status == ApprovalStatus.EXECUTED and a.result == "sent: hi"
    assert a.started_at is not None and a.resolved_at is not None  # execution marker, then outcome
    # a plain-string result is model-only: the receipt shows nothing of it (hotfix4 H3)
    assert [to_plain(m) for m in out_a["final_messages"]] == ["Done ✓"]
    assert out_b["final_messages"] == [og.NOTHING_TO_APPROVE_TEXT]
    assert fake_llm.structured_calls == []  # approval responder is deterministic (F22)


async def test_ok_without_resolving_claim_does_not_execute(user, fake_llm, rec_bus, note_tool):
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    again = await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "ok"}), _cfg(tid))
    assert note_tool == []
    assert again["__interrupt__"][0].value["approval_id"] == aid  # re-prompted, still PENDING


async def test_failed_execution_is_recorded_and_reported(user, fake_llm, rec_bus, fresh_registry):
    from mavis.domain.policy import RiskClass
    from mavis.tools.registry import MavisTool

    async def _boom(user_id, args):
        raise RuntimeError("smtp refused")

    fresh_registry.register(MavisTool(name="send_note", description="d", args_model=SendNoteArgs,
                                      risk=RiskClass.OUTWARD, fn=_boom, agents=frozenset({"conversation"})))
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    out = await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "ok"}), _cfg(tid))
    assert (await approvals.get(aid)).status == ApprovalStatus.FAILED
    # an unexpected exception is described in plain words, never quoted (hotfix4 H3)
    shown = [to_plain(m) for m in out["final_messages"]]
    assert shown == ["Tried, but it failed: something went wrong on my side"]


@pytest.mark.parametrize("decision, status, text", [
    ("no", ApprovalStatus.REJECTED, "Okay, not doing that."),
    ("expired", ApprovalStatus.EXPIRED, "That one expired, so I left it."),
])
async def test_reject_and_expire_are_deterministic(
    user, fake_llm, rec_bus, note_tool, decision, status, text
):
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    out = await graph.ainvoke(Command(resume={"approval_id": aid, "decision": decision}), _cfg(tid))
    assert (await approvals.get(aid)).status == status
    assert out["final_messages"] == [text]
    assert note_tool == [] and fake_llm.structured_calls == []
    assert (await tasks.get(tid)).status == TaskStatus.DONE
    [ev] = [e for e in rec_bus.events if e.type == EventType.TASK_COMPLETED]
    assert ev.payload["messages"] == [text]


async def test_edit_revises_args_and_reprompts(user, fake_llm, rec_bus, note_tool):
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    fake_llm.push_structured(SendNoteArgs(text="hi, warmly"))
    out = await graph.ainvoke(
        Command(resume={"approval_id": aid, "decision": "edit", "instructions": "make it warmer"}), _cfg(tid))
    a = await approvals.get(aid)
    assert a.status == ApprovalStatus.PENDING and a.arguments == {"text": "hi, warmly"}
    assert out["__interrupt__"][0].value["preview"] == "Send note: hi, warmly"
    assert fake_llm.structured_calls[0]["schema"] is SendNoteArgs
    assert note_tool == []


async def test_failed_edit_restores_pending_and_tells_the_user(
    user, fake_llm, rec_bus, note_tool, monkeypatch
):
    notices: list[tuple[int, int, int]] = []

    async def _notice(user_id, approval_id, attempt):
        notices.append((user_id, approval_id, attempt))

    monkeypatch.setattr(og, "notify_revise_failed", _notice)
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    for attempt in (1, 2):
        await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
        fake_llm.push_error(LLMError("model down"), structured=True)
        out = await graph.ainvoke(
            Command(resume={"approval_id": aid, "decision": "edit", "instructions": "warmer"}), _cfg(tid))
        a = await approvals.get(aid)
        assert a.status == ApprovalStatus.PENDING and a.arguments == {"text": "hi"}
        assert out["__interrupt__"][0].value["approval_id"] == aid
        assert notices[-1] == (user.id, aid, attempt)
    assert note_tool == []


async def test_notify_revise_failed_never_raises(monkeypatch):
    import sys
    import types

    calls: list[dict] = []

    async def _say(user_id, text, buttons=None, dedupe_key=None):
        calls.append({"user_id": user_id, "text": text, "dedupe_key": dedupe_key})
        raise RuntimeError("outbox down")

    fake = types.ModuleType("mavis.policy.approvals")
    fake.say = _say  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mavis.policy.approvals", fake)
    import mavis.policy as policy_pkg

    monkeypatch.setattr(policy_pkg, "approvals", fake, raising=False)
    await og.notify_revise_failed(7, 42, 2)
    assert calls == [
        {"user_id": 7, "text": og.REVISE_FAILED_TEXT, "dedupe_key": "approval:42:revise_failed:2"}
    ]


def test_user_facing_and_prompt_text_has_no_dashes():
    texts = [og.PLANNER_PROMPT, og.CRITIC_PROMPT, og.RESPONDER_RULES, og.REVISE_PROMPT, og.REVISE_FAILED_TEXT,
             og.NOTHING_TO_APPROVE_TEXT, *og.APPROVAL_TEXT.values()]
    assert not [t for t in texts if any(d in t for d in DASHES)]
    # the failure reason is the tool's own text: shown verbatim, not rewritten
    msgs = og._approval_messages([{"status": "failed", "detail": "bad \u2014 thing"}])
    assert [to_plain(m) for m in msgs] == ["Tried, but it failed: bad \u2014 thing"]


async def test_approved_integration_failure_is_failed_not_done(user, fake_llm, rec_bus, fresh_registry,
                                                               provider, cache, monkeypatch):
    """A provider that answers ok=False must not be recorded as EXECUTED or reported as Done."""
    from mavis.domain.integrations import ConnectionState, ToolResult
    from mavis.tools.integrations import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    tools_mod.register_integration_tools(fresh_registry)
    provider.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)
    provider.results["mail.send"] = ToolResult(ok=False, error="Recipient address rejected")
    tid = await tasks.create(user.id, goal="approve", kind=TaskKind.APPROVAL)
    aid = await approvals.create(user.id, tid, "mail_send",
                                 {"to": ["jawahar@example.com"], "subject": "Late", "body": "10 min late"},
                                 "Email Jawahar", utcnow() + timedelta(hours=48))
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    out = await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "ok"}), _cfg(tid))
    a = await approvals.get(aid)
    assert a.status == ApprovalStatus.FAILED
    shown = [to_plain(m) for m in out["final_messages"]]
    assert shown == ["Tried, but it failed: Gmail reported an error"]  # the kind's words, not the provider's
    assert [e[1] for e in provider.executed] == ["mail.send"]


async def test_stale_reject_is_not_reported(user, fake_llm, rec_bus, note_tool):
    tid, aid = await _approval_task(user.id)
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.set_status(aid, ApprovalStatus.EXPIRED)  # resolved elsewhere first
    out = await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "no"}), _cfg(tid))
    assert (await approvals.get(aid)).status == ApprovalStatus.EXPIRED
    assert out["final_messages"] == [og.NOTHING_TO_APPROVE_TEXT]


def test_clip_result_keeps_the_untrusted_wrapper_closed():
    wrapped = '<untrusted source="mail_send">\n' + "x" * 900 + "\n</untrusted>"
    clipped = og._clip_result(wrapped, 100)
    assert clipped.startswith('<untrusted source="mail_send">\n') and clipped.endswith("\n</untrusted>")
    assert clipped.count("x") == 100
    assert og._clip_result("plain " * 200, 10) == "plain plai"


# --- hotfix4 H3: receipts render user_text only; failures read as the kind's plain words -----------------


async def _approve_once(user_id: int, tool: str, args: dict, preview: str) -> tuple[int, dict]:
    tid = await tasks.create(user_id, goal="approve", kind=TaskKind.APPROVAL)
    aid = await approvals.create(user_id, tid, tool, args, preview, utcnow() + timedelta(hours=48))
    graph = _graph()
    await graph.ainvoke(og.initial_state(await tasks.get(tid)), _cfg(tid))
    await approvals.claim(aid, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    return aid, await graph.ainvoke(Command(resume={"approval_id": aid, "decision": "ok"}), _cfg(tid))


@pytest.mark.parametrize("user_text,note", [
    ("Started background task #7.", "Tell the user you're on it and will report back."),
    ("Reminder set for Thu 08 Oct, 09:00.", "Wakeup #31 set for 2026-10-08T03:30:00+00:00."),
    ("", "QUEUE_ID=99. Do not mention ids to the user."),
])
async def test_receipt_shows_user_text_and_never_the_model_note(user, fake_llm, rec_bus, fresh_registry,
                                                                user_text, note):
    from mavis.domain.policy import RiskClass
    from mavis.domain.results import ToolOutput
    from mavis.tools.registry import MavisTool

    async def _do(user_id, args):
        return ToolOutput(user_text, note)

    fresh_registry.register(MavisTool(name="send_note", description="d", args_model=SendNoteArgs,
                                      risk=RiskClass.OUTWARD, fn=_do, agents=frozenset({"conversation"})))
    aid, out = await _approve_once(user.id, "send_note", {"text": "x"}, "Send note: x")
    shown = "\n".join(to_plain(m) for m in out["final_messages"])
    assert shown == (f"Done ✓\n{user_text}" if user_text else "Done ✓")
    assert note not in shown
    assert note in (await approvals.get(aid)).result  # the model-facing record keeps the full result


@pytest.mark.parametrize("tool,capability,args,error,service", [
    ("calendar_create_event", Capability.CALENDAR,
     {"summary": "Block", "start": "2099-10-08T14:00:00", "attendees": ["a@x.io"]},
     '{"error": {"errors": [{"reason": "invalid", "message": "Invalid attendee email."}], "code": 400}}',
     "Google Calendar"),
    ("slack_send", Capability.SLACK, {"channel": "#ops", "text": "deploy done"},
     '{"ok": false, "error": "channel_not_found"}', "Slack"),
    ("notion_create_page", Capability.NOTION, {"parent_id": "p1", "title": "Notes"},
     '{"status": 429, "code": "rate_limited", "message": "slow down"}', "Notion"),
    ("mail_send", Capability.GMAIL, {"to": ["kim@x.io"], "subject": "Hi", "body": "Hello"},
     'calendar.create_event failed: {"error": {"code": 401, "message": "Request had invalid credentials"}}',
     "Gmail"),
])
async def test_failed_provider_body_never_reaches_the_user(user, fake_llm, rec_bus, fresh_registry, provider,
                                                          cache, monkeypatch, tool, capability, args, error,
                                                          service):
    from mavis.domain.integrations import ConnectionState, ToolResult
    from mavis.tools.integrations import tools as tools_mod
    from mavis.tools.integrations.actions import ACTIONS

    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (provider, cache))
    tools_mod.register_integration_tools(fresh_registry)
    provider.set_state(user.id, capability, ConnectionState.ACTIVE)
    action = next(a for a in ACTIONS if a.replace(".", "_") == tool)
    provider.results[action] = ToolResult(ok=False, error=error)
    aid, out = await _approve_once(user.id, tool, args, "the card")
    shown = "\n".join(to_plain(m) for m in out["final_messages"])
    assert (await approvals.get(aid)).status == ApprovalStatus.FAILED
    assert shown.startswith(f"Tried, but it failed: {service} ")
    assert "{" not in shown and "error" not in shown.lower()


async def test_responder_masks_slurs(user, fake_llm):
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Those faggots at the bank fixed it."]))
    out = await og.responder({"user_id": user.id, "goal": "check the bank", "kind": TaskKind.TASK})
    assert "faggots" not in out["final_messages"][0] and "f*****s" in out["final_messages"][0]

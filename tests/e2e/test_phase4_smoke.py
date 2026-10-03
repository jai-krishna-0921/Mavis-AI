"""Phase 4 startup smoke: the real worker wiring on SQLite and the in-process bus, fakes only.

register_default_handlers + run_worker (startup hooks, event and job consumers), the SQLite LangGraph
checkpointer, the outbox and the outbox sender are all real. Only the LLM, the integration provider,
memory and the channel are fakes.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage

from mavis.channels.outbox_sender import OutboxSender
from mavis.domain.decisions import ComposedMessage
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.integrations import ConnectionState
from mavis.domain.plans import Plan, PlanStep
from mavis.domain.policy import Capability
from mavis.domain.tasks import ApprovalStatus, StepOutcome, TaskKind, TaskOrigin, TaskStatus
from mavis.store.db import utcnow
from mavis.store.repo import approvals, tasks
from mavis.tools.integrations import wiring as integrations_wiring
from mavis.worker.handlers import register_default_handlers
from mavis.worker.runner import run_worker

DASHES = re.compile("[–—]")


def _getter(value):
    def getter():
        return value

    getter.cache_clear = lambda: None  # the autouse reset fixture clears these singletons
    return getter


@pytest.fixture
def integ(monkeypatch, provider, cache):
    from mavis.tools import integrations

    for module in (integrations, integrations_wiring):  # wiring imported the getters by name
        monkeypatch.setattr(module, "get_provider", _getter(provider))
        monkeypatch.setattr(module, "get_connection_cache", _getter(cache))
    return provider


@pytest.fixture
async def worker(settings, db, bus, fake_llm, fake_memory, channel, integ):
    """The worker as `mavis dev` starts it: handlers registered, then run_worker (startup hooks first)."""
    register_default_handlers()
    task = asyncio.create_task(run_worker(bus, "smoke", concurrency=1))
    yield bus
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def settle(bus, channel):
    """Run until the bus is idle, then deliver the outbox to the fake channel."""
    yield
    await asyncio.wait_for(bus.wait_idle(), timeout=20)
    await OutboxSender(channel).run_once()
    assert not bus.dead_events and not bus.dead_jobs


def _message(user_id: int, n: int, text: str) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
                 source="telegram", payload={"text": text}, trust=Trust.USER)


def _tap(user_id: int, n: int, data: str) -> Event:
    return Event(id=f"tg:cb:{n}", user_id=user_id, type=EventType.BUTTON_PRESSED, occurred_at=utcnow(),
                 source="telegram", payload={"data": data}, trust=Trust.USER)


async def test_mail_send_waits_for_approve_then_sends_once_and_says_done(user, worker, fake_llm, integ,
                                                                         channel, settings):
    bus = worker
    integ.set_state(user.id, Capability.GMAIL, ConnectionState.ACTIVE)
    fake_llm.push_ai(AIMessage(content="", tool_calls=[{"name": "mail_send", "id": "c1", "args": {
        "to": ["jawahar@example.com"], "subject": "Running late", "body": "I'll be 10 minutes late."}}]))
    fake_llm.push_text("Ready to send it to Jawahar, waiting for your OK.")

    async with settle(bus, channel):
        await bus.publish(_message(user.id, 1, "email Jawahar that I'll be 10 minutes late"))

    assert integ.executed == []  # nothing goes out before the tap
    assert len(await approvals.open_for_user(user.id)) == 1
    prompts = [s for s in channel.sent if s.buttons]
    assert len(prompts) == 1
    prompt = prompts[0]
    ok = next(b.data for b in prompt.buttons[0] if b.data.endswith(":ok"))
    approval_id = int(ok.split(":")[1])
    # the explanation bubble comes before the buttons (F38)
    assert channel.texts.index("Ready to send it to Jawahar, waiting for your OK.") < channel.texts.index(
        prompt.text)
    ap = await approvals.get(approval_id)
    assert ap.status == ApprovalStatus.PENDING and ap.tool == "mail_send"
    task = await tasks.get(ap.task_id)
    assert task.kind == TaskKind.APPROVAL and task.status == TaskStatus.AWAITING_APPROVAL
    assert (settings.data_dir / "checkpoints.db").exists()  # the dev checkpointer is the SQLite file

    async with settle(bus, channel):
        await bus.publish(_tap(user.id, 2, ok))

    assert [(a, args["to"]) for _, a, args in integ.executed] == [("mail.send", ["jawahar@example.com"])]
    assert any(t.startswith("Done ✓") for t in channel.texts)
    assert (await approvals.get(approval_id)).status == ApprovalStatus.EXECUTED
    assert (await tasks.get(ap.task_id)).status == TaskStatus.DONE

    async with settle(bus, channel):  # a second tap on the same button does nothing more
        await bus.publish(_tap(user.id, 3, ok))
    assert len(integ.executed) == 1
    assert channel.texts[-1] == "That one's already been handled."
    assert not any(DASHES.search(t) for t in channel.texts)


async def test_start_task_runs_and_its_result_is_delivered(user, worker, fake_llm, channel, monkeypatch):
    from mavis.agents import orchestrator_graph as og

    bus = worker
    steps: list[str] = []

    async def step(plan_step, user_id, context):  # the specialist's own loop is covered elsewhere
        steps.append(plan_step.instruction)
        return StepOutcome(ok=True, text="Teamcenter, Windchill and Arena compared on price and fit.")

    monkeypatch.setattr(og, "run_step_agent", step)
    fake_llm.push_ai(AIMessage(content="", tool_calls=[{"name": "start_task", "id": "c1", "args": {
        "goal": "research the top 3 PLM tools and compare them"}}]))
    fake_llm.push_text("On it, I'll report back.")
    fake_llm.push_structured(Plan(goal="compare PLM tools", steps=[
        PlanStep(id="s1", agent="research", instruction="compare the top 3 PLM tools")]))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[
        "Here's the short version: Teamcenter for scale, Windchill for PTC shops, Arena for startups."]))

    async with settle(bus, channel):
        await bus.publish(_message(user.id, 1, "research the top 3 PLM tools and compare them"))

    assert steps == ["compare the top 3 PLM tools"]
    task = await tasks.get(await tasks.by_source_ref(user.id, "turn:tg:update:1:start:0"))
    assert task.origin == TaskOrigin.USER and task.status == TaskStatus.DONE
    assert channel.texts[0] == "On it, I'll report back."
    assert channel.texts[-1].startswith("Here's the short version")
    assert not fake_llm.ai_queue and not fake_llm.structured_queue

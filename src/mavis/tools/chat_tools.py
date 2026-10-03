"""Tools only the chat turn has: hand work to a background task, and send a connect link.

Both read `current_turn` (set by `agents.conversation.run_turn`) for the triggering event, so a retried
turn reuses the same dedupe keys, and the tool loop's `current_run` for taint.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel, Field

from mavis.domain.decisions import TaskRequest
from mavis.domain.messages import Role
from mavis.domain.policy import RiskClass
from mavis.domain.tasks import TaskOrigin
from mavis.store.repo import messages, tasks
from mavis.tools.registry import MavisTool, TaintPolicy, current_run, current_task_id


@dataclass
class TurnInfo:
    event_id: str
    starts: int = 0  # start_task calls so far in this turn (the idempotency key's ordinal)


current_turn: ContextVar[TurnInfo | None] = ContextVar("current_turn", default=None)

START_TASK_RESULT = "Started background task #{id}. Tell the user you're on it and will report back."
TASK_EXISTS_RESULT = ("Task #{id} is already working on this, so no new task was started. Tell the user "
                      "it's already in progress and you'll report back.")
CONNECT_RESULT = "Sent them the connect link and buttons. Don't repeat the link."


class StartTaskArgs(BaseModel):
    goal: str = Field(min_length=3, max_length=2000,
                      description="What to do, in the user's words plus anything needed to do it well")
    context: str = Field(default="", max_length=4000, description="Relevant facts from the conversation")


class ConnectArgs(BaseModel):
    service: str = Field(min_length=2, max_length=40, description="gmail, calendar, slack or notion")


def _tainted() -> bool:
    run = current_run.get()
    return run is not None and run.tainted


async def _approved_from_tainted_task() -> bool:
    """Run by the approval gate (execute_approved sets current_task_id to the APPROVAL task)."""
    task_id = current_task_id.get()
    if task_id is None:
        return False
    task = await tasks.get(task_id)
    return bool(task is not None and task.tainted)


async def start_task(user_id: int, args: StartTaskArgs) -> str:
    # lazy: agents import the registry
    from mavis.agents.task_dispatch import dispatch_task_requests, find_duplicate

    # After third-party content this tool needs the user's approval first (on_taint=APPROVE); once
    # approved, the task is still tainted for every step it runs. The user saw and approved the goal,
    # so hosts named in it count as theirs (web_extract's allowlist in tainted tasks).
    turn = current_turn.get()
    # A redelivered turn (crash before the reply went out) re-runs the model, which words the goal
    # differently, so the key is the call's position in the turn, not its text. Taken before any await,
    # so calls from one AI message (run concurrently, started in call order) keep their order.
    ref = None
    if turn is not None:
        ref = f"turn:{turn.event_id}:start:{turn.starts}"
        turn.starts += 1
    if (dup := await find_duplicate(user_id, args.goal, ref)) is not None:
        return TASK_EXISTS_RESULT.format(id=dup)
    tainted = _tainted() or await _approved_from_tainted_task()
    # The approval preview shows only the goal, so after third-party content the unseen `context`
    # (free text the model chose) is dropped rather than smuggled into the task.
    context = "" if tainted else args.context
    [task_id] = await dispatch_task_requests(
        user_id, [TaskRequest(goal=args.goal, context=context)], TaskOrigin.USER, tainted=tainted,
        source_ref=ref,
    )
    return START_TASK_RESULT.format(id=task_id)


async def connect_account(user_id: int, args: ConnectArgs) -> str:
    from mavis.agents import commands
    from mavis.tools.integrations.wiring import get_connect_flow

    flow = get_connect_flow()
    turn = current_turn.get()
    scope_id = f"{turn.event_id}:connect" if turn else None
    if scope_id is None:
        await commands.handle_connect(user_id, args.service, flow)
        return CONNECT_RESULT
    with flow.reply_scope(scope_id) as scope:
        await commands.handle_connect(user_id, args.service, flow)
    if scope.texts:  # the link and buttons went out directly: keep them in the history too
        await messages.log(user_id, Role.ASSISTANT, "\n\n".join(scope.texts), event_id=f"reply:{scope_id}")
    return CONNECT_RESULT


_CONV = frozenset({"conversation"})

TOOLS = [
    MavisTool("start_task", "Start a background task for multi-step work that takes more than a few "
              "seconds: research, comparisons, plans, drafting documents. You report back when it is done.",
              StartTaskArgs, RiskClass.WRITE_SELF, start_task, _CONV, priority=80,
              preview=lambda a: f"Start a background task: {a.goal}", on_taint=TaintPolicy.APPROVE),
    MavisTool("connect_account", "Send the user a link to connect an account (Gmail, Google Calendar, "
              "Slack, Notion) when they ask to connect one.",
              ConnectArgs, RiskClass.WRITE_SELF, connect_account, _CONV, priority=60),
]

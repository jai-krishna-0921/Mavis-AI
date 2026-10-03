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
from mavis.store.repo import messages
from mavis.tools.registry import MavisTool, current_run


@dataclass(frozen=True)
class TurnInfo:
    event_id: str


current_turn: ContextVar[TurnInfo | None] = ContextVar("current_turn", default=None)

START_TASK_RESULT = "Started background task #{id}. Tell the user you're on it and will report back."
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


async def start_task(user_id: int, args: StartTaskArgs) -> str:
    from mavis.agents.task_dispatch import dispatch_task_requests  # lazy: agents import the registry

    # A task started after the model read third-party content stays tainted for every step it runs.
    [task_id] = await dispatch_task_requests(
        user_id, [TaskRequest(goal=args.goal, context=args.context)], TaskOrigin.USER, tainted=_tainted(),
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
              StartTaskArgs, RiskClass.WRITE_SELF, start_task, _CONV, priority=80),
    MavisTool("connect_account", "Send the user a link to connect an account (Gmail, Google Calendar, "
              "Slack, Notion) when they ask to connect one.",
              ConnectArgs, RiskClass.WRITE_SELF, connect_account, _CONV, priority=60),
]

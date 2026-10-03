"""Mavis's own tools: memory, wakeups, open loops, task board, standing rules."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from mavis import bus
from mavis.domain import timeutil
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.policy import RiskClass
from mavis.loops import service as loops_service
from mavis.memory import service as memory_service
from mavis.store.repo import approvals, policy_rules, tasks, users
from mavis.timers import service as timers_service
from mavis.tools.registry import MavisTool


async def to_utc(user_id: int, dt: datetime) -> datetime:
    """Naive datetimes are interpreted in the user's timezone."""
    if dt.tzinfo is None:
        user = await users.get(user_id)
        dt = dt.replace(tzinfo=ZoneInfo(user.timezone))
    return dt.astimezone(UTC)


class RememberArgs(BaseModel):
    fact: str = Field(min_length=2, max_length=1000, description="The fact to remember, as a sentence")


class ForgetArgs(BaseModel):
    needle: str = Field(min_length=2, max_length=200, description="Word or phrase to delete")


class WakeMeArgs(BaseModel):
    at: datetime = Field(description="ISO-8601 time. Without an offset it is read as the user's local time.")
    reason: str = Field(min_length=2, max_length=300, description="What to do or check when it fires")


class TrackLoopArgs(BaseModel):
    kind: LoopKind
    title: str = Field(min_length=2, max_length=200)
    due_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)


class NoArgs(BaseModel):
    pass


class CancelTaskArgs(BaseModel):
    task_id: int


class KnowArgs(BaseModel):
    topic: str | None = Field(default=None, description="Person/topic to focus on; empty = general")


class PolicyRuleArgs(BaseModel):
    tool: str = Field(description="Tool name the rule applies to, e.g. calendar_create_event")
    field: str = Field(description="Argument name to inspect, e.g. attendees")
    contains: str = Field(min_length=2, description="Text that must appear in that argument")
    description: str = Field(description="The rule in the user's words")


async def remember(user_id: int, args: RememberArgs) -> str:
    await memory_service.get_memory().learn(
        user_id, f"The user asked me to remember: {args.fact}", source_ref="tool:remember"
    )
    return "Saved to memory."


async def forget(user_id: int, args: ForgetArgs) -> str:
    n = await memory_service.get_memory().forget(user_id, args.needle)
    return f"Forgot {n} memories matching '{args.needle}'."


async def wake_me(user_id: int, args: WakeMeArgs) -> str:
    at = await to_utc(user_id, args.at)
    if at <= timeutil.now():
        return "That time is in the past; pick a future time."
    wakeup_id = await timers_service.WakeupService().wake_me(
        user_id, at, f"Reminder the user asked for: {args.reason}", kind="agent", payload={"reminder": True}
    )
    return f"Wakeup #{wakeup_id} set for {at.isoformat()}."


async def track_loop(user_id: int, args: TrackLoopArgs) -> str:
    due = await to_utc(user_id, args.due_at) if args.due_at else None
    loop = await loops_service.LoopService(bus.get_bus()).upsert(
        user_id,
        LoopUpsert(kind=args.kind, title=args.title, due_at=due, entities=args.entities,
                   importance=args.importance, source="tool:track_loop"),
    )
    return f"Tracking loop #{loop.id}: {loop.title}"


async def list_tasks(user_id: int, args: NoArgs) -> str:
    rows = await tasks.active_for_user(user_id)
    if not rows:
        return "No active tasks."
    return "\n".join(f"#{t.id} [{t.status}] {t.goal[:100]}" for t in rows)


async def cancel_task(user_id: int, args: CancelTaskArgs) -> str:
    if not await tasks.cancel(user_id, args.task_id):
        return f"Task #{args.task_id} is not active (or not yours)."
    await approvals.reject_open_for_task(args.task_id)
    return f"Task #{args.task_id} cancelled."


async def what_do_you_know(user_id: int, args: KnowArgs) -> str:
    ctx = await memory_service.get_memory().recall(
        user_id, args.topic or "the user, their people, goals and preferences"
    )
    return ctx.render() or "I don't know much yet."


async def add_policy_rule(user_id: int, args: PolicyRuleArgs) -> str:
    rule_id = await policy_rules.add(user_id, args.tool, args.field, args.contains, args.description)
    return f"Rule #{rule_id} saved: {args.description}"


_CONV = frozenset({"conversation"})

TOOLS = [
    MavisTool("remember", "Store a durable fact the user wants remembered.", RememberArgs,
              RiskClass.WRITE_SELF, remember, _CONV, priority=60),
    MavisTool("forget", "Delete memories matching a word or phrase (asks the user first).", ForgetArgs,
              RiskClass.DESTRUCTIVE, forget, _CONV,
              preview=lambda a: f"Forget everything I know matching “{a.needle}”", priority=30),
    MavisTool("wake_me", "Set a reminder / alarm for yourself to act or check something at a time.",
              WakeMeArgs, RiskClass.WRITE_SELF, wake_me, _CONV, priority=65),
    MavisTool("track_loop", "Track an open loop: commitment, waiting-on, goal, concern, routine or watch.",
              TrackLoopArgs, RiskClass.WRITE_SELF, track_loop, _CONV, priority=55),
    MavisTool("list_tasks", "List background tasks Mavis is working on.", NoArgs,
              RiskClass.READ, list_tasks, _CONV, priority=40),
    MavisTool("cancel_task", "Cancel a background task by id.", CancelTaskArgs,
              RiskClass.WRITE_SELF, cancel_task, _CONV, priority=35),
    MavisTool("what_do_you_know", "Recall what Mavis knows about the user or a person/topic.", KnowArgs,
              RiskClass.READ, what_do_you_know, _CONV, priority=50),
    MavisTool("add_policy_rule", "Save a standing rule so a kind of action no longer needs approval.",
              PolicyRuleArgs, RiskClass.OUTWARD, add_policy_rule, _CONV,
              preview=lambda a: f"Always allow {a.tool} when {a.field} contains “{a.contains}”",
              priority=20),
]

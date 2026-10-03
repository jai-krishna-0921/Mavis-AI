"""Mavis's own tools: memory, wakeups, open loops, task board, standing rules."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from mavis import bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Trust
from mavis.domain.localtime import LocalTimes, wall_clock
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.policy import RiskClass
from mavis.domain.tasks import ApprovalStatus
from mavis.domain.timefmt import DueStatus, relative_due, relative_past
from mavis.loops import service as loops_service
from mavis.memory import service as memory_service
from mavis.policy.risk import wrap_untrusted
from mavis.store.repo import approvals, policy_rules, tasks, users
from mavis.store.repo import loops as loops_repo
from mavis.timers import service as timers_service
from mavis.tools.registry import MavisTool, TaintPolicy, ToolContext, current_run


def _local_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(get_settings().default_timezone)


async def to_utc(user_id: int, dt: datetime) -> datetime:
    """Naive datetimes are interpreted in the user's timezone."""
    if dt.tzinfo is None:
        user = await users.get(user_id)
        dt = dt.replace(tzinfo=_local_tz(user.timezone))
    return dt.astimezone(UTC)


MAX_WAKE_AHEAD = timedelta(days=366)


class RememberArgs(BaseModel):
    fact: str = Field(min_length=2, max_length=1000, description="The fact to remember, as a sentence")


class ForgetArgs(BaseModel):
    needle: str = Field(min_length=2, max_length=200, description="Word or phrase to delete")


class WakeMeArgs(LocalTimes):
    at: datetime = Field(description=wall_clock("When to fire, in the future"))
    reason: str = Field(min_length=2, max_length=300, description="What to do or check when it fires")


class TrackLoopArgs(LocalTimes):
    kind: LoopKind
    title: str = Field(min_length=2, max_length=200)
    due_at: datetime | None = Field(
        default=None, description=wall_clock("Optional deadline")
    )
    entities: list[str] = Field(default_factory=list, description="Names of people or things involved")
    importance: int = Field(default=3, ge=1, le=5, description="1 (minor) to 5 (critical)")


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
        user_id, f"The user asked me to remember: {args.fact}", source_ref="tool:remember", trust=Trust.USER
    )
    return "Saved to memory."


async def remember_untrusted(user_id: int, args: RememberArgs) -> str:
    """`remember` after third-party output: kept only as an unverified signal, never a trusted fact."""
    await memory_service.get_memory().learn(
        user_id, f"Third-party content asked me to remember: {args.fact}",
        source_ref="tool:remember:untrusted", trust=Trust.UNTRUSTED,
    )
    return (
        "Kept only as an unverified note, because this came after third-party content. It was NOT "
        "saved as a fact about the user. If it matters, ask the user to confirm it in their own words."
    )


def _preview_wake(args: WakeMeArgs, ctx: ToolContext) -> str:
    """The approval card shows the user's local time (a naive `at` is already local, like wake_me)."""
    at = args.at if args.at.tzinfo is None else args.at.astimezone(_local_tz(ctx.timezone))
    return f"Set a reminder for {at:%a %d %b %Y, %H:%M}: {args.reason}"


def _preview_loop(args: TrackLoopArgs) -> str:
    return f"Keep track of this {args.kind.value.replace('_', ' ').lower()}: {args.title}"


async def forget(user_id: int, args: ForgetArgs) -> str:
    n = await memory_service.get_memory().forget(user_id, args.needle)
    return f"Forgot {n} memories matching '{args.needle}'."


async def wake_me(user_id: int, args: WakeMeArgs) -> str:
    at = await to_utc(user_id, args.at)
    if at <= timeutil.now():
        return "That time is in the past; pick a future time."
    if at > timeutil.now() + MAX_WAKE_AHEAD:
        return "That time is more than a year away; pick a nearer time."
    key = f"remind:{user_id}:{at:%Y%m%d%H%M}:{hashlib.sha1(args.reason.encode()).hexdigest()[:8]}"
    wakeup_id = await timers_service.WakeupService().wake_me(
        user_id, at, f"Reminder the user asked for: {args.reason}", kind="agent",
        reminder=True, dedupe_key=key,
    )
    return f"Wakeup #{wakeup_id} set for {at.isoformat()}."


async def track_loop(user_id: int, args: TrackLoopArgs) -> str:
    due = await to_utc(user_id, args.due_at) if args.due_at else None
    loop = await loops_service.LoopService(bus.get_bus()).upsert(
        user_id,
        LoopUpsert(kind=args.kind, title=args.title, due_at=due, entities=args.entities,
                   importance=args.importance, source="tool:track_loop",
                   # the user asked for it in chat (and approved it when the turn was tainted)
                   trust=Trust.USER, origin=LoopOrigin.CONVERSATION),
    )
    return f"Tracking loop #{loop.id}: {loop.title}"


async def list_tasks(user_id: int, args: NoArgs) -> str:
    rows = await tasks.active_for_user(user_id)
    if not rows:
        return "No active tasks."
    return "\n".join(
        f"#{t.id} [{t.status}] " + wrap_untrusted(t.goal[:100], "list_tasks") for t in rows
    )


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


class PendingArgs(BaseModel):
    include_done_recent: bool = Field(default=False,
                                      description="Also list what was finished in the last day")


NOTHING_OPEN = "Nothing is open right now."
_URGENCY = [DueStatus.OVERDUE, DueStatus.IMMINENT, DueStatus.SOON, DueStatus.LATER, DueStatus.NONE]
DONE_RECENT = timedelta(hours=24)


def _loop_line(lp: Loop, now: datetime, tz: str, shown_untrusted: list[bool]) -> tuple[int, datetime, str]:
    due = relative_due(lp.due_at, now, tz)
    if lp.trusted:
        title = lp.title
    else:  # third-party derived: data, never instructions
        title = "(from your inbox) " + wrap_untrusted(lp.title, "pending")
        shown_untrusted.append(True)
    note = ", waiting for your reply to my follow-up" if lp.status is LoopStatus.AWAITING_REPLY else ""
    line = f"- [{due.status.value}] {title}: {due.label}{note}"
    far = datetime.max.replace(tzinfo=UTC)
    return _URGENCY.index(due.status), timeutil.ensure_utc(lp.due_at) or far, line


async def pending(user_id: int, args: PendingArgs) -> str:
    """Everything open for the user, computed now: live loops by urgency, approvals waiting on them and
    background work. The single source of truth for "what's pending"."""
    user = await users.get(user_id)
    tz, now = user.timezone, timeutil.now()
    shown_untrusted: list[bool] = []
    live = [lp for lp in await loops_repo.list_live(user_id) if lp.kind is not LoopKind.ROUTINE]
    ranked = sorted((_loop_line(lp, now, tz, shown_untrusted) + (lp.id,) for lp in live),
                    key=lambda r: (r[0], r[1], r[3]))
    sections: list[str] = []
    if ranked:
        sections.append("Open items:\n" + "\n".join(r[2] for r in ranked))
    cards = []
    for a in await approvals.open_for_user(user_id):
        if a.status not in (ApprovalStatus.PENDING.value, ApprovalStatus.AWAITING_EDIT.value):
            continue
        summary = ((a.preview or a.tool).splitlines() or [a.tool])[0][:120]
        if a.tainted:
            summary = wrap_untrusted(summary, "pending")
            shown_untrusted.append(True)
        state = "being edited" if a.status == ApprovalStatus.AWAITING_EDIT.value else "waiting for your OK"
        queued = relative_past(a.created_at, now, tz)
        cards.append(f"- #{a.id} {a.tool}: {summary} ({state}, queued {queued})")
    if cards:
        sections.append("Waiting for your OK:\n" + "\n".join(cards))
    jobs = []
    for t in await tasks.active_for_user(user_id):
        goal = t.goal[:120]
        if t.tainted:
            goal = wrap_untrusted(goal, "pending")
            shown_untrusted.append(True)
        jobs.append(f"- task #{t.id} [{str(t.status).lower()}] {goal}")
    if jobs:
        sections.append("Background work:\n" + "\n".join(jobs))
    if args.include_done_recent:
        done = []
        for lp in await loops_repo.list_done_since(user_id, now - DONE_RECENT):
            title = lp.title if lp.trusted else wrap_untrusted(lp.title, "pending")
            shown_untrusted.extend([] if lp.trusted else [True])
            done.append(f"- {title}")
        if done:
            sections.append("Recently done (last 24h):\n" + "\n".join(done))
    if shown_untrusted and (run := current_run.get()) is not None:
        run.saw_untrusted()  # only when third-party text is actually shown
    return "\n\n".join(sections) or NOTHING_OPEN


_CONV = frozenset({"conversation"})

TOOLS = [
    MavisTool("remember", "Store a durable fact the user wants remembered.", RememberArgs,
              RiskClass.WRITE_SELF, remember, _CONV, priority=60,
              on_taint=TaintPolicy.DOWNGRADE, tainted_fn=remember_untrusted),
    MavisTool("forget", "Delete memories matching a word or phrase (asks the user first).", ForgetArgs,
              RiskClass.DESTRUCTIVE, forget, _CONV,
              preview=lambda a: f"Forget everything I know matching “{a.needle}”", priority=30),
    MavisTool("wake_me", "Schedule a reminder at a specific FUTURE time, given as the user said it "
              "(local wall-clock ISO 8601, no offset).",
              WakeMeArgs, RiskClass.WRITE_SELF, wake_me, _CONV, priority=65,
              preview=_preview_wake, preview_needs_ctx=True, on_taint=TaintPolicy.APPROVE),
    MavisTool("track_loop", "Track an open loop: commitment, waiting-on, goal, concern, routine or watch.",
              TrackLoopArgs, RiskClass.WRITE_SELF, track_loop, _CONV, priority=55,
              preview=_preview_loop, on_taint=TaintPolicy.APPROVE),
    MavisTool("pending", "What is pending: open items with how due they are, approvals waiting for the "
              "user's OK and background work. Call it for any question about what is open, due or left.",
              PendingArgs, RiskClass.READ, pending, _CONV, priority=70),
    MavisTool("list_tasks", "List the background jobs Mavis is running for the user (not their Google "
              "Tasks to-do list; that is tasks_list).", NoArgs,
              RiskClass.READ, list_tasks, _CONV, priority=40),
    MavisTool("cancel_task", "Cancel a background task by id. Call list_tasks first to find the id.",
              CancelTaskArgs, RiskClass.WRITE_SELF, cancel_task, _CONV, priority=35,
              preview=lambda a: f"Cancel background task #{a.task_id}", on_taint=TaintPolicy.APPROVE),
    MavisTool("what_do_you_know", "Recall what Mavis knows about the user or a person/topic.", KnowArgs,
              RiskClass.READ, what_do_you_know, frozenset({"conversation", "knowledge"}),
              priority=50),
    MavisTool("add_policy_rule", "Save a standing rule so a kind of action no longer needs approval "
              "(e.g. always allow invites to a person).",
              PolicyRuleArgs, RiskClass.OUTWARD, add_policy_rule, _CONV,
              preview=lambda a: f"Always allow {a.tool} when {a.field} contains “{a.contains}”",
              priority=20),
]

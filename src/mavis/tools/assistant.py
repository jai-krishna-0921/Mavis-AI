"""Mavis's own tools: memory, wakeups, open loops, task board, standing rules."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from mavis import bus
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.args import ToolArgs
from mavis.domain.errors import ActionFailed, FailureKind
from mavis.domain.events import Trust
from mavis.domain.localtime import LocalTimes, wall_clock
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.policy import RiskClass
from mavis.domain.results import ToolOutput
from mavis.domain.tasks import ApprovalStatus
from mavis.domain.timefmt import DueStatus, relative_due, relative_past
from mavis.loops import service as loops_service
from mavis.memory import service as memory_service
from mavis.policy import outcomes
from mavis.policy.risk import wrap_untrusted
from mavis.store.repo import approvals, policy_rules, tasks, users
from mavis.store.repo import loops as loops_repo
from mavis.timers import service as timers_service
from mavis.tools.registry import MavisTool, TaintPolicy, ToolContext, call_untrusted, current_run


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


class RememberArgs(ToolArgs):
    fact: str = Field(min_length=2, max_length=1000, description="The fact to remember, as a sentence")


class ForgetArgs(ToolArgs):
    needle: str = Field(min_length=2, max_length=200, description="Word or phrase to delete")


class WakeMeArgs(LocalTimes):
    at: datetime = Field(description=wall_clock("When to fire, in the future"))
    reason: str = Field(min_length=2, max_length=300, description="What to do or check when it fires")


class TrackLoopArgs(LocalTimes):
    kind: LoopKind
    title: str = Field(min_length=2, max_length=200, description=(
        "What it is, read days later: write any day as an absolute date (Sun 4 Oct), never today or "
        "tomorrow"))
    due_at: datetime | None = Field(
        default=None, description=wall_clock("Optional deadline")
    )
    entities: list[str] = Field(default_factory=list, description="Names of people or things involved")
    importance: int = Field(default=3, ge=1, le=5, description="1 (minor) to 5 (critical)")


class NoArgs(ToolArgs):
    pass


class CancelTaskArgs(ToolArgs):
    task_id: int


class KnowArgs(ToolArgs):
    topic: str | None = Field(default=None, description="Person/topic to focus on; empty = general")


class PolicyRuleArgs(ToolArgs):
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


async def forget(user_id: int, args: ForgetArgs) -> ToolOutput:
    n = await memory_service.get_memory().forget(user_id, args.needle)
    return ToolOutput(f"Forgot {n} memories matching '{args.needle}'.")


async def wake_me(user_id: int, args: WakeMeArgs) -> ToolOutput:
    at = await to_utc(user_id, args.at)
    # a soft failure is an ActionFailed (the model reads the sentence; an approved run reports it as
    # failed, never as "Done")
    if at <= timeutil.now():
        raise ActionFailed("That time is in the past; pick a future time.",
                           reason="that time had already passed", kind=FailureKind.INVALID_ARGUMENT)
    if at > timeutil.now() + MAX_WAKE_AHEAD:
        raise ActionFailed("That time is more than a year away; pick a nearer time.",
                           reason="that time is more than a year away", kind=FailureKind.INVALID_ARGUMENT)
    key = f"remind:{user_id}:{at:%Y%m%d%H%M}:{hashlib.sha1(args.reason.encode()).hexdigest()[:8]}"
    # A reason derived from third-party text (registry.call_untrusted) fires on the untrusted path: the
    # composer words it, scrubbed of links and addresses, never relayed verbatim as "your reminder".
    wakeup_id = await timers_service.WakeupService().wake_me(
        user_id, at, f"Reminder the user asked for: {args.reason}", kind="agent",
        reminder=True, dedupe_key=key, payload={"untrusted": True} if call_untrusted() else None,
    )
    # args.at is the user's wall clock (the registry attached their zone), so it reads as they said it
    return ToolOutput(f"Reminder set for {args.at:%a %d %b, %H:%M}.",
                      f"Wakeup #{wakeup_id} set for {at.isoformat()}.")


async def track_loop(user_id: int, args: TrackLoopArgs) -> ToolOutput:
    due = await to_utc(user_id, args.due_at) if args.due_at else None
    loop = await loops_service.LoopService(bus.get_bus()).upsert(
        user_id,
        LoopUpsert(kind=args.kind, title=args.title, due_at=due, entities=args.entities,
                   importance=args.importance, source="tool:track_loop",
                   # the user's own request (their words, or a card they approved); worded from
                   # third-party text without a card, it is stored as untrusted (registry.call_untrusted)
                   trust=Trust.UNTRUSTED if call_untrusted() else Trust.USER,
                   origin=LoopOrigin.CONVERSATION),
    )
    return ToolOutput(f"Keeping track of: {loop.title}", f"Tracking loop #{loop.id}.")


async def list_tasks(user_id: int, args: NoArgs) -> str:
    rows = await tasks.active_for_user(user_id)
    if not rows:
        return "No active tasks."
    return "\n".join(
        f"#{t.id} [{t.status}] " + wrap_untrusted(t.goal[:100], "list_tasks") for t in rows
    )


async def cancel_task(user_id: int, args: CancelTaskArgs) -> ToolOutput:
    if not await tasks.cancel(user_id, args.task_id):
        raise ActionFailed(f"Task #{args.task_id} is not active (or not yours).",
                           reason=f"task #{args.task_id} was not running, so there was nothing to cancel",
                           kind=FailureKind.NOT_FOUND)
    await approvals.reject_open_for_task(args.task_id)
    return ToolOutput(f"Task #{args.task_id} cancelled.")


async def what_do_you_know(user_id: int, args: KnowArgs) -> str:
    ctx = await memory_service.get_memory().recall(
        user_id, args.topic or "the user, their people, goals and preferences"
    )
    return ctx.render() or "I don't know much yet."


async def add_policy_rule(user_id: int, args: PolicyRuleArgs) -> ToolOutput:
    rule_id = await policy_rules.add(user_id, args.tool, args.field, args.contains, args.description)
    return ToolOutput(f"Rule saved: {args.description}", f"Rule #{rule_id}.")


class PendingArgs(ToolArgs):
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
    failed, failed_untrusted = outcomes.render_recently_failed(
        await outcomes.recently_failed(user_id, now), now, tz, source="pending")
    if failed:
        sections.insert(0, failed)  # what went wrong comes first: it needs the user's decision
        shown_untrusted.extend([True] if failed_untrusted else [])
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


class AcknowledgeArgs(ToolArgs):
    refs: list[str] = Field(min_length=1, description=(
        "Refs from the pending tool's \"Recently failed\" list, e.g. [\"approval:9\", \"task:14\"]"))


async def acknowledge_failure(user_id: int, args: AcknowledgeArgs) -> ToolOutput:
    n = await outcomes.acknowledge(user_id, args.refs)
    if not n:
        raise ActionFailed("Nothing matched those refs; call pending to see the current list.",
                           reason="there was nothing like that left to clear", kind=FailureKind.NOT_FOUND)
    return ToolOutput("Okay, I'll stop bringing that up.", f"Acknowledged {n} item(s).")


_CONV = frozenset({"conversation"})

TOOLS = [
    MavisTool("remember", "Store a durable fact the user wants remembered.", RememberArgs,
              RiskClass.WRITE_SELF, remember, _CONV, priority=60,
              on_taint=TaintPolicy.DOWNGRADE, tainted_fn=remember_untrusted, provenance=("fact",)),
    MavisTool("forget", "Delete memories matching a word or phrase (asks the user first).", ForgetArgs,
              RiskClass.DESTRUCTIVE, forget, _CONV,
              preview=lambda a: f"Forget everything I know matching “{a.needle}”", priority=30),
    MavisTool("wake_me", "Schedule a reminder at a specific FUTURE time, given as the user said it "
              "(local wall-clock ISO 8601, no offset).",
              WakeMeArgs, RiskClass.WRITE_SELF, wake_me, _CONV, priority=65,
              preview=_preview_wake, preview_needs_ctx=True, on_taint=TaintPolicy.APPROVE,
              provenance=("reason",)),
    MavisTool("track_loop", "Track an open loop: commitment, waiting-on, goal, concern, routine or watch.",
              TrackLoopArgs, RiskClass.WRITE_SELF, track_loop, _CONV, priority=55,
              preview=_preview_loop, on_taint=TaintPolicy.APPROVE, provenance=("title",)),
    MavisTool("pending", "What is pending: actions and tasks that recently failed, open items with how "
              "due they are, approvals waiting for the user's OK and background work. Call it for any "
              "question about what is open, due, left or whether something went through.",
              PendingArgs, RiskClass.READ, pending, _CONV, priority=70),
    MavisTool("acknowledge_failure", "Stop listing a recently failed action or task once the user has "
              "seen it and decided (they said to leave it, or will handle it themselves).",
              AcknowledgeArgs, RiskClass.WRITE_SELF, acknowledge_failure, _CONV, priority=30,
              preview=lambda a: f"Stop reminding you about: {', '.join(a.refs)}",
              on_taint=TaintPolicy.APPROVE),
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

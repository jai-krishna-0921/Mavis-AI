"""Workspace lines for the morning brief and the evening wrap (spec 2026-10-03 section 5.3).

Every title here is third-party text (scrubbed of URLs by workspace_signals.safe_title at intake), so all
brief items are untrusted and the evening lines land inside the wrap's untrusted block.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mavis.attention.rhythm import _plural
from mavis.attention.workspace import WorkspaceIntake
from mavis.attention.workspace_signals import SignalKind
from mavis.domain import timeutil
from mavis.initiative.routines import BriefItem
from mavis.store.repo import attention as repo
from mavis.store.repo import users
from mavis.tools.integrations.normalize import to_datetime

MAX_FILES = 3
MAX_LINES = 5
DUE_LOOKBACK = timedelta(hours=36)  # today's due rows, whichever poll of the last day and a half wrote them
FIRST_BRIEF_LOOKBACK = timedelta(hours=24)  # shares before any brief went out
COMMENT_DAYS = 3  # evening wrap: comments on the user's own docs still waiting (plan deviation 15)


def _who(actor: object) -> str:
    """A sharer's name for a brief line: the part before the @ (never a full address)."""
    return str(actor or "someone").split("@", 1)[0] or "someone"


class WorkspaceBrief:
    """Morning brief "Today" block: tasks due today and files shared since the last brief (untrusted).

    `last_brief_at` is stamped by `delivered`, which Routines calls only once the brief went out
    (amendment A10): building items never changes state, so a blocked or deferred brief repeats its files."""

    name = "workspace"

    def __init__(self, workspace: WorkspaceIntake) -> None:
        self.workspace = workspace

    async def items(self, user_id: int, start: datetime, end: datetime) -> list[BriefItem]:
        user = await users.get(user_id)
        now = timeutil.now()
        st = await self.workspace.state(user_id)
        since = to_datetime(st.get("last_brief_at")) or now - FIRST_BRIEF_LOOKBACK
        today = timeutil.to_local(now, user.timezone).date().isoformat()
        due = await repo.signals(user_id, now - DUE_LOOKBACK, sources=("tasks",),
                                 kinds=(SignalKind.TASK_DUE.value,))
        due_today = [r for r in due if (r.facts or {}).get("due") == today]
        shared = [r for r in await repo.signals(user_id, since, sources=("drive",),
                                                kinds=(SignalKind.FILE_SHARED.value,))
                  if r.verdict in ("brief", "notify")]
        comments = [r for r in await repo.signals(user_id, since, sources=("docs",),
                                                  kinds=(SignalKind.COMMENT.value,))
                    if r.verdict == "brief"]
        items: list[BriefItem] = []
        if due_today:
            names = "; ".join(r.summary for r in due_today[:MAX_LINES])
            items.append(BriefItem(f"Today: {_plural(len(due_today), 'task')} due: {names}", False))
        if st.get("upcoming"):
            more = _plural(int(st["upcoming"]), "more task")
            items.append(BriefItem(f"Coming up this week: {more} due.", True))
        for r in shared[:MAX_FILES]:
            facts = r.facts or {}
            if facts.get("security"):
                text = (f"Heads up: someone you haven't emailed shared a file named \"{r.summary}\". "
                        "Don't open links in it unless you expected it.")
            else:
                text = f"Shared with you: {r.summary} (from {_who(facts.get('actor'))})"
            items.append(BriefItem(text, False))
        if comments:
            count = _plural(len(comments), "new comment")
            items.append(BriefItem(f"Comments: {count} on docs you follow.", True))
        return items

    async def delivered(self, user_id: int, at: datetime) -> None:
        """The brief went out: later briefs list only files shared after `at`; the week count is spent."""
        await self.workspace.patch(user_id, last_brief_at=at.isoformat(), upcoming=0)


async def workspace_evening(user_id: int, start: datetime) -> list[str]:
    """Evening wrap lines: tasks that went overdue today and comments on the user's own docs (3 days)."""
    overdue = await repo.signals(user_id, start, sources=("tasks",), kinds=(SignalKind.TASK_OVERDUE.value,))
    recent = await repo.signals(user_id, timeutil.now() - timedelta(days=COMMENT_DAYS), sources=("docs",),
                                kinds=(SignalKind.COMMENT.value,))
    lines = [f"Overdue task: {r.summary}" for r in overdue[:MAX_LINES]]
    waiting = [r for r in recent if (r.facts or {}).get("owned_by_me") and r.feedback is None]
    lines += [f"Comment waiting on your doc: {r.summary}" for r in waiting[:MAX_FILES]]
    return lines

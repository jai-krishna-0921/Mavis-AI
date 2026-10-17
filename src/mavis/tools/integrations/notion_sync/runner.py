# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The resumable Notion sync: one NOTION_SYNC job per user runs the registered passes inside a time budget.

Passes (people, projects, meetings, todos, briefings) register themselves with `register_pass`. A pass is
`async fn(SyncCtx)`; it should check `ctx.left()` between units of work and simply return when the budget is
gone. A pass that returns with no time left is treated as unfinished: it is not stamped, and the job
queues itself again. Cursors (all under `users.state["cursors"]`, keys start with `notion_`):

- `notion_setup`: "done" once the first full run finished.
- `notion_<pass>_at`: when that pass last completed.
- `notion_problem`: {"kind", "at"} while the home cannot be built or has lost a database.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from mavis.bus import get_bus
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.events import Job, JobKind
from mavis.store.repo import notion_links, users
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.notion_api import NotionApi, NotionError
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.notion_sync import blocks
from mavis.tools.integrations.notion_sync.home import HOME_LINK, Home, ensure_home
from mavis.worker import locks

log = structlog.get_logger(__name__)

BUDGET_S = 240
CLAIM_TTL_S = 600
PROBLEM_RETRY = timedelta(hours=24)
SETUP_TEXT = "Mavis AI is still setting up. This takes a few minutes."
PROBLEM_TEXT = {
    "no_page": "Tell the user Mavis AI could not set up its Notion home yet: they need to share one page "
               "with Mavis AI in Notion (link to reconnect).",
    "gone": "Tell the user a part of their Mavis AI home in Notion was deleted; they can say rebuild to "
            "recreate it.",
}


@dataclass
class SyncCtx:
    user_id: int
    api: NotionApi
    home: Home
    account: dict
    now: datetime
    deadline: float
    force: frozenset[str]
    clock: Callable[[], float] = time.monotonic

    def left(self) -> bool:
        return self.clock() < self.deadline


Pass = Callable[[SyncCtx], Awaitable[None]]
PASSES: list[tuple[int, str, timedelta, Pass]] = []


def register_pass(name: str, cadence: timedelta, fn: Pass, *, order: int) -> None:
    """Add a pass (people 10, projects 20, meetings 30, todos 40, briefings 50). Same name replaces."""
    PASSES[:] = sorted([p for p in PASSES if p[1] != name] + [(order, name, cadence, fn)],
                       key=lambda p: p[0])


# ----- the router's Notion pieces (injectable in tests)

def _router():
    from mavis.tools.integrations import get_provider

    return get_provider()


def notion_api() -> NotionApi | None:
    """The router's own Notion executor api, so the sync shares its pacing bucket with the tools."""
    try:
        executor = _router().executors.get(NativeProvider.NOTION)
    except AttributeError:
        return None
    return getattr(executor, "api", None)


async def notion_grant(user_id: int) -> Grant | None:
    tokens = getattr(_router(), "tokens", None)
    return await tokens.grant(user_id, NativeProvider.NOTION) if tokens is not None else None


# ----- cursors

async def _cursors(user_id: int) -> dict:
    return dict((await users.get_state(user_id)).get("cursors") or {})


async def _put(user_id: int, **patch: object) -> None:
    await users.update_nested(user_id, "cursors", patch)


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return timeutil.ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _due(cursors: dict, name: str, cadence: timedelta, now: datetime) -> bool:
    at = _when(cursors.get(f"notion_{name}_at"))
    return at is None or now - at >= cadence


# ----- the job

async def enqueue_sync(user_id: int, force: Iterable[str] = (), *, again: int = 0) -> None:
    names = sorted(set(force))
    minute = timeutil.now().strftime("%Y%m%d%H%M")
    suffix = f":again{again}" if again else ""
    await get_bus().enqueue(Job(
        id=f"notion_sync:{user_id}:{','.join(names)}:{minute}{suffix}", user_id=user_id,
        kind=JobKind.NOTION_SYNC, payload={"force": names, "again": again}))


async def rebuild_home(user_id: int) -> None:
    """Forget the home and its databases (the pages stay in Notion) and build a fresh one."""
    await notion_links.delete_user_kinds(user_id, ["home", "db"])
    await _put(user_id, notion_problem=None, notion_setup=None)
    await enqueue_sync(user_id)


async def _notify(user_id: int, problem: str) -> None:
    from mavis.initiative import wiring

    user = await users.get(user_id)
    await wiring.current().executor.notify(user, NotifyIntent(
        urgency=3, intent=PROBLEM_TEXT[problem], dedupe_key=f"notion_home:{problem}"))


class NotionSync:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = timeutil.now) -> None:
        self._clock, self._now = clock, now

    async def run(self, user_id: int, force: Iterable[str] = (), *, again: int = 0) -> None:
        key = f"notion_sync:{user_id}"
        if not await locks.claim(key, CLAIM_TTL_S):
            return
        try:
            await self._run(user_id, frozenset(force), again)
        except ReauthRequired:
            log.info("notion_sync.reauth", user_id=user_id)  # the connection check owns this
        except NotionError as exc:
            log.warning("notion_sync.failed", user_id=user_id, kind=exc.kind.value, code=exc.code)
        finally:
            await locks.release(key)

    async def _run(self, user_id: int, force: frozenset[str], again: int) -> None:
        grant = await notion_grant(user_id)
        api = notion_api()
        if grant is None or grant.status != ACTIVE or api is None:
            return
        now = self._now()
        cursors = await _cursors(user_id)
        home = await ensure_home(api, user_id, grant.account)
        if isinstance(home, str):
            seen = cursors.get("notion_problem") or {}
            if seen.get("kind") != home:
                await _notify(user_id, home)
            await _put(user_id, notion_problem={"kind": home, "at": now.isoformat()})
            return
        if cursors.get("notion_problem"):
            await _put(user_id, notion_problem=None)
        setting_up = cursors.get("notion_setup") != "done"
        if setting_up:
            await self._show_callout(api, user_id, home)
        ctx = SyncCtx(user_id, api, home, grant.account, now, self._clock() + BUDGET_S, force, self._clock)
        finished = True
        for _, name, cadence, fn in list(PASSES):
            if not (setting_up or name in force or _due(cursors, name, cadence, now)):
                continue
            if not ctx.left():
                finished = False
                break
            try:
                await fn(ctx)
            except NotionError as exc:
                log.warning("notion_sync.pass_failed", user_id=user_id, name=name, kind=exc.kind.value,
                            code=exc.code)
                finished = False
                continue
            if not ctx.left():
                finished = False  # cut short: not stamped, picked up again
                break
            await _put(user_id, **{f"notion_{name}_at": self._now().isoformat()})
        if not ctx.left() and not finished:
            await enqueue_sync(user_id, force, again=again + 1)
        if setting_up and finished:
            await self._hide_callout(api, user_id, home)
            await _put(user_id, notion_setup="done")

    async def _show_callout(self, api: NotionApi, user_id: int, home: Home) -> None:
        link = await notion_links.get(user_id, *HOME_LINK)
        if link is None or link.meta.get("callout"):
            return
        ids = await api.append(user_id, home.page_id, [blocks.callout(SETUP_TEXT)],
                               position={"type": "start"})
        await notion_links.upsert(user_id, *HOME_LINK, home.page_id, meta={**link.meta, "callout": ids})

    async def _hide_callout(self, api: NotionApi, user_id: int, home: Home) -> None:
        link = await notion_links.get(user_id, *HOME_LINK)
        if link is None or not link.meta.get("callout"):
            return
        await blocks.replace_owned(api, user_id, home.page_id, list(link.meta["callout"]), [])
        await notion_links.upsert(user_id, *HOME_LINK, home.page_id, meta={**link.meta, "callout": []})


async def poll_notion(poller, user_id: int, st: dict) -> int:
    """Poll hook: queue a sync when setup is unfinished, a pass is due, or a home problem is a day old."""
    cursors = st.get("cursors") or {}
    now = timeutil.now()
    problem = cursors.get("notion_problem")
    if problem:
        at = _when(problem.get("at") if isinstance(problem, dict) else None)
        if at is None or now - at >= PROBLEM_RETRY:
            await enqueue_sync(user_id)
        return 0
    if cursors.get("notion_setup") != "done" or any(
            _due(cursors, name, cadence, now) for _, name, cadence, _ in PASSES):
        await enqueue_sync(user_id)
    return 0


async def notion_sync_job(job: Job) -> None:
    await NotionSync().run(job.user_id, job.payload.get("force") or (),
                           again=int(job.payload.get("again") or 0))

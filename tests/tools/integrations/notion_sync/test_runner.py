from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.domain.events import JobKind
from mavis.domain.policy import Capability
from mavis.store.repo import notion_links, users
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.notion_sync import runner
from mavis.tools.integrations.notion_sync.runner import NotionSync, SyncCtx, register_pass


class Notices:
    def __init__(self):
        self.sent = []

    async def notify(self, user, intent, **kw):
        self.sent.append(intent)
        return True


@pytest.fixture
def env(fake, api, monkeypatch, recording_bus, user):
    from mavis import bus as bus_mod
    from mavis.initiative import wiring

    notices = Notices()
    wiring.set_current(SimpleNamespace(executor=notices))
    bus_mod.set_bus(recording_bus)
    account = {"template_page_id": fake.add_page("Template")["id"]}

    async def grant(uid):
        return Grant(uid, NativeProvider.NOTION, ACTIVE, account)

    monkeypatch.setattr(runner, "notion_api", lambda: api)
    monkeypatch.setattr(runner, "notion_grant", grant)
    monkeypatch.setattr(runner, "PASSES", [])
    yield SimpleNamespace(notices=notices, bus=recording_bus, account=account, uid=user.id)
    bus_mod.set_bus(None)


async def cursors(uid):
    return (await users.get_state(uid)).get("cursors", {})


def recorder(log, name, cost=0.0, clock=None):
    async def fn(ctx: SyncCtx):
        log.append(name)
        if clock is not None:
            clock[0] += cost

    return fn


async def test_setup_callout_shown_then_removed(fake, env):
    seen = []

    async def fn(ctx):
        kids = await ctx.api.children(ctx.user_id, ctx.home.page_id)
        seen.append([b["type"] for b in kids if b["type"] == "callout"])

    register_pass("people", timedelta(hours=1), fn, order=10)
    await NotionSync().run(env.uid)
    assert seen == [["callout"]]
    home = (await notion_links.get(env.uid, "home", "home")).page_id
    assert not [b for b in await runner_children(fake, home) if b["type"] == "callout"]
    assert (await cursors(env.uid))["notion_setup"] == "done"
    assert "notion_people_at" in await cursors(env.uid)


async def runner_children(fake, page_id):
    return [b for b in fake.blocks[page_id] if not b["in_trash"]]


async def test_no_shared_page_notifies_once(fake, env):
    env.account["template_page_id"] = ""
    fake.pages.clear()
    await NotionSync().run(env.uid)
    await NotionSync().run(env.uid)
    assert len(env.notices.sent) == 1
    assert env.notices.sent[0].dedupe_key == "notion_home:no_page"
    assert env.notices.sent[0].urgency == 3
    assert "share one page" in env.notices.sent[0].intent
    assert (await cursors(env.uid))["notion_problem"]["kind"] == "no_page"


async def test_deleted_database_reports_gone_once_and_does_not_rebuild(fake, env):
    await NotionSync().run(env.uid)
    link = await notion_links.get(env.uid, "db", "meetings")
    fake.data_sources[link.meta["ds_id"]]["in_trash"] = True
    await NotionSync().run(env.uid)
    await NotionSync().run(env.uid)
    assert [i.dedupe_key for i in env.notices.sent] == ["notion_home:gone"]
    assert "rebuild" in env.notices.sent[0].intent
    assert len(fake.databases) == 5


async def test_rebuild_tool_recreates_after_gone(fake, env):
    from mavis.tools import assistant

    await NotionSync().run(env.uid)
    link = await notion_links.get(env.uid, "db", "meetings")
    fake.data_sources[link.meta["ds_id"]]["in_trash"] = True
    await NotionSync().run(env.uid)
    tool = next(t for t in assistant.TOOLS if t.name == "notion_rebuild_home")
    assert tool.requires is Capability.NOTION and tool.agents == frozenset({"conversation"})
    out = await tool.fn(env.uid, assistant.NoArgs())
    assert "stay where they are" in str(out)
    st = await cursors(env.uid)
    assert not st.get("notion_problem") and not st.get("notion_setup")
    [job] = [j for j in env.bus.jobs if j.kind is JobKind.NOTION_SYNC]
    assert job.user_id == env.uid
    await NotionSync().run(env.uid)
    assert len(fake.databases) == 10
    assert (await cursors(env.uid))["notion_setup"] == "done"


async def test_budget_exhaustion_reenqueues_with_same_force(fake, env):
    t = [0.0]
    log = []
    register_pass("people", timedelta(hours=1), recorder(log, "people", 500, t), order=10)
    register_pass("projects", timedelta(hours=1), recorder(log, "projects"), order=20)
    await NotionSync(clock=lambda: t[0]).run(env.uid, force=["projects", "people"])
    assert log == ["people"]
    [job] = [j for j in env.bus.jobs if j.kind is JobKind.NOTION_SYNC]
    assert job.payload["force"] == ["people", "projects"]
    assert job.id.startswith(f"notion_sync:{env.uid}:people,projects:")
    st = await cursors(env.uid)
    assert "notion_people_at" not in st and st.get("notion_setup") != "done"


async def test_due_passes_only(fake, env):
    log = []
    register_pass("people", timedelta(hours=1), recorder(log, "people"), order=10)
    register_pass("projects", timedelta(hours=6), recorder(log, "projects"), order=20)
    register_pass("todos", timedelta(minutes=2), recorder(log, "todos"), order=40)
    register_pass("meetings", timedelta(hours=1), recorder(log, "meetings"), order=30)
    await NotionSync().run(env.uid)
    assert log == ["people", "projects", "meetings", "todos"]  # setup: all, in order
    now = timeutil.now()
    iso = lambda d: (now - d).isoformat()  # noqa: E731
    await users.update_nested(env.uid, "cursors", {
        "notion_people_at": iso(timedelta(minutes=10)), "notion_projects_at": iso(timedelta(hours=7)),
        "notion_meetings_at": iso(timedelta(minutes=10)), "notion_todos_at": iso(timedelta(minutes=3))})
    log.clear()
    await NotionSync().run(env.uid)
    assert log == ["projects", "todos"]
    log.clear()
    await NotionSync().run(env.uid, force=["people"])
    assert log == ["people"]


async def test_concurrent_run_is_skipped_by_claim(fake, env):
    from mavis.worker import locks

    assert await locks.claim(f"notion_sync:{env.uid}", 600)
    log = []
    register_pass("people", timedelta(hours=1), recorder(log, "people"), order=10)
    await NotionSync().run(env.uid)
    assert log == [] and not fake.databases
    await locks.release(f"notion_sync:{env.uid}")


async def test_register_pass_sorts_and_replaces(env):
    async def a(ctx): ...
    async def b(ctx): ...
    register_pass("projects", timedelta(hours=1), a, order=20)
    register_pass("people", timedelta(hours=1), a, order=10)
    register_pass("projects", timedelta(hours=2), b, order=20)
    assert [(o, n) for o, n, _, _ in runner.PASSES] == [(10, "people"), (20, "projects")]
    assert runner.PASSES[1][3] is b


async def test_poll_enqueues_when_needed(env):
    poller = SimpleNamespace(merge_cursors=lambda uid, patch: users.update_nested(uid, "cursors", patch))
    register_pass("people", timedelta(hours=1), recorder([], "people"), order=10)
    assert await runner.poll_notion(poller, env.uid, {"cursors": {}}) == 0
    assert len(env.bus.jobs) == 1
    now = timeutil.now()
    fresh = {"notion_setup": "done", "notion_people_at": now.isoformat()}
    await runner.poll_notion(poller, env.uid, {"cursors": fresh})
    assert len(env.bus.jobs) == 1  # nothing due
    due = {**fresh, "notion_people_at": (now - timedelta(hours=2)).isoformat()}
    await runner.poll_notion(poller, env.uid, {"cursors": due})
    assert len(env.bus.jobs) == 2
    young = {**due, "notion_problem": {"kind": "gone", "at": now.isoformat()}}
    await runner.poll_notion(poller, env.uid, {"cursors": young})
    assert len(env.bus.jobs) == 2  # a recent problem is not retried
    old = {**due, "notion_problem": {"kind": "gone", "at": (now - timedelta(hours=25)).isoformat()}}
    await runner.poll_notion(poller, env.uid, {"cursors": old})
    assert len(env.bus.jobs) == 3

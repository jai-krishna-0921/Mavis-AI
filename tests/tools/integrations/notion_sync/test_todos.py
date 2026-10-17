from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.domain.errors import FailureKind
from mavis.domain.events import Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.loops.service import LoopService
from mavis.store.repo import loops as loop_repo
from mavis.store.repo import notion_links, users
from mavis.tools.integrations.native.notion_api import NotionError
from mavis.tools.integrations.notion_sync import runner
from mavis.tools.integrations.notion_sync.home import ensure_home
from mavis.tools.integrations.notion_sync.todos import KINDS, todos_pass
from tests.tools.integrations.native.notionfake import BOT_ID, OWNER_ID

T0 = datetime(2026, 10, 17, 12, 0, 30, tzinfo=UTC)


class Notices:
    def __init__(self):
        self.sent = []

    async def notify(self, user, intent, **kw):
        self.sent.append((intent, kw))
        return True


class Clock:
    def __init__(self):
        self.now = T0

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
async def env(fake, api, monkeypatch, recording_bus, user):
    from mavis import bus as bus_mod
    from mavis.initiative import wiring

    notices = Notices()
    wiring.set_current(SimpleNamespace(executor=notices))
    bus_mod.set_bus(recording_bus)
    clock = Clock()
    monkeypatch.setattr(timeutil, "_clock", lambda: clock.now)
    fake.clock = lambda: clock.now
    account = {"template_page_id": fake.add_page("Template")["id"], "owner_id": OWNER_ID,
               "bot_user_id": BOT_ID}
    home = await ensure_home(api, user.id, account)
    svc = LoopService(recording_bus)

    class Env:
        pass

    e = Env()
    e.__dict__.update(fake=fake, api=api, clock=clock, notices=notices, account=account, home=home,
                      svc=svc, uid=user.id, ds=home.ds["todos"])

    async def run(force=()):
        ctx = runner.SyncCtx(user.id, api, home, account, clock.now, 1e18, frozenset(force),
                             lambda: 0.0)
        await todos_pass(ctx)

    async def add(title="Send deck to Dana", kind=LoopKind.COMMITMENT, **kw):
        return await svc.upsert(user.id, LoopUpsert(kind=kind, title=title, trust=Trust.USER, **kw))

    def rows():
        return [p for p in fake.pages.values()
                if p["parent"].get("data_source_id") == e.ds and not p["in_trash"]]

    e.run, e.add, e.rows = run, add, rows
    yield e
    bus_mod.set_bus(None)


def text(page, name):
    prop = page["properties"][name]
    return "".join(r.get("plain_text", "") for r in prop.get(prop["type"]) or [])


def sel(page, name):
    return (page["properties"][name]["select"] or {}).get("name")


def due(page):
    return (page["properties"]["Due"]["date"] or {}).get("start")


def select(name):
    return {"select": {"name": name}}


def edit_title(title):
    return {"Name": {"title": [{"type": "text", "text": {"content": title}}]}}


async def link_of(env, loop):
    return await notion_links.get(env.uid, "todo", str(loop.id))


async def test_new_loop_appears_as_row_with_kind_status_due_person(env):
    await notion_links.upsert(env.uid, "person", "dana", "page-dana", meta={"name": "Dana"})
    due_at = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    loop = await env.add(due_at=due_at, entities=["dana"], source="evt-1")
    await env.run()
    [row] = env.rows()
    assert text(row, "Name") == "Send deck to Dana"
    assert text(row, "Mavis key") == f"loop:{loop.id}"
    assert sel(row, "Kind") == "I owe"
    assert sel(row, "Status") == "Open"
    tz = (await users.get(env.uid)).timezone
    assert due(row) == timeutil.to_local(due_at, tz).date().isoformat()
    assert [r["id"] for r in row["properties"]["People"]["relation"]] == ["page-dana"]
    assert text(row, "Source") == "evt-1"
    link = await link_of(env, loop)
    assert link.page_id == row["id"] and link.pushed_hash and link.pushed_at and link.edited_at


async def test_waiting_and_goal_kind_labels(env):
    await env.add("Hear back from Sam", kind=LoopKind.WAITING_ON)
    await env.add("Run a marathon", kind=LoopKind.GOAL)
    await env.run()
    assert sorted(sel(r, "Kind") for r in env.rows()) == ["Goal", "Waiting on them"]


async def test_own_echo_ignored(env):
    loop = await env.add()
    await env.run()
    before = (await env.svc.get(loop.id)).version
    env.clock.advance(minutes=5)
    n = len(env.fake.requests)
    await env.run()
    after = await env.svc.get(loop.id)
    assert after.version == before and after.status == LoopStatus.OPEN
    assert [r for r in env.fake.requests[n:] if r.method in ("POST", "PATCH")
            and not r.url.path.endswith("/query")] == []
    assert env.notices.sent == []


async def test_owner_ticks_done_closes_loop_done(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], {"Status": select("Done")})
    env.clock.advance(minutes=1)
    await env.run()
    assert (await env.svc.get(loop.id)).status == LoopStatus.DONE
    assert sel(env.rows()[0], "Status") == "Done"


async def test_owner_sets_dropped_drops(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], {"Status": select("Dropped")})
    await env.run()
    assert (await env.svc.get(loop.id)).status == LoopStatus.DROPPED


async def test_owner_reopens_closed_loop(env):
    loop = await env.add()
    await env.svc.close(loop.id, LoopStatus.DONE)
    env.clock.advance(minutes=1)
    await env.run()
    assert sel(env.rows()[0], "Status") == "Done"
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], {"Status": select("Open")})
    await env.run()
    assert (await env.svc.get(loop.id)).status == LoopStatus.OPEN


async def test_owner_adds_row_creates_commitment_without_merging_similar_title(env):
    existing = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    page = env.fake._page_obj({"type": "data_source_id", "data_source_id": env.ds}, {})
    env.fake.blocks[page["id"]] = []
    env.fake.user_edit(page["id"], edit_title("Send deck to Dana"))
    await env.run()
    mine = [lp for lp in await loop_repo.list_open(env.uid)]
    assert len(mine) == 2
    new = next(lp for lp in mine if lp.id != existing.id)
    assert new.kind == LoopKind.COMMITMENT and new.origin == LoopOrigin.NOTION
    assert new.trust == Trust.USER
    assert text(env.fake.pages[page["id"]], "Mavis key") == f"loop:{new.id}"
    assert (await link_of(env, new)).page_id == page["id"]
    n = len(env.rows())
    await env.run()
    assert len(env.rows()) == n and len(await loop_repo.list_open(env.uid)) == 2


async def test_owner_changes_kind_and_due(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"],
                       {"Kind": select("Goal"), "Due": {"date": {"start": "2026-11-02"}}})
    await env.run()
    got = await env.svc.get(loop.id)
    tz = (await users.get(env.uid)).timezone
    assert got.kind == LoopKind.GOAL
    assert timeutil.to_local(got.due_at, tz).date().isoformat() == "2026-11-02"


async def test_owner_edits_title_of_mavis_row_renames_loop(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], edit_title("Send the board deck to Dana"))
    await env.run()
    assert (await env.svc.get(loop.id)).title == "Send the board deck to Dana"
    assert text(env.rows()[0], "Name") == "Send the board deck to Dana"


@pytest.mark.parametrize("editor", ["user-someone-else", "bot-other"])
async def test_foreign_edit_mentions_once_and_applies_nothing(env, editor):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], edit_title("Hacked"), editor_id=editor)
    await env.run()
    env.clock.advance(minutes=5)
    await env.run()
    assert (await env.svc.get(loop.id)).title == "Send deck to Dana"
    assert len(env.notices.sent) == 1
    intent, kw = env.notices.sent[0]
    assert intent.urgency == 2 and kw["untrusted"] is True
    assert intent.dedupe_key.startswith(f"notion_foreign:{env.rows()[0]['id']}:")
    assert "Hacked" in intent.intent


async def test_conflict_newer_loop_change_wins_and_overwrites_row(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], edit_title("Owner title"))
    env.clock.advance(minutes=5)
    await env.svc.upsert(env.uid, LoopUpsert(id=loop.id, title="Mavis title", trust=Trust.USER))
    env.clock.advance(minutes=1)
    await env.run()
    assert (await env.svc.get(loop.id)).title == "Mavis title"
    assert text(env.rows()[0], "Name") == "Mavis title"


async def test_minute_truncated_edits_not_missed_by_overlap(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)  # 12:05:30
    pid = env.rows()[0]["id"]
    env.fake.user_edit(pid, edit_title("First"))
    env.clock.advance(seconds=10)
    await env.run()
    assert (await env.svc.get(loop.id)).title == "First"
    env.clock.advance(seconds=10)  # still 12:05
    env.fake.user_edit(pid, {"Kind": select("Goal")})
    env.clock.advance(seconds=10)
    await env.run()
    got = await env.svc.get(loop.id)
    assert got.title == "First" and got.kind == LoopKind.GOAL


async def test_closed_loop_archived_after_30_days(env):
    loop = await env.add()
    await env.svc.close(loop.id, LoopStatus.DONE)
    await env.run()
    pid = env.rows()[0]["id"]
    env.clock.advance(days=29)
    await env.run()
    assert not env.fake.pages[pid]["in_trash"]
    env.clock.advance(days=2)
    await env.run()
    assert env.fake.pages[pid]["in_trash"]
    assert await link_of(env, loop) is None
    assert (await env.svc.get(loop.id)).status == LoopStatus.DONE


async def test_concern_routine_watch_never_pushed(env):
    for kind in (LoopKind.CONCERN, LoopKind.ROUTINE, LoopKind.WATCH):
        await env.add(f"a {kind.value.lower()}", kind=kind)
    await env.run()
    assert env.rows() == []
    assert KINDS == {LoopKind.COMMITMENT, LoopKind.WAITING_ON, LoopKind.GOAL}


async def test_trashed_row_drops_loop(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    pid = env.rows()[0]["id"]
    env.fake.user_edit(pid, {})
    env.fake.pages[pid]["in_trash"] = True
    await env.run()
    assert (await env.svc.get(loop.id)).status == LoopStatus.DROPPED
    env.clock.advance(minutes=5)
    await env.run()
    assert env.rows() == []  # the dropped loop is not rebuilt as a new row


async def test_failed_writeback_retry_creates_one_loop(env, monkeypatch):
    env.clock.advance(minutes=5)
    page = env.fake._page_obj({"type": "data_source_id", "data_source_id": env.ds}, {})
    env.fake.blocks[page["id"]] = []
    env.fake.user_edit(page["id"], edit_title("Call the plumber"))
    real, calls = env.api.update_page, []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise NotionError(FailureKind.UNKNOWN, "internal_server_error")
        return await real(*a, **kw)

    monkeypatch.setattr(env.api, "update_page", flaky)
    with pytest.raises(NotionError):
        await env.run()
    assert len(await loop_repo.list_open(env.uid)) == 1
    env.clock.advance(minutes=5)
    await env.run()
    [loop] = await loop_repo.list_open(env.uid)
    assert text(env.fake.pages[page["id"]], "Mavis key") == f"loop:{loop.id}"
    env.clock.advance(minutes=5)
    await env.run()
    assert len(await loop_repo.list_open(env.uid)) == 1


async def test_same_minute_conflict_goes_to_the_owner(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    env.fake.user_edit(env.rows()[0]["id"], edit_title("Owner title"))
    env.clock.advance(seconds=10)  # Mavis edits later in the same minute: the stamp cannot tell
    await env.svc.upsert(env.uid, LoopUpsert(id=loop.id, title="Mavis title", trust=Trust.USER))
    env.clock.advance(seconds=10)
    await env.run()
    assert (await env.svc.get(loop.id)).title == "Owner title"


async def test_push_continues_past_the_changed_cap(env, monkeypatch):
    monkeypatch.setattr(loop_repo, "CHANGED_LIMIT", 2)
    for word in ("plumber", "invoice", "passport", "dentist", "garden"):
        await env.add(f"Call about {word}")
        env.clock.advance(seconds=1)
    await env.run()
    assert len(env.rows()) == 5
    env.clock.advance(minutes=5)
    await env.add("Renew insurance")
    await env.run()
    assert len(env.rows()) == 6


async def test_trash_by_someone_else_mentions_once_and_keeps_loop(env):
    loop = await env.add()
    await env.run()
    env.clock.advance(minutes=5)
    pid = env.rows()[0]["id"]
    env.fake.user_edit(pid, {}, editor_id="user-someone-else")
    env.fake.pages[pid]["in_trash"] = True
    await env.run()
    env.clock.advance(minutes=5)
    await env.run()
    assert (await env.svc.get(loop.id)).status == LoopStatus.OPEN
    assert len(env.notices.sent) == 1 and env.notices.sent[0][1]["untrusted"] is True

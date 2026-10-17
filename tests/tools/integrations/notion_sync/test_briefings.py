from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.domain.errors import FailureKind
from mavis.domain.messages import Role
from mavis.store.repo import messages, notion_links
from mavis.tools.integrations.native.notion_api import NotionError
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.notion_sync import briefings, runner
from mavis.tools.integrations.notion_sync.briefings import NotionBriefSource, briefings_pass
from mavis.tools.integrations.notion_sync.home import ensure_home
from tests.tools.integrations.native.notionfake import BOT_ID, OWNER_ID


@pytest.fixture
async def env(fake, api, user):
    account = {"template_page_id": fake.add_page("Template")["id"], "owner_id": OWNER_ID,
               "bot_user_id": BOT_ID}
    home = await ensure_home(api, user.id, account)

    def ctx(now=None):
        return runner.SyncCtx(user.id, api, home, account, now or timeutil.now(), 1e18, frozenset(),
                              lambda: 0.0)

    def pages():
        return [p for p in fake.pages.values()
                if p["parent"].get("data_source_id") == home.ds["briefings"] and not p["in_trash"]]

    return SimpleNamespace(uid=user.id, ctx=ctx, pages=pages, api=api, fake=fake)


async def brief(uid, day, text):
    await messages.log(uid, Role.ASSISTANT, text, proactive=True, event_id=f"proactive:morning:{day}")


async def test_briefing_saved_once_per_day(env):
    await brief(env.uid, "2026-10-17", "Good morning.\n\nYou have 2 meetings.\nInbox is quiet.")
    await briefings_pass(env.ctx())
    await briefings_pass(env.ctx())  # a second run changes nothing
    [page] = env.pages()
    title = page["properties"]["Name"]["title"]
    assert "".join(r.get("plain_text", "") for r in title) == "Briefing 2026-10-17"
    assert page["properties"]["Date"]["date"]["start"] == "2026-10-17"
    body = []
    for b in await env.api.children(env.uid, page["id"]):
        rich = b["paragraph"]["rich_text"][0]
        body.append(rich.get("plain_text") or rich["text"]["content"])
    assert body == ["Good morning.", "You have 2 meetings.", "Inbox is quiet."]
    assert (await notion_links.get(env.uid, "briefing", "2026-10-17")).page_id == page["id"]
    await brief(env.uid, "2026-10-18", "Next day")
    await briefings_pass(env.ctx())
    assert len(env.pages()) == 2


async def test_delivered_forces_briefings_only_when_connected(db, monkeypatch):
    calls, state = [], SimpleNamespace(grant=None)

    async def grant(uid):
        return state.grant

    async def enqueue(uid, force=()):
        calls.append((uid, set(force)))

    monkeypatch.setattr(briefings, "notion_grant", grant)
    monkeypatch.setattr(briefings, "enqueue_sync", enqueue)
    src = NotionBriefSource()
    assert src.name == "notion" and await src.items(1, None, None) == []
    await src.delivered(1, timeutil.now())
    assert calls == []
    state.grant = Grant(1, "notion", "FAILED", {})
    await src.delivered(1, timeutil.now())
    assert calls == []
    state.grant = Grant(1, "notion", ACTIVE, {})
    await src.delivered(1, timeutil.now())
    assert calls == [(1, {"briefings"})]


async def test_dated_briefing_older_than_the_window_is_skipped(env):
    await brief(env.uid, "2026-10-10", "Old news")
    await briefings_pass(env.ctx(timeutil.now() + timedelta(days=3)))
    assert env.pages() == []
    await briefings_pass(env.ctx())
    assert len(env.pages()) == 1


async def test_message_without_a_date_is_skipped(env):
    await brief(env.uid, "no-date-here", "x")
    await briefings_pass(env.ctx())
    assert env.pages() == []


async def test_failed_body_append_retries_onto_the_same_page(env, monkeypatch):
    await brief(env.uid, "2026-10-17", "Hello\nWorld")
    real, calls = env.api.append, []

    async def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise NotionError(FailureKind.UNAVAILABLE, "boom")
        return await real(*a, **kw)

    monkeypatch.setattr(env.api, "append", flaky)
    with pytest.raises(NotionError):
        await briefings_pass(env.ctx())
    assert len(env.pages()) == 1
    await briefings_pass(env.ctx())
    [page] = env.pages()
    assert len(await env.api.children(env.uid, page["id"])) == 2
    await briefings_pass(env.ctx())
    assert len(env.pages()) == 1 and len(calls) == 2


async def test_a_150_line_briefing_is_appended_whole(env):
    await brief(env.uid, "2026-10-17", "\n".join(f"line {i}" for i in range(150)))
    await briefings_pass(env.ctx())
    [page] = env.pages()
    assert len(await env.api.children(env.uid, page["id"])) == 150


async def test_i1_lost_briefing_create_answer_does_not_duplicate_the_page(env):
    await brief(env.uid, "2026-10-17", "Good morning.")
    env.fake.script_lost("POST", r"/pages")
    with pytest.raises(NotionError):
        await briefings_pass(env.ctx())
    assert len(env.pages()) == 1
    await briefings_pass(env.ctx())
    assert len(env.pages()) == 1

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.domain.messages import Role
from mavis.store.repo import messages, notion_links
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

    def ctx():
        return runner.SyncCtx(user.id, api, home, account, timeutil.now(), 1e18, frozenset(), lambda: 0.0)

    def pages():
        return [p for p in fake.pages.values()
                if p["parent"].get("data_source_id") == home.ds["briefings"] and not p["in_trash"]]

    return SimpleNamespace(uid=user.id, ctx=ctx, pages=pages, api=api)


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


async def test_old_briefings_outside_the_window_are_skipped(env):
    await brief(env.uid, "no-date-here", "x")
    await briefings_pass(env.ctx())
    assert env.pages() == []
    assert timedelta(days=2) == briefings.WINDOW

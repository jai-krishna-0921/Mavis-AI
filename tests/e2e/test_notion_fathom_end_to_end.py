# ruff: noqa: E501
"""Notion and Fathom end to end, no network: connect both through the real OAuth callback -> the sync builds the
Mavis AI home and fills People, Todos and Meetings -> the owner ticks a todo, a teammate's edit is only mentioned
-> a signed Fathom webhook becomes notes, loops and an untrusted memory record -> the ask is sent only for the
unrecorded meeting and the reply lands under "From you" -> the morning brief is saved as a page -> a model write to
a Notion page waits for the approval card -> forgetting a person trashes the page -> disconnecting Fathom removes
the webhook and its notes, disconnecting and reconnecting Notion adopts the same home -> account deletion empties
both tables. One MockTransport answers for Notion, Fathom, Google and the token endpoints. Each numbered block is
its own coroutine."""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select

from mavis.access import deletion
from mavis.agents import orchestrator_graph as og
from mavis.agents.conversation import attach_queued_approvals
from mavis.agents.react import react_loop
from mavis.api.routes import fathom as fathom_route
from mavis.api.routes import oauth as oauth_route
from mavis.bus import get_bus
from mavis.domain import timeutil
from mavis.domain.events import EventType, JobKind, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.memory import Entity
from mavis.domain.messages import Role
from mavis.domain.policy import Capability as C
from mavis.domain.tasks import ApprovalStatus
from mavis.initiative import routines
from mavis.initiative import wiring as initiative
from mavis.initiative.routines import Routines
from mavis.loops.service import LoopService
from mavis.memory.personal import PersonAgg
from mavis.store import db as dbm
from mavis.store.models import MeetingNote, NotionLink
from mavis.store.repo import approvals, meeting_notes, messages, notion_links, tasks, users
from mavis.store.repo import loops as loop_repo
from mavis.store.repo import personal as personal_repo
from mavis.tools import assistant
from mavis.tools.integrations import fathom_ingest, get_connection_cache, get_provider, wiring
from mavis.tools.integrations import tools as tools_mod
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.fathom import FathomExecutor
from mavis.tools.integrations.native.google import GoogleExecutor
from mavis.tools.integrations.native.notion import NotionExecutor
from mavis.tools.integrations.native.oauth import NativeOAuth
from mavis.tools.integrations.native.router import NativeRouter
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL, NativeTokenStore
from mavis.tools.integrations.notion_sync import meetings, people, runner
from mavis.tools.integrations.notion_sync.briefings import NotionBriefSource
from mavis.tools.integrations.notion_sync.runner import NotionSync
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.registry import ToolRegistry
from tests.tools.integrations.fakes import FakeProvider
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures
from tests.tools.integrations.native.fathomfake import FakeFathom, fathom_meeting
from tests.tools.integrations.native.gfake import FakeGoogle
from tests.tools.integrations.native.notionfake import BOT_ID, OWNER_ID, FakeNotion
from tests.tools.integrations.native.test_notion_oauth import query
from tests.tools.integrations.native.test_router import G, grant
from tests.tools.integrations.native.test_router_merge import ALL_GOOGLE

N, F = NativeProvider.NOTION, NativeProvider.FATHOM
T0 = datetime(2026, 10, 17, 12, 0, 30, tzinfo=UTC)
ME = "me@acme.test"
DANA = "dana@x.test"
NOTION_TOKEN = "https://api.notion.com/v1/oauth/token"
FATHOM_TOKEN = "https://api.fathom.ai/external/v1/oauth2/token"
ALLOWED_HOSTS = {"api.notion.com", "api.fathom.ai", "fathom.video", "oauth2.googleapis.com",
                 "openidconnect.googleapis.com", "www.googleapis.com", "gmail.googleapis.com"}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class World:
    """Everything the vendors answer, behind one transport; the hosts seen are recorded."""

    def __init__(self, vendor, notion: FakeNotion, fathom: FakeFathom) -> None:
        self.vendor, self.hosts = vendor, set()
        self.notion, self.fathom, self.google = notion, fathom, FakeGoogle()
        self.events: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.hosts.add(host)
        if host == "api.notion.com":
            return self.vendor(request) if request.url.path.endswith("/oauth/token") else await self.notion(request)
        if host == "api.fathom.ai":
            return self.vendor(request) if request.url.path.endswith("/oauth2/token") else await self.fathom(request)
        if host in ("oauth2.googleapis.com", "openidconnect.googleapis.com"):
            return self.vendor(request)
        return await self.google(request)


class Notices:
    """The initiative executor: records what Mavis tells the user, and files a morning brief like the real one."""

    def __init__(self) -> None:
        self.sent: list = []

    async def notify(self, user, intent, **kw):
        self.sent.append(intent)
        if intent.dedupe_key.startswith("morning:"):
            await messages.log(user.id, Role.ASSISTANT, intent.intent, proactive=True,
                               event_id=f"proactive:{intent.dedupe_key}")
        return True

    def keys(self, prefix: str) -> list[str]:
        return [i.dedupe_key for i in self.sent if i.dedupe_key.startswith(prefix)]


class FakeGraph:
    async def entities(self, user_id):
        return [Entity(name="Dana Reyes", label="Person", aliases=[])]

    async def dump(self, user_id):
        return [{"subject": "Dana Reyes", "relation": "leads", "object": "Finance",
                 "statement": "Dana Reyes leads Finance", "source_ref": ""}]

    async def neighborhood(self, user_id, names, hops=2, limit=25):
        return ["leads Finance"] if names and names[0] == "Dana Reyes" else []


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def cal(eid: str, title: str, start: datetime, end: datetime, who: tuple[str, str]) -> dict:
    return {"id": eid, "summary": title, "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)},
            "status": "confirmed",
            "attendees": [{"email": ME, "self": True, "responseStatus": "accepted"},
                          {"email": who[1], "displayName": who[0]}]}


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(timeutil, "_clock", lambda: c.now)
    return c


@pytest.fixture
def world(vendor, native_env, clock):
    notion, fathom = FakeNotion(), FakeFathom()
    notion.clock = lambda: clock.now
    w = World(vendor, notion, fathom)
    template = notion.add_page("Template")
    vendor.routes[NOTION_TOKEN] = lambda r: httpx.Response(200, json={
        "access_token": "ntn_access", "refresh_token": "ntn_refresh", "bot_id": BOT_ID, "workspace_id": "ws-1",
        "workspace_name": "Orbit", "duplicated_template_id": template["id"],
        "owner": {"type": "user", "user": {"id": OWNER_ID, "person": {"email": "owner@example.test"}}}})
    vendor.routes[FATHOM_TOKEN] = lambda r: httpx.Response(200, json={
        "access_token": "fa_access", "refresh_token": "fa_refresh", "expires_in": 3600})
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})
    w.google.on("GET", r"/calendar/v3/calendars/[^/]+/events$", lambda r: httpx.Response(200, json={"items": w.events}))
    return w


@pytest.fixture
async def router(world, db, user):
    client = httpx.AsyncClient(transport=httpx.MockTransport(world))

    async def no_sleep(_s: float) -> None:
        return None

    tokens = NativeTokenStore(client)
    r = NativeRouter(FakeProvider(), tokens, NativeOAuth(tokens, client),
                     [GoogleExecutor(tokens, client, sleep=no_sleep), NotionExecutor(tokens, client, sleep=no_sleep),
                      FathomExecutor(tokens, client, sleep=no_sleep)], client)
    yield r
    await client.aclose()


def web(router, bus):
    app = FastAPI()
    app.include_router(oauth_route.router)
    app.include_router(fathom_route.router)
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_provider] = lambda: router
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def connect(router, bus, user_id, provider: NativeProvider) -> httpx.Response:
    link = await router.oauth.authorize_url(user_id, provider, None)
    async with web(router, bus) as c:
        return await c.get(f"/oauth/{provider.value}/callback", params={"code": "code-1", "state": query(link)["state"]})


def production_handlers() -> dict:
    return {JobKind.CONNECTION_CHECK: wiring._connection_check_job, JobKind.FIRST_SYNC: wiring._first_sync_job,
            JobKind.NOTION_SYNC: wiring._notion_sync_job, JobKind.FATHOM_RECORDING: wiring._fathom_recording_job}


async def drain(bus) -> None:
    """Run what the code queued through the handlers the worker registers (wiring.py), until nothing is left:
    connection checks, the CONNECTION_CHANGED event, first syncs, Notion sync and Fathom recording jobs.
    Each job kind that ran is recorded in `bus.ran`."""
    handlers, flow = production_handlers(), wiring.get_connect_flow()
    if not hasattr(bus, "ran"):
        bus.ran = []
    for _ in range(25):
        events = [e for e in bus.events if e.type is EventType.CONNECTION_CHANGED]
        bus.events[:] = [e for e in bus.events if e not in events]
        due = [j for j in bus.jobs if j.kind in handlers]
        bus.jobs[:] = [j for j in bus.jobs if j not in due]
        if not events and not due:
            return
        for event in events:
            await flow.on_connection_changed(event)
        for job in due:
            bus.ran.append(job.kind)
            await handlers[job.kind](job)
    raise AssertionError("jobs keep queueing each other")


def text_of(page: dict, name: str) -> str:
    prop = page["properties"][name]
    return "".join(r.get("plain_text") or r["text"]["content"] for r in prop.get(prop["type"]) or [])


def rows_of(world: World, home, db: str, kind: str | None = None) -> list[dict]:
    return [p for p in world.notion.pages.values()
            if p["parent"].get("data_source_id") == home.ds[db] and not p["in_trash"]]


async def body_of(world: World, uid: int, page_id: str) -> list[tuple[str, str]]:
    out = []
    for b in await world.notion.executor()[0].api.children(uid, page_id):
        rich = b[b["type"]].get("rich_text") or []
        out.append((b["type"], "".join(r.get("plain_text") or r["text"]["content"] for r in rich)))
    return out


async def current_home(uid: int):
    from mavis.tools.integrations.notion_sync.home import ensure_home

    grant_ = await runner.notion_grant(uid)
    return await ensure_home(runner.notion_api(), uid, grant_.account)


# ---------------------------------------------------------------------------- 1 and 2 connect


async def block_connect_notion(router, world, user, bus):
    r = await connect(router, bus, user.id, N)
    assert r.status_code == 200 and "is now linked" in r.text
    g = await router.tokens.grant(user.id, N)
    assert g.status == "ACTIVE" and g.account["owner_id"] == OWNER_ID and g.account["bot_user_id"] == BOT_ID
    assert g.account["template_page_id"] in world.notion.pages
    assert [j for j in bus.jobs if j.kind is JobKind.CONNECTION_CHECK]  # the callback queues the activation check
    return g


async def block_connect_fathom(router, world, user, bus):
    r = await connect(router, bus, user.id, F)
    assert r.status_code == 200 and "is now linked" in r.text
    assert (await router.tokens.grant(user.id, F)).status == "ACTIVE"
    [hook] = world.fathom.webhooks.values()
    assert hook["triggered_for"] == ["my_recordings"] and hook["include_action_items"] and hook["include_summary"]
    route = hook["url"].rsplit("/", 1)[1]
    assert hook["url"] == f"https://mavis.test/webhooks/fathom/{route}"
    return hook, route


# ---------------------------------------------------------------------------- 3 seed


async def block_seed(router, world, user, bus, monkeypatch):
    await grant(router.tokens, G, ALL_GOOGLE, user=user.id)
    from mavis.attention.connector_ingest import remember_identity

    await remember_identity(user.id, emails=(ME,))
    world.events[:] = [
        cal("e-budget", "Budget review", T0 + timedelta(days=1), T0 + timedelta(days=1, minutes=30), ("Dana Reyes", DANA)),
        cal("e-kick", "Kickoff sync", T0 - timedelta(minutes=110), T0 - timedelta(minutes=80), ("Dana Reyes", DANA)),
        cal("e-vendor", "Vendor call", T0 - timedelta(minutes=190), T0 - timedelta(minutes=160), ("Priya Nair", "priya@vend.test")),
    ]
    old_start = T0 - timedelta(days=2)
    world.fathom.add_meeting(fathom_meeting(400, "Intro call", iso(old_start), iso(old_start + timedelta(minutes=30)),
                                            invitees=("Raj Patel <raj@x.test>",), summary="Met Raj, no follow ups."))
    svc = LoopService(bus)
    mine = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Send Dana the Q4 deck", entities=["Dana Reyes"],
                                                trust=Trust.USER, origin=LoopOrigin.CONVERSATION))
    theirs = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Dana to return the vendor contract",
                                                  entities=["Dana Reyes"], trust=Trust.USER, origin=LoopOrigin.CONVERSATION))
    aggs = [PersonAgg(key=DANA, name="Dana Reyes", email=DANA, counts=Counter(sent=4, received=5, meeting=2), score=9.0,
                      last=T0 - timedelta(days=1))]

    async def aggregate(uid, now):
        return list(aggs)

    async def summary(messages_, tier, temp, **kw):
        return "Finance lead who likes short decks."

    monkeypatch.setattr(people, "aggregate_people", aggregate)
    monkeypatch.setattr(people.llm, "complete", summary)
    monkeypatch.setattr(people, "get_memory", lambda: SimpleNamespace(graph=FakeGraph()))
    monkeypatch.setattr(meetings, "get_memory", lambda: SimpleNamespace(graph=FakeGraph()))
    return mine, theirs


# ---------------------------------------------------------------------------- 4 first sync


async def block_first_sync(world, user, bus, mine, theirs):
    uid = user.id
    await drain(bus)
    ran = Counter(bus.ran)  # connect -> check -> CONNECTION_CHANGED -> FIRST_SYNC -> sync and ingest jobs
    assert ran[JobKind.CONNECTION_CHECK] == 2 and ran[JobKind.FIRST_SYNC] == 2
    assert ran[JobKind.NOTION_SYNC] >= 1 and ran[JobKind.FATHOM_RECORDING] == 1
    intro = await meeting_notes.get(uid, "fathom:400")  # the older recording was read by the Fathom first sync
    assert intro.recording_id == "400" and intro.summary == "Met Raj, no follow ups."
    assert len(world.notion.databases) == 5
    home = await current_home(uid)
    [person] = rows_of(world, home, "people")
    assert text_of(person, "Name") == "Dana Reyes" and (person["properties"]["Email"]["email"]) == DANA
    todo_names = sorted(text_of(p, "Name") for p in rows_of(world, home, "todos"))
    assert todo_names == ["Dana to return the vendor contract", "Send Dana the Q4 deck"]
    pages = {text_of(p, "Name"): p for p in rows_of(world, home, "meetings")}
    assert set(pages) == {"Budget review", "Kickoff sync", "Vendor call", "Intro call"}
    body = await body_of(world, uid, pages["Budget review"]["id"])
    assert ("heading_2", "Prep") in body
    assert any(t == "With: Dana Reyes" for _, t in body) and any(t == "Open: Send Dana the Q4 deck" for _, t in body)
    assert [r["id"] for r in pages["Budget review"]["properties"]["People"]["relation"]] == [
        (await notion_links.get(uid, "person", DANA)).page_id]
    page_id = (await notion_links.get(uid, *runner.HOME_LINK)).page_id
    shown = [r for r in world.notion.requests if r.method == "PATCH" and r.url.path.endswith(f"/blocks/{page_id}/children")
             and b'"callout"' in r.content]
    removed = [r for r in world.notion.requests if r.method == "DELETE" and "/blocks/" in r.url.path]
    assert len(shown) == 1 and removed  # the setup callout was shown during setup, then deleted
    assert not [b for b in world.notion.blocks[page_id] if b["type"] == "callout"]  # and none is left
    assert (await users.get_state(uid))["cursors"]["notion_setup"] == "done"
    return home


# ---------------------------------------------------------------------------- 5 two-way todos


async def block_todo_edits(world, user, clock, notices, home, mine, theirs):
    uid = user.id
    clock.advance(minutes=10)
    world.notion.clock = lambda: clock.now
    by_name = {text_of(p, "Name"): p for p in rows_of(world, home, "todos")}
    world.notion.user_edit(by_name["Send Dana the Q4 deck"]["id"], {"Status": {"select": {"name": "Done"}}})
    world.notion.user_edit(by_name["Dana to return the vendor contract"]["id"], {"Name": {"title": [
        {"type": "text", "text": {"content": "Dana to return the contract by Friday"}}]}}, editor_id="user-teammate")
    await NotionSync().run(uid, force=["todos"])
    await NotionSync().run(uid, force=["todos"])
    assert (await loop_repo.get(mine.id)).status is LoopStatus.DONE  # the owner's tick closes the loop
    other = await loop_repo.get(theirs.id)
    assert other.status is LoopStatus.OPEN and other.title == "Dana to return the vendor contract"  # a teammate's edit changes nothing
    assert len(notices.keys("notion_foreign:")) == 1  # mentioned once, however often the pass runs


# ---------------------------------------------------------------------------- 6 fathom webhook


async def block_fathom_webhook(router, world, user, bus, hook, route):
    uid = user.id
    start, end = T0 - timedelta(minutes=110), T0 - timedelta(minutes=80)
    meeting = fathom_meeting(501, "Kickoff sync", iso(start), iso(end),
                             invitees=("Dana Reyes <dana@x.test>", f"Me <{ME}>"),
                             action_items=("Send Dana the revised budget", "Dana to confirm the venue booking"),
                             summary="Agreed the budget and the venue.")
    meeting["action_items"][0]["assignee"] = {"name": "Me", "email": ME, "team": None}
    meeting["action_items"][1]["assignee"] = {"name": "Dana Reyes", "email": DANA, "team": None}
    world.fathom.add_meeting(meeting, transcript=[("Dana Reyes", "Let us lock the budget this week.")])
    import json

    body = json.dumps(meeting).encode()
    ts = str(int(time.time()))
    headers = {"webhook-id": "msg_1", "webhook-timestamp": ts,
               "webhook-signature": FakeFathom.sign(hook["secret"], "msg_1", ts, body)}
    async with web(router, bus) as c:
        assert (await c.post(f"/webhooks/fathom/{route}", content=body,
                             headers={**headers, "webhook-signature": "v1,AAAA"})).status_code == 401
        assert (await c.post("/webhooks/fathom/nope", content=body, headers=headers)).status_code == 404
        assert (await c.post(f"/webhooks/fathom/{route}", content=body, headers=headers)).status_code == 204
        assert (await c.post(f"/webhooks/fathom/{route}", content=body, headers=headers)).status_code == 204  # a replay
    assert len([j for j in bus.jobs if j.kind is JobKind.FATHOM_RECORDING]) == 1
    await drain(bus)
    row = await meeting_notes.get(uid, "e-kick")  # matched to the calendar event, not a fathom: key
    assert row.recording_id == "501" and row.summary == "Agreed the budget and the venue."
    live = {lp.title: lp for lp in await loop_repo.list_live(uid)}
    commitment, waiting = live["Send Dana the revised budget"], live["Dana to confirm the venue booking"]
    assert commitment.kind is LoopKind.COMMITMENT and commitment.entities == []
    assert waiting.kind is LoopKind.WAITING_ON and waiting.entities == ["Dana Reyes"]
    assert commitment.trust is Trust.UNTRUSTED and waiting.origin is LoopOrigin.MEETING
    [learn] = [j for j in bus.jobs if j.kind is JobKind.LEARN and j.payload.get("source_ref") == "fathom:501"]
    assert learn.payload["trust"] == "untrusted" and not learn.payload.get("authored") and "Agreed the budget" in learn.payload["text"]
    home = await current_home(uid)
    kick = next(p for p in rows_of(world, home, "meetings") if text_of(p, "Name") == "Kickoff sync")
    body_ = await body_of(world, uid, kick["id"])
    assert ("heading_2", "From Fathom") in body_
    assert any("Agreed the budget and the venue." in t for _, t in body_)
    assert kick["properties"]["Recording"]["url"] == "https://fathom.video/share/501"
    return home


# ---------------------------------------------------------------------------- 7 the ask


async def block_ask_and_note(world, user, bus, clock, notices, home):
    uid = user.id
    clock.advance(minutes=5)
    world.notion.clock = lambda: clock.now
    await NotionSync().run(uid, force=["meetings"])
    await NotionSync().run(uid, force=["meetings"])
    assert notices.keys("meeting_ask:") == ["meeting_ask:e-vendor"]  # once, and never for the recorded meeting
    assert "Vendor call" in next(i.intent for i in notices.sent if i.dedupe_key == "meeting_ask:e-vendor")
    out = await assistant.meeting_note(uid, assistant.MeetingNoteArgs(text="Agreed to pilot with two vendors."))
    assert out == "Saved to your notes for Vendor call."
    await drain(bus)
    page = next(p for p in rows_of(world, home, "meetings") if text_of(p, "Name") == "Vendor call")
    body = await body_of(world, uid, page["id"])
    texts = [t for _, t in body]
    assert ("heading_2", "From you") in body and "Agreed to pilot with two vendors." in texts
    assert texts.index("From you") < texts.index("Agreed to pilot with two vendors.")


# ---------------------------------------------------------------------------- 8 morning brief


async def block_morning_brief(world, user, bus, notices, home):
    uid = user.id
    routines.register_brief_source(NotionBriefSource())
    try:
        await Routines(LoopService(bus), None, notices)._send_morning(await users.get(uid))
    finally:
        routines.unregister_brief_source("notion")
    day = timeutil.to_local(timeutil.now(), user.timezone).date().isoformat()
    assert [j for j in bus.jobs if j.kind is JobKind.NOTION_SYNC and j.payload["force"] == ["briefings"]]
    await drain(bus)
    [page] = rows_of(world, home, "briefings")
    assert text_of(page, "Name") == f"Briefing {day}"
    assert any("Warm good-morning" in t for _, t in await body_of(world, uid, page["id"]))


# ---------------------------------------------------------------------------- 9 approval card


def calls(*specs):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": c} for n, a, c in specs])


async def _noop_enqueue(*a, **kw):
    return None


async def block_append_needs_approval(router, world, user, monkeypatch, fake_llm):
    uid = user.id
    monkeypatch.setattr("mavis.agents.conversation.enqueue_run", _noop_enqueue)
    monkeypatch.setattr(tools_mod, "_deps", lambda p, c: (router, ConnectionCache(router, ttl_s=0)))
    reg = ToolRegistry()
    register_integration_tools(reg)
    trip = world.notion.add_page("Trip notes")
    fake_llm.push_ai(calls(("notion_append", {"page_id": trip["id"], "content": "Pack the adapters."}, "n1")))
    fake_llm.push_text("I drafted that for you to approve.")
    res = await react_loop(reg.for_agent("conversation", uid), [HumanMessage("add a line to my trip notes")], max_steps=3)
    assert str([m for m in res.messages if getattr(m, "tool_call_id", None)][0].content).startswith("QUEUED_FOR_APPROVAL")
    [card] = await approvals.unattached_for_user(uid)
    task_id = await attach_queued_approvals(uid, "add to trip notes", False)
    graph = og.build_orchestrator().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f"task:{task_id}"}}
    first = await graph.ainvoke(og.initial_state(await tasks.get(task_id)), cfg)
    assert first["__interrupt__"][0].value["approval_id"] == card.id
    assert not [b for b in world.notion.blocks[trip["id"]]]  # nothing is written before the user taps OK
    assert await approvals.claim(card.id, {ApprovalStatus.PENDING}, ApprovalStatus.RESOLVING)
    go = Command(resume={"approval_id": card.id, "decision": "ok", "instructions": ""})
    await graph.ainvoke(go, cfg)
    await graph.ainvoke(go, cfg)  # a redelivered tap does not write again
    written = [b for b in world.notion.blocks[trip["id"]]]
    assert len(written) == 1 and written[0]["paragraph"]["rich_text"][0]["text"]["content"] == "Pack the adapters."
    assert (await approvals.get(card.id)).status == ApprovalStatus.EXECUTED


# ---------------------------------------------------------------------------- 10 forget a person


async def block_forget_person(world, user, home):
    uid = user.id
    link = await notion_links.get(uid, "person", DANA)
    await personal_repo.suppress(uid, DANA, "Dana Reyes")
    await NotionSync().run(uid, force=["people"])
    assert world.notion.pages[link.page_id]["in_trash"] is True
    assert await notion_links.get(uid, "person", DANA) is None


# ---------------------------------------------------------------------------- 11 disconnects


async def block_disconnect_and_reconnect(router, world, user, bus, monkeypatch):
    uid = user.id
    forgotten = []

    async def forget(user_id, prefix):
        forgotten.append(prefix)
        return 1

    monkeypatch.setattr(wiring, "forget_learned_source", forget)
    wiring.get_connect_flow.cache_clear()
    flow = wiring.get_connect_flow()  # the production construction: activator, first sync claim, hooks
    assert await flow.disconnect(uid, C.FATHOM, forget=True) is True
    assert world.fathom.webhooks == {}  # the webhook at Fathom is deleted
    assert await router.tokens.grant(uid, F) is None
    assert await meeting_notes.get(uid, "e-kick") is None  # Fathom's notes are cleared
    kept = await meeting_notes.get(uid, "e-vendor")
    assert "Agreed to pilot with two vendors." in kept.user_notes  # what the user said stays
    assert "fathom:" in forgotten

    home_before = await notion_links.get(uid, *runner.HOME_LINK)
    assert await flow.disconnect(uid, C.NOTION, forget=False) is True
    assert await router.tokens.grant(uid, N) is None
    assert (await users.get_state(uid)).get("cursors", {}).get("notion_setup") is None
    assert (await notion_links.get(uid, *runner.HOME_LINK)).page_id == home_before.page_id  # the pages stay linked

    bus.jobs.clear()
    r = await connect(router, bus, uid, N)
    assert r.status_code == 200
    bus.ran.clear()
    await drain(bus)  # the reconnect goes through the check and the FIRST_SYNC job again
    assert JobKind.FIRST_SYNC in bus.ran and JobKind.NOTION_SYNC in bus.ran
    assert (await notion_links.get(uid, *runner.HOME_LINK)).page_id == home_before.page_id  # adopted
    assert len(world.notion.databases) == 5  # and there is no second home
    homes = [p for p in world.notion.pages.values() if text_of_title(p) == "Mavis AI" and not p["in_trash"]]
    assert len(homes) == 1
    assert (await users.get_state(uid))["cursors"]["notion_setup"] == "done"


def text_of_title(page: dict) -> str:
    title = (page.get("properties") or {}).get("title") or {}
    return "".join(r.get("plain_text") or r["text"]["content"] for r in title.get("title") or [])


# ---------------------------------------------------------------------------- 12 deletion


async def count(model, uid: int) -> int:
    async with dbm.Session() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(model.user_id == uid)) or 0)


async def block_deletion(router, user):
    uid = user.id
    assert await count(NotionLink, uid) > 0 and await count(MeetingNote, uid) > 0
    await deletion.run_steps(uid)  # the production deletion path, every step
    assert await router.tokens.grant(uid, N) is None
    assert await count(NotionLink, uid) == 0 and await count(MeetingNote, uid) == 0
    assert (await users.get(uid)).status == "deleted"


def install(router, monkeypatch) -> None:
    """The router is the process provider, and the production wiring objects are built fresh around it."""
    shim = lambda: router  # noqa: E731
    shim.cache_clear = lambda: None
    for target in ("mavis.tools.integrations.get_provider", "mavis.tools.integrations.wiring.get_provider"):
        monkeypatch.setattr(target, shim)
    monkeypatch.setattr(meetings, "get_provider", shim)
    monkeypatch.setattr(fathom_ingest, "get_provider", shim)
    for getter in (*wiring.WIRING_GETTERS, get_connection_cache):
        getter.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_wiring():
    yield
    for getter in (*wiring.WIRING_GETTERS, get_connection_cache):
        getter.cache_clear()


async def test_notion_fathom_journey(router, world, vendor, user, rec_bus, clock, monkeypatch, fake_llm, memory,
                                     memory_checkpointer, channel):
    install(router, monkeypatch)
    notices, previous = Notices(), initiative._current
    initiative.set_current(SimpleNamespace(executor=notices))
    try:
        bus = rec_bus
        await block_connect_notion(router, world, user, bus)
        hook, route = await block_connect_fathom(router, world, user, bus)
        mine, theirs = await block_seed(router, world, user, bus, monkeypatch)
        home = await block_first_sync(world, user, bus, mine, theirs)
        await block_todo_edits(world, user, clock, notices, home, mine, theirs)
        home = await block_fathom_webhook(router, world, user, bus, hook, route)
        await block_ask_and_note(world, user, bus, clock, notices, home)
        await block_morning_brief(world, user, bus, notices, home)
        await block_append_needs_approval(router, world, user, monkeypatch, fake_llm)
        await block_forget_person(world, user, home)
        await block_disconnect_and_reconnect(router, world, user, bus, monkeypatch)
        await block_deletion(router, user)
    finally:
        initiative.set_current(previous)
    assert world.hosts <= ALLOWED_HOSTS, world.hosts - ALLOWED_HOSTS
    world.notion.assert_all_routed()
    world.fathom.assert_all_routed()


def test_every_notion_and_fathom_action_has_a_handler():
    from mavis.tools.integrations.actions import ACTIONS

    for cap, ex in ((C.NOTION, NotionExecutor(None, httpx.AsyncClient())), (C.FATHOM, FathomExecutor(None, httpx.AsyncClient()))):
        specs = {n for n in ACTIONS if ACTIONS[n].capability is cap}
        handled = set(ex._handlers)
        assert specs and specs <= handled and handled <= set(ACTIONS)


async def test_account_deletion_with_a_live_fathom_webhook(router, world, user, rec_bus, clock, monkeypatch, memory,
                                                           memory_checkpointer, channel):
    """Deleting the account while Fathom is still connected removes the webhook at Fathom, both Mavis tables
    and every grant, including the webhook companion."""
    install(router, monkeypatch)
    uid, bus = user.id, rec_bus
    await connect(router, bus, uid, N)
    await connect(router, bus, uid, F)
    [hook] = world.fathom.webhooks.values()
    await notion_links.upsert(uid, "home", "home", "page-home")
    await meeting_notes.upsert(uid, "fathom:9", title="T", recording_id="9", summary="s")
    assert await router.tokens.grant(uid, NativeProvider.FATHOM_HOOK) is not None
    await deletion.run_steps(uid)
    deletes = [r for r in world.fathom.requests if r.method == "DELETE"]
    assert [r.url.path.rsplit("/", 1)[1] for r in deletes] == [hook["id"]]  # the DELETE reached Fathom
    assert world.fathom.webhooks == {}
    for provider in (N, F, NativeProvider.FATHOM_HOOK):
        assert await router.tokens.grant(uid, provider) is None
    assert await count(NotionLink, uid) == 0 and await count(MeetingNote, uid) == 0
    world.fathom.assert_all_routed()

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mavis.domain import timeutil
from mavis.domain.events import Trust
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.loops.service import LoopService
from mavis.store.repo import meeting_notes, notion_links, users
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.notion_sync import meetings, runner
from mavis.tools.integrations.notion_sync.home import ensure_home
from mavis.tools.integrations.notion_sync.meetings import ask_due, meetings_pass, qualifies
from tests.tools.integrations.native.notionfake import BOT_ID, OWNER_ID

T0 = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def cal(eid="evt-1", title="Budget review", start=60, end=90, attendees=(("Dana", "dana@x.test"),), **kw):
    """A raw Google event; start and end are minutes from T0."""
    def at(m):
        return (T0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")

    people = [{"email": "me@acme.test", "self": True, "responseStatus": "accepted"}]
    people += [{"email": e, "displayName": n} for n, e in attendees]
    return {"id": eid, "summary": title, "start": {"dateTime": at(start)}, "end": {"dateTime": at(end)},
            "status": "confirmed", "attendees": people, **kw}


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


class FakeGraph:
    def __init__(self):
        self.facts: dict[str, list[str]] = {}
        self.asked: list = []

    async def neighborhood(self, user_id, names, hops=2, limit=25):
        self.asked.append((names, hops, limit))
        return self.facts.get(names[0], [])[:limit]


@pytest.fixture
async def env(fake, api, monkeypatch, recording_bus, user):
    from mavis import bus as bus_mod
    from mavis.initiative import wiring

    notices, clock, graph = Notices(), Clock(), FakeGraph()
    wiring.set_current(SimpleNamespace(executor=notices))
    bus_mod.set_bus(recording_bus)
    monkeypatch.setattr(timeutil, "_clock", lambda: clock.now)
    fake.clock = lambda: clock.now
    account = {"template_page_id": fake.add_page("Template")["id"], "owner_id": OWNER_ID,
               "bot_user_id": BOT_ID}
    home = await ensure_home(api, user.id, account)
    state = SimpleNamespace(events=[], fathom=False)

    async def events(uid, start, end):
        return list(state.events)

    async def fathom_grant(uid):
        return Grant(uid, "fathom", ACTIVE, {}) if state.fathom else None

    monkeypatch.setattr(meetings, "calendar_events", events)
    monkeypatch.setattr(meetings, "fathom_grant", fathom_grant)
    monkeypatch.setattr(meetings, "get_memory", lambda: SimpleNamespace(graph=graph))

    class Env:
        pass

    e = Env()
    e.__dict__.update(fake=fake, api=api, clock=clock, notices=notices, graph=graph, state=state, uid=user.id,
                      home=home, svc=LoopService(recording_bus))

    def ctx():
        return runner.SyncCtx(user.id, api, home, account, clock.now, 1e18, frozenset(), lambda: 0.0)

    async def run():
        await meetings_pass(ctx())

    async def ask():
        await ask_due(ctx(), list(state.events))

    def pages():
        return [p for p in fake.pages.values()
                if p["parent"].get("data_source_id") == home.ds["meetings"] and not p["in_trash"]]

    async def body(page):
        out = []
        for b in await api.children(user.id, page["id"]):
            rich = b[b["type"]].get("rich_text") or []
            out.append((b["type"], "".join(r.get("plain_text") or r["text"]["content"] for r in rich)))
        return out

    async def setup_done():
        await runner._put(user.id, notion_setup="done")

    e.run, e.ask, e.pages, e.body, e.setup_done = run, ask, pages, body, setup_done
    e.bus = recording_bus
    yield e
    bus_mod.set_bus(None)


def text(page, name):
    prop = page["properties"][name]
    return "".join(r.get("plain_text", "") for r in prop.get(prop["type"]) or [])


def test_qualifies_rules():
    assert qualifies(cal())
    assert not qualifies({**cal(), "start": {"date": "2026-10-17"}, "end": {"date": "2026-10-18"}})
    assert not qualifies({**cal(), "status": "cancelled"})
    declined = cal()
    declined["attendees"][0]["responseStatus"] = "declined"
    assert not qualifies(declined)
    assert not qualifies(cal(attendees=()))  # only the user
    assert qualifies({**cal(), "attendees": [{"email": "a@x.test"}]})  # no self marker, someone invited


async def test_meeting_page_has_prep_and_people_relation(env):
    await notion_links.upsert(env.uid, "person", "dana", "page-dana",
                              meta={"name": "Dana Cole", "email": "dana@x.test"})
    env.graph.facts["Dana Cole"] = ["prefers short decks", "runs finance"]
    await env.svc.upsert(env.uid, LoopUpsert(kind=LoopKind.COMMITMENT, title="Send Dana the numbers",
                                             entities=["Dana Cole"], trust=Trust.USER))
    await env.svc.upsert(env.uid, LoopUpsert(kind=LoopKind.COMMITMENT, title="Buy milk", trust=Trust.USER))
    env.state.events = [cal(attendees=(("Dana", "Dana@X.test"), ("Raj", "raj@x.test")))]
    await env.run()
    [page] = env.pages()
    assert text(page, "Name") == "Budget review"
    assert text(page, "Mavis key") == "meeting:evt-1"
    assert [r["id"] for r in page["properties"]["People"]["relation"]] == ["page-dana"]
    when = page["properties"]["When"]["date"]
    assert when["start"].startswith("2026-10-17T13:00") and when["end"].startswith("2026-10-17T13:30")
    body = await env.body(page)
    assert body[0] == ("heading_2", "Prep")
    texts = [t for _, t in body]
    assert "With: Dana Cole, Raj" in texts
    assert "Dana Cole: prefers short decks" in texts and "Dana Cole: runs finance" in texts
    assert "Open: Send Dana the numbers" in texts and "Open: Buy milk" not in texts
    assert env.graph.asked == [(["Dana Cole"], 1, 3)]
    assert ("heading_2", "Your notes") in body and ("divider", "") in body
    link = await notion_links.get(env.uid, "meeting", "evt-1")
    assert link.page_id == page["id"] and link.meta["blocks"] and link.pushed_hash


async def test_unchanged_meeting_makes_no_writes(env):
    env.state.events = [cal()]
    await env.run()
    n = len(env.fake.requests)
    await env.run()
    writes = [r for r in env.fake.requests[n:]
              if r.method in ("POST", "PATCH", "DELETE") and not r.url.path.endswith("/query")]
    assert writes == []


async def test_fathom_notes_and_user_notes_side_by_side(env):
    env.state.events = [cal(start=-120, end=-90)]
    await meeting_notes.upsert(
        env.uid, "evt-1", title="Budget review", starts_at=T0 - timedelta(minutes=120),
        ends_at=T0 - timedelta(minutes=90), recording_id="42", fathom_url="https://fathom.test/r/42",
        summary="We agreed the budget.", action_items=[{"description": "Send the deck",
                                                        "assignee_name": "Dana"}],
        user_notes="Felt rushed.")
    await env.run()
    [page] = env.pages()
    assert page["properties"]["Recording"]["url"] == "https://fathom.test/r/42"
    body = await env.body(page)
    kinds = [t for k, t in body if k.startswith("heading")]
    assert kinds == ["Prep", "From Fathom", "Action items", "From you", "Your notes"]
    texts = [t for _, t in body]
    assert "We agreed the budget." in texts and "Send the deck (Dana)" in texts and "Felt rushed." in texts


async def test_rerender_keeps_your_notes(env):
    env.state.events = [cal()]
    await env.run()
    [page] = env.pages()
    env.fake._insert(page["id"], [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "my own scribble"}}]}}], {"type": "end"})
    await meeting_notes.append_user_notes(env.uid, "evt-1", "Went well.")
    await env.run()
    body = await env.body(page)
    assert ("paragraph", "my own scribble") in body and ("paragraph", "Went well.") in body
    assert [t for k, t in body if k.startswith("heading")] == ["Prep", "From you", "Your notes"]
    assert body.index(("paragraph", "Went well.")) < body.index(("heading_2", "Your notes"))
    assert len(env.pages()) == 1


async def test_future_meeting_prep_refreshes_but_past_meeting_prep_is_frozen(env):
    await notion_links.upsert(env.uid, "person", "dana", "page-dana",
                              meta={"name": "Dana Cole", "email": "dana@x.test"})
    env.graph.facts["Dana Cole"] = ["old fact"]
    env.state.events = [cal(start=30, end=60)]
    await env.run()
    [page] = env.pages()
    env.graph.facts["Dana Cole"] = ["new fact"]
    env.clock.advance(minutes=10)  # still before the start
    await env.run()
    assert "Dana Cole: new fact" in [t for _, t in await env.body(page)]
    env.clock.advance(minutes=30)  # started
    env.graph.facts["Dana Cole"] = ["later fact"]
    await meeting_notes.upsert(env.uid, "evt-1", summary="Notes from the call.")
    await env.run()
    texts = [t for _, t in await env.body(page)]
    assert "Dana Cole: new fact" in texts and "Dana Cole: later fact" not in texts
    assert "Notes from the call." in texts


async def test_fathom_only_meeting_gets_page(env):
    await notion_links.upsert(env.uid, "person", "raj", "page-raj",
                              meta={"name": "Raj", "email": "raj@x.test"})
    await meeting_notes.upsert(
        env.uid, "fathom:42", title="Ad hoc call", starts_at=T0 - timedelta(hours=3),
        ends_at=T0 - timedelta(hours=2), attendees=[{"name": "Raj", "email": "raj@x.test"}],
        recording_id="42", fathom_url="https://fathom.test/r/42", summary="Quick sync.")
    await meeting_notes.upsert(env.uid, "evt-old", title="Calendar row", starts_at=T0 - timedelta(hours=3),
                               ends_at=T0 - timedelta(hours=2))
    env.state.events = []
    await env.run()
    [page] = env.pages()  # the calendar-keyed row without a calendar event gets no page
    assert text(page, "Mavis key") == "meeting:fathom:42"
    assert [r["id"] for r in page["properties"]["People"]["relation"]] == ["page-raj"]
    assert "Quick sync." in [t for _, t in await env.body(page)]


async def test_non_qualifying_events_get_no_page(env):
    env.state.events = [{**cal(), "status": "cancelled"}, cal("solo", attendees=())]
    await env.run()
    assert env.pages() == []


# ----- the ask

async def test_ask_after_15_min_without_fathom(env):
    await env.setup_done()
    env.state.events = [cal(start=-60, end=-20, attendees=(("Dana", "d@x.test"), ("Raj", "r@x.test"),
                                                            ("Mo", "m@x.test")))]
    await env.ask()
    [(intent, kw)] = env.notices.sent
    assert intent.urgency == 2 and kw == {"untrusted": True}
    assert intent.dedupe_key == "meeting_ask:evt-1"
    assert "Budget review" in intent.intent and "Dana" in intent.intent and "Raj" in intent.intent
    assert "Mo" not in intent.intent and "skip" in intent.intent
    row = await meeting_notes.get(env.uid, "evt-1")
    assert row.asked_at == T0
    state = await users.get_state(env.uid)
    assert state["meeting_ask"] == {"event_key": "evt-1", "asked_at": T0.isoformat()}


async def test_ask_waits_for_15_min_and_for_notion_setup(env):
    env.state.events = [cal(start=-40, end=-10)]
    await env.ask()
    assert env.notices.sent == []  # setup not done
    await env.setup_done()
    await env.ask()
    assert env.notices.sent == []  # only 10 minutes since the end


async def test_ask_waits_90_min_with_fathom_and_skips_if_recording_arrives(env):
    await env.setup_done()
    env.state.fathom = True
    env.state.events = [cal(start=-120, end=-60)]
    await env.ask()
    assert env.notices.sent == []
    env.clock.advance(minutes=40)  # 100 minutes after the end, but the recording is in
    await meeting_notes.upsert(env.uid, "evt-1", recording_id="42")
    await env.ask()
    assert env.notices.sent == []
    env.state.events.append(cal("evt-2", "Other call", start=-190, end=-100))
    await env.ask()
    assert [i.dedupe_key for i, _ in env.notices.sent] == ["meeting_ask:evt-2"]


async def test_never_asks_twice_or_for_old_meetings_or_when_notes_exist(env):
    await env.setup_done()
    env.state.events = [cal("fresh", start=-60, end=-30), cal("old", start=-500, end=-420),
                        cal("noted", start=-60, end=-30), cal("future", start=60, end=90)]
    await meeting_notes.append_user_notes(env.uid, "noted", "already said")
    await env.ask()
    await env.ask()
    env.clock.advance(minutes=30)
    await env.ask()
    assert [i.dedupe_key for i, _ in env.notices.sent] == ["meeting_ask:fresh"]


async def test_at_most_two_asks_per_pass(env):
    await env.setup_done()
    env.state.events = [cal(f"e{i}", f"Call {i}", start=-100 + i * 5, end=-60 + i * 5) for i in range(4)]
    await env.ask()
    assert len(env.notices.sent) == 2
    await env.ask()
    assert len(env.notices.sent) == 4


# ----- final review fix wave

async def _forget(monkeypatch, keys):
    async def suppressed(uid):
        return set(keys)

    monkeypatch.setattr(meetings.personal_repo, "suppressed_keys", suppressed)


async def test_i5_forgotten_person_is_taken_out_of_a_frozen_prep(env, monkeypatch):
    await notion_links.upsert(env.uid, "person", "arun", "page-arun",
                              meta={"name": "Arun Rao", "email": "arun@x.test"})
    env.graph.facts["Arun Rao"] = ["is allergic to nuts"]
    env.state.events = [cal(start=-120, end=-90, attendees=(("Arun", "arun@x.test"), ("Raj", "raj@x.test")))]
    await env.run()
    [page] = env.pages()
    assert "Arun Rao: is allergic to nuts" in [t for _, t in await env.body(page)]
    await _forget(monkeypatch, {"arun@x.test"})
    await notion_links.delete(env.uid, "person", "arun")  # what the People pass does on a forget
    await env.run()
    texts = [t for _, t in await env.body(page)]
    assert not [t for t in texts if "Arun" in t] and "With: Raj" in texts
    link = await notion_links.get(env.uid, "meeting", "evt-1")
    assert "Arun" not in str(link.meta) and "allergic" not in str(link.meta)


async def test_i5_old_meeting_pages_outside_the_window_are_scrubbed_too(env, monkeypatch):
    await notion_links.upsert(env.uid, "person", "arun", "page-arun",
                              meta={"name": "Arun Rao", "email": "arun@x.test"})
    env.graph.facts["Arun Rao"] = ["is allergic to nuts"]
    env.state.events = [cal(start=-120, end=-90, attendees=(("Arun", "arun@x.test"),))]
    await env.run()
    [page] = env.pages()
    env.clock.advance(days=20)
    env.state.events = []  # long out of the sync window: the pass never rewrites this page
    await _forget(monkeypatch, {"arun@x.test"})
    await env.run()
    assert not [t for _, t in await env.body(page) if "Arun" in t or "allergic" in t]
    assert "allergic" not in str((await notion_links.get(env.uid, "meeting", "evt-1")).meta)


async def test_i7_meeting_your_notes_are_learned(env, monkeypatch):
    from mavis.domain.events import JobKind
    from mavis.tools.integrations.notion_sync import notes

    monkeypatch.setattr(notes, "get_bus", lambda: env.bus)
    env.state.events = [cal(start=-120, end=-90)]
    await env.run()
    [page] = env.pages()
    env.fake.clock = lambda: T0 + timedelta(hours=2)
    [block] = env.fake._insert(page["id"], [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "Dana wants the numbers by Friday"}}]}}], {"type": "end"})
    block["last_edited_by"] = {"object": "user", "id": OWNER_ID}
    env.fake.pages[page["id"]]["last_edited_time"] = env.fake._now()
    await env.run()
    [job] = [j for j in env.bus.jobs if j.kind is JobKind.LEARN]
    assert job.payload["trust"] == "user" and job.payload["source_ref"].startswith("notion:")
    assert "numbers by Friday" in job.payload["text"]


async def test_t11_a_named_meeting_that_matches_nothing_asks_which(env):
    from mavis.tools import assistant

    await meeting_notes.upsert(env.uid, "evt-asked", title="Standup", starts_at=T0 - timedelta(hours=2),
                               ends_at=T0 - timedelta(hours=1))
    await users.update_state(env.uid, {"meeting_ask": {"event_key": "evt-asked",
                                                       "asked_at": T0.isoformat()}})
    assert await assistant._find_meeting(env.uid, "quarterly planning offsite") is None
    found = await assistant._find_meeting(env.uid, "")
    assert found is not None and found[0] == "evt-asked"


async def test_n1_facts_of_a_person_without_a_page_link_leave_the_prep(env):
    await notion_links.upsert(env.uid, "person", "arun", "page-arun",
                              meta={"name": "Arun Rao", "email": "arun@x.test"})
    env.graph.facts["Arun Rao"] = ["is allergic to nuts"]
    env.state.events = [cal(start=-120, end=-90, attendees=(("Arun", "arun@x.test"),))]
    await env.run()
    [page] = env.pages()
    await notion_links.delete(env.uid, "person", "arun")  # retired by the People pass, no suppression key
    await env.run()
    assert not [t for _, t in await env.body(page) if "allergic" in t]
    assert "allergic" not in str((await notion_links.get(env.uid, "meeting", "evt-1")).meta)

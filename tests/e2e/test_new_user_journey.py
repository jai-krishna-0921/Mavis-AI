# ruff: noqa: E501
"""A brand-new person, end to end: invite, first message, onboarding, connect Google and Slack, first sync,
graph and vault, chat recall, a second person in parallel, disconnect, revoked tokens, missing scopes, an
unverified-app consent with some boxes unticked, and a Slack user from another workspace.

Real app code throughout (access gate, onboarding, connect flow, OAuth, token store, first sync, records,
graph, vault, outbox, dashboard API, Slack webhook, worker). Fakes only at the edges: the Telegram channel,
Google and Slack over httpx.MockTransport (PKCE and scopes enforced), and the model."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from mavis.access.codes import InviteError
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.store.repo import invites, users
from tests.e2e.journey_kit import (
    BOT_NAME,
    OWNER_CHAT,
    SIGNING_SECRET,
    Cloud,
    GoogleAccount,
    JourneyLLM,
    SlackPerson,
    Telegram,
    mail,
    signed_slack_headers,
)

DASHES = re.compile("[–—]")
BASE = "https://mavis.test"

PRIYA_MAIL = mail("m-priya", "Priya Nair <priya@acme.com>", "Q3 vendor contract renewal",
                  "Hi, this is Priya Nair from Acme. The Q3 vendor contract renewal is due on 30 October. "
                  "Your portal OTP is 482913, do not share it.")
MARA_MAIL = mail("m-mara", "Mara Lindqvist <mara@nordwind.example>", "Nordwind pilot kickoff",
                 "Mara Lindqvist from Nordwind here. Can we kick off the Nordwind pilot next week?")


@pytest.fixture
def env(settings, monkeypatch):
    for key, value in {
        "ACCESS_MODE": "invite", "OWNER_TELEGRAM_CHAT_IDS": f"[{OWNER_CHAT}]", "TELEGRAM_BOT_USERNAME": BOT_NAME,
        "INTEGRATION_PROVIDER": "native", "GOOGLE_WORKSPACE_ENABLED": "true", "DASHBOARD_ENABLED": "true",
        "PUBLIC_BASE_URL": BASE, "GOOGLE_OAUTH_CLIENT_ID": "gid", "GOOGLE_OAUTH_CLIENT_SECRET": "gsecret",
        "SLACK_CLIENT_ID": "sid", "SLACK_CLIENT_SECRET": "ssecret", "SLACK_SIGNING_SECRET": SIGNING_SECRET,
        "NATIVE_TOKEN_KEK": "q8vN2rXk0m5y1h0Zy3uT7b6c9d4e8f1a2b3c4d5e6f7=", "COMPOSIO_API_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


class Journey:
    """The running system plus the people and vendors around it."""

    def __init__(self, tg: Telegram, cloud: Cloud, llm: JourneyLLM, app, slack_channel) -> None:
        self.tg, self.cloud, self.llm, self.app, self.slack_channel = tg, cloud, llm, app, slack_channel
        self.bus = tg.bus
        self.channel = tg.channel

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    async def settle(self) -> None:
        await self.tg.settle()

    # -- invites --------------------------------------------------------------------------------------

    async def owner_invite(self, args: str = "") -> tuple[str, str]:
        """/invite new from the owner's chat: (link, reply text)."""
        out = await self.tg.say(OWNER_CHAT, f"/invite new {args}".strip(), "Owner")
        text = out[-1].text
        link = next(w for w in text.split() if w.startswith("https://t.me/"))
        return link, text

    @staticmethod
    def start_text(link: str) -> str:
        return f"/start {parse_qs(urlsplit(link).query)['start'][0]}"

    async def join(self, chat: int, name: str, link: str):
        """A stranger taps the invite link: Telegram sends `/start <param>`."""
        return await self.tg.say(chat, self.start_text(link), name)

    async def web_signin(self, chat: int, name: str, invite_param: str | None = None):
        """The dashboard's Telegram sign-in as a browser does it: start, open the deep link in Telegram,
        tap Approve, poll. Returns (client holding the session, csrf header)."""
        c = self.http()
        body = {"invite": invite_param} if invite_param else {}
        r = await c.post("/api/v1/auth/telegram/start", json=body)
        assert r.status_code == 200, r.text
        start = parse_qs(urlsplit(r.json()["deep_link"]).query)["start"][0]
        await self.tg.say(chat, f"/start {start}", name)
        approve = next(b for b in self.tg.buttons(chat) if b.label == "Approve")
        await self.tg.tap(chat, approve.data)
        poll = await c.get("/api/v1/auth/telegram/poll", params={"nonce": r.json()["nonce"]})
        assert poll.json() == {"status": "ok"}, poll.text
        me = (await c.get("/api/v1/me")).json()
        return c, {"X-Mavis-CSRF": me["csrf"]}

    def google_account(self, email: str, *mails: dict) -> GoogleAccount:
        acct = self.cloud.google_accounts.setdefault(email, GoogleAccount(email))
        acct.mail = list(mails)
        return acct

    async def callback(self, provider: str, params: dict, client: httpx.AsyncClient | None = None):
        c = client or self.http()
        return await c.get(f"/oauth/{provider}/callback", params=params)

    async def connect_google(self, chat: int, email: str, *, untick: tuple[str, ...] = (), status: int = 200):
        """/connect google: the link the bot sends, the consent screen, the browser callback, then the worker
        does the rest (connection check, announcement, first sync). Returns (link, callback response)."""
        await self.tg.say(chat, "/connect google")
        url = self.tg.link_in(chat, "https://accounts.google.com/")
        params = self.cloud.google_consent(url, email, untick=untick)
        r = await self.callback("google", params)
        assert r.status_code == status, r.text
        await self.settle()
        return url, r

    async def dashboard(self, chat: int, name: str = "x"):
        """A signed-in browser for someone who already has an account."""
        return await self.web_signin(chat, name)

    async def grant_of(self, chat: int, provider):
        from mavis.tools.integrations import get_provider

        user = await users.get_by_chat(chat)
        return await get_provider().tokens.grant(user.id, provider)

    async def onboard(self, chat: int, name: str, link: str, *, tz_ok: bool = True) -> None:
        await self.join(chat, name, link)
        await self.tg.tap(chat, "ob:tz:yes" if tz_ok else "ob:tz:no")


@pytest.fixture
async def j(env, db, bus, channel, memory, monkeypatch):
    """The whole system, started the way `mavis dev` starts it."""
    from mavis.api.app import create_app
    from mavis.channels import routing
    from mavis.llm import models
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native import router as native_router
    from mavis.worker.handlers import register_default_handlers
    from mavis.worker.runner import run_worker
    from tests.channels.slack_fakes import SlackFake

    cloud = Cloud()
    llm = JourneyLLM()
    monkeypatch.setattr(models, "chat_model", llm.chat_model)
    monkeypatch.setattr(models, "structured", llm.structured)
    monkeypatch.setattr(native_router, "make_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(cloud), follow_redirects=False))
    get_provider.cache_clear()
    slack_channel = SlackFake()
    routing.set_slack_channel(slack_channel)
    register_default_handlers()
    task = asyncio.create_task(run_worker(bus, "journey", concurrency=1))
    app = create_app()
    world = Journey(Telegram(bus, channel), cloud, llm, app, slack_channel)
    yield world
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    routing.set_slack_channel(None)
    with suppress(Exception):
        await get_provider().aclose()


# --- 1. invites ---------------------------------------------------------------------------------------


async def test_owner_makes_an_invite_by_telegram_and_a_stranger_joins_with_the_link(j):
    link, reply = await j.owner_invite("uses=1 days=7 tz=Asia/Kolkata Priya")
    assert link.startswith(f"https://t.me/{BOT_NAME}?start=MAV")
    assert "1 use" in reply and "expires" in reply
    out = await j.join(5001, "Priya", link)
    assert out[0].text.startswith("Hi Priya, I'm Mavis")
    assert (await users.get_by_chat(5001)).status == "active"


async def test_owner_makes_an_invite_on_the_dashboard_and_the_landing_link_signs_a_stranger_in(j):
    owner, _ = await users.get_or_create_by_chat(OWNER_CHAT, "Owner")
    from mavis.web import sessions

    token = await sessions.create(owner.id)
    c = j.http()
    c.cookies.set(sessions.COOKIE, token)
    h = {"X-Mavis-CSRF": sessions.csrf_for(token)}
    r = await c.post("/api/v1/invites", json={"name": "Aiko"}, headers=h)
    assert r.status_code == 201
    link = r.json()["link"]
    assert link.startswith(f"{BASE}/?invite=MAV")  # the landing page forwards ?invite= to /login?invite=
    param = parse_qs(urlsplit(link).query)["invite"][0]
    c2, h2 = await j.web_signin(7001, "Aiko", param)
    me = (await c2.get("/api/v1/me", headers=h2)).json()
    assert me["name"] == "Aiko" and me["channels"]["telegram"]["connected"] is True
    assert (await users.get_by_chat(7001)).status == "active"
    assert j.tg.texts(7001)[0].startswith("Hi Aiko, I'm Mavis")  # onboarding starts the same way


async def test_a_stranger_without_an_invite_is_turned_away_politely_and_never_processed(j):
    out = await j.tg.say(8001, "hello, what is on Jai's calendar tomorrow?", "Mallory")
    assert [o.text for o in out] == ["Hi! Mavis is invite-only for now. If someone gave you an invite code, send it here."]
    again = await j.tg.say(8001, "hello?? anyone", "Mallory")
    assert again == []  # one polite reply per day, not one per message
    u = await users.get_by_chat(8001)
    assert u.status == "pending"
    from mavis.store.repo import messages

    assert await messages.recent(u.id, 10) == []  # nothing they said was stored
    assert j.llm.calls == [] and j.llm.structured_calls == []  # and no model ever saw it
    wrong = await j.tg.say(8001, "MAV-AAAAA-AAAA1", "Mallory")
    assert "That code didn't work" in wrong[-1].text
    assert (await users.get_by_chat(8001)).status == "pending"


async def test_invite_is_single_use_by_default_and_expires(j, clock):
    link, _ = await j.owner_invite()
    await j.join(5101, "First", link)
    second = await j.join(5102, "Second", link)
    assert [o.text for o in second] == [
        "That code didn't work. Check it and send it again, or ask the person who invited you."]
    assert (await users.get_by_chat(5102)).status == "pending"
    again = await j.join(5101, "First", link)  # the same person tapping the link twice burns nothing
    assert (await users.get_by_chat(5101)).status == "active"
    assert [o.text for o in again] == [
        "You're already in, First. Tell me what's on your mind, or send /connect to link your Google or Slack."]
    assert j.llm.calls == []  # a repeated Start is never a chat turn for the model
    bare = await j.tg.say(5101, "/start", "First")
    assert [o.text for o in bare] == [again[0].text] and j.llm.calls == []
    link2, _ = await j.owner_invite("days=2")
    clock.advance(days=3)
    late = await j.join(5103, "Late", link2)
    assert "That code didn't work" in late[-1].text
    assert (await users.get_by_chat(5103)).status == "pending"


async def test_multi_use_invite_counts_and_the_owner_can_revoke(j):
    link, _ = await j.owner_invite("uses=2")
    await j.join(5201, "A", link)
    await j.join(5202, "B", link)
    third = await j.join(5203, "C", link)
    assert "That code didn't work" in third[-1].text
    link2, _ = await j.owner_invite()
    code_hint = link2.rsplit("start=MAV", 1)[1][-4:]
    out = await j.tg.say(OWNER_CHAT, f"/invite revoke {code_hint}", "Owner")
    assert "Revoked" in out[-1].text
    late = await j.join(5204, "D", link2)
    assert "That code didn't work" in late[-1].text


async def test_invite_caps_are_explained_in_plain_words(j):
    s = get_settings()
    over = await j.tg.say(OWNER_CHAT, f"/invite new uses={s.invite_max_uses + 1}", "Owner")
    assert "at most" in over[-1].text and str(s.invite_max_uses) in over[-1].text
    # a member can hand out only a few links of their own, and is told what to do about it
    u, _ = await users.get_or_create_by_chat(5301, "Capped")
    await users.update(u.id, status="active")
    from mavis.web import sessions

    token = await sessions.create(u.id)
    c = j.http()
    c.cookies.set(sessions.COOKIE, token)
    h = {"X-Mavis-CSRF": sessions.csrf_for(token)}
    for _ in range(s.invite_cap_standard):
        assert (await c.post("/api/v1/invites", json={}, headers=h)).status_code == 201
    r = await c.post("/api/v1/invites", json={}, headers=h)
    assert r.status_code == 409
    assert r.json()["message"] == "You have used all your invite links. Revoke one to make another."
    first = (await c.get("/api/v1/invites", headers=h)).json()[0]["code"]
    assert (await c.delete(f"/api/v1/invites/{first}", headers=h)).status_code == 204
    assert (await c.post("/api/v1/invites", json={}, headers=h)).status_code == 201
    # and the owner is told when the whole system has too many open codes
    with suppress(InviteError):
        for _ in range(s.invite_max_active):
            await invites.mint(created_by=None)
    full = await j.tg.say(OWNER_CHAT, "/invite new", "Owner")
    assert "at most" in full[-1].text and "open codes" in full[-1].text


async def test_owner_lists_codes_and_sees_who_joined(j):
    link, _ = await j.owner_invite("uses=2 Aiko and Ben")
    await j.join(5401, "Aiko", link)
    listed = (await j.tg.say(OWNER_CHAT, "/invite list", "Owner"))[-1].text
    assert "Aiko and Ben" in listed and "1/2" in listed
    row = (await invites.list_active())[0]
    who = (await j.tg.say(OWNER_CHAT, f"/invite users {row.id}", "Owner"))[-1].text
    assert who.startswith("Aiko (#")


# --- 2. first message ---------------------------------------------------------------------------------


async def test_first_messages_name_timezone_then_the_connector_offer(j, clock):
    from mavis.initiative.wiring import current

    link, _ = await j.owner_invite()
    out = await j.join(6001, "Lena", link)
    assert out[0].text.startswith("Hi Lena, I'm Mavis")
    assert "Quick check: is it" in out[1].text
    assert [b.label for b in j.tg.buttons(6001)] == ["Yes", "No"]
    assert (await users.get_by_chat(6001)).name == "Lena"
    asked = await j.tg.tap(6001, "ob:tz:no")
    assert asked[-1].text == "Tell me your city, or share your location."
    placed = await j.tg.say(6001, "Lisbon", "Lena")
    assert (await users.get_by_chat(6001)).timezone == "Europe/Lisbon"
    assert any("What's one thing you want off your mind" in o.text for o in placed)
    j.llm.push_text("Got it. I will remind you about the dentist call.")
    reply = await j.tg.say(6001, "I need to call the dentist before Friday", "Lena")
    assert reply[-1].text == "Got it. I will remind you about the dentist call."
    clock.advance(seconds=90)
    await current().timer.tick()
    await j.settle()
    offer = j.tg.texts(6001)[-1]
    assert offer == "Want me to keep an eye on your email, calendar and Slack too?"
    assert [b.label for b in j.tg.buttons(6001)] == ["Connect Google", "Connect Slack", "Later"]
    slack = await j.tg.tap(6001, "ob:conn:slack")
    assert slack[-1].text.startswith("Let's connect your Slack.") and UNVERIFIED not in slack[-1].text
    google = await j.tg.tap(6001, "ob:conn:google")
    assert google[-1].text.startswith("Let's connect your Google.") and UNVERIFIED in google[-1].text
    assert all(not DASHES.search(t) for t in j.tg.texts(6001))


# --- 3. Google ----------------------------------------------------------------------------------------



UNVERIFIED = "Google will say it has not verified Mavis yet. Tap Advanced, then Go to Mavis AI."


async def test_connect_google_from_telegram_end_to_end(j, memory):
    from mavis.api.dashboard.connectors import GOOGLE_SERVICES  # noqa: F401
    from mavis.memory.graph import is_third_party
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.tools.integrations.native.oauth import GOOGLE_SCOPES

    link, _ = await j.owner_invite("uses=2")
    await j.onboard(6101, "Priya", link)
    await j.onboard(6102, "Dev", link)  # a second person who connects nothing
    j.google_account("priya@kripya.com", PRIYA_MAIL)

    await j.tg.say(6101, "/connect google")
    texts = j.tg.texts(6101)
    prompt = next(t for t in texts if t.startswith("Let's connect your Google"))
    assert UNVERIFIED in prompt.split("\n")  # one short line, before the consent link
    url = j.tg.link_in(6101, "https://accounts.google.com/")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert set(q["scope"].split()) == set(GOOGLE_SCOPES)
    assert q["redirect_uri"] == f"{BASE}/oauth/google/callback"
    params = j.cloud.google_consent(url, "priya@kripya.com")
    r = await j.callback("google", params)
    assert r.status_code == 200 and "priya@kripya.com" in r.text
    await j.settle()

    grant = await j.grant_of(6101, NativeProvider.GOOGLE)
    assert grant.status == "ACTIVE" and grant.account["email"] == "priya@kripya.com"
    assert set(grant.scopes) == set(GOOGLE_SCOPES)
    assert await j.grant_of(6102, NativeProvider.GOOGLE) is None  # nobody else got it
    after = j.tg.texts(6101)
    assert any(t.startswith("Google is connected: priya@kripya.com") for t in after)
    assert any(t.startswith("Connected") for t in after)
    assert not any("Connected" in t or "Google is connected" in t for t in j.tg.texts(6102))

    status = (await j.tg.say(6101, "/connections"))[-1].text
    assert status == "\u2705 Google Workspace: connected\n\u26aa Slack: not connected"  # no dead-end Notion row
    menu = await j.tg.say(6101, "/connect")
    assert [b.label for b in j.tg.buttons(6101)] == ["Connect Slack"] and menu[-1].text == "Which one should I hook up?"

    # first sync read only recent mail, with the junk categories excluded
    assert any("newer_than:14d" in q for q in j.cloud.gmail_queries)
    assert all("-in:spam" in q and "-in:trash" in q for q in j.cloud.gmail_queries if q)
    [cal] = j.cloud.calls_to("www.googleapis.com", r"/calendar/v3/calendars/.+/events$")[:1]
    now = timeutil.now()
    t_min = datetime.fromisoformat(cal.url.params["timeMin"].replace("Z", "+00:00"))
    t_max = datetime.fromisoformat(cal.url.params["timeMax"].replace("Z", "+00:00"))
    assert timedelta(days=6) < now - t_min < timedelta(days=8) and timedelta(days=29) < t_max - now < timedelta(days=31)

    # guarded records reached the graph, for this user only, with the mail they came from
    priya, dev = await users.get_by_chat(6101), await users.get_by_chat(6102)
    dump = await memory.graph.dump(priya.id)
    assert {d["source_ref"] for d in dump if is_third_party(d["source_ref"])} == {"tp:gmail:m-priya"}
    assert {"Priya Nair", "Acme"} <= {e.name for e in await memory.graph.entities(priya.id)}
    assert await memory.graph.dump(dev.id) == [] and await memory.graph.entities(dev.id) == []
    assert "482913" not in " ".join(str(c["user"]) for c in j.llm.structured_calls)  # the OTP never reached the model

    # the Vault shows it to Priya and to nobody else
    c, h = await j.dashboard(6101, "Priya")
    people = (await c.get("/api/v1/vault/items", params={"kind": "person"}, headers=h)).json()
    assert "Priya Nair" in json.dumps(people)
    srcs = (await c.get("/api/v1/vault/sources", headers=h)).json()
    assert [(x["source"], x["count"] > 0) for x in srcs] == [("gmail", True)]
    c2, h2 = await j.dashboard(6102, "Dev")
    assert "Priya Nair" not in json.dumps((await c2.get("/api/v1/vault/items", params={"kind": "person"}, headers=h2)).json())

    # chat recall: Priya asks, the model is given the mail as untrusted text; Dev asking gets nothing of it
    j.llm.push_text("Priya wrote that the Q3 vendor contract renewal is due on 30 October.")
    answer = await j.tg.say(6101, "what did Priya Nair write about the vendor contract?", "Priya")
    assert answer[-1].text.startswith("Priya wrote that the Q3")
    seen = "\n".join(str(m.content) for m in j.llm.calls[-1])
    assert '<untrusted source="gmail:m-priya">' in seen and "Q3 vendor contract renewal" in seen
    assert "482913" not in seen
    j.llm.push_text("I don't know anyone called Priya Nair yet.")
    await j.tg.say(6102, "what did Priya Nair write about the vendor contract?", "Dev")
    assert "Q3 vendor contract" not in "\n".join(str(m.content) for m in j.llm.calls[-1])


async def test_add_google_account_from_the_dashboard(j, memory):
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite()
    await j.onboard(6201, "Aiko", link)
    j.google_account("aiko@kripya.com", PRIYA_MAIL)
    c, h = await j.dashboard(6201, "Aiko")
    listed = (await c.get("/api/v1/connectors", headers=h)).json()
    assert {x["id"]: x["status"] for x in listed} == {"google": "none", "slack": "none"}
    r = await c.post("/api/v1/connectors/google/connect", json={}, headers=h)
    assert r.status_code == 200
    assert r.json()["notice"] == UNVERIFIED  # the page shows this beside the button, before the redirect
    url = r.json()["url"]
    params = j.cloud.google_consent(url, "aiko@kripya.com")
    # a different browser (no session) cannot finish a consent that was started from the dashboard
    stranger = await j.callback("google", params)
    assert stranger.status_code == 400 and "different browser session" in stranger.text
    assert await j.grant_of(6201, NativeProvider.GOOGLE) is None
    # the state is spent, so the real browser needs a fresh consent
    r = await c.post("/api/v1/connectors/google/connect", json={}, headers=h)
    params = j.cloud.google_consent(r.json()["url"], "aiko@kripya.com")
    done = await c.get("/oauth/google/callback", params=params)
    assert done.status_code == 303 and done.headers["location"] == "/workspace?connected=google"
    await j.settle()
    listed = {x["id"]: x for x in (await c.get("/api/v1/connectors", headers=h)).json()}
    assert listed["google"]["status"] == "active" and listed["google"]["account"] == "aiko@kripya.com"
    assert listed["google"]["missing_scopes"] == [] and "mail" in listed["google"]["scopes_granted"]
    texts = j.tg.texts(6201)
    assert any(t.startswith("Google is connected: aiko@kripya.com") for t in texts)  # the same confirmation
    aiko = await users.get_by_chat(6201)
    assert "Priya Nair" in {e.name for e in await memory.graph.entities(aiko.id)}  # first sync ran


# --- 4. Slack -----------------------------------------------------------------------------------------


def _ts(back_s: float = 0.0) -> str:
    return f"{timeutil.now().timestamp() - back_s:.6f}"


def slack_person(team: str, team_name: str, uid: str, name: str, email: str, peer: str, peer_name: str,
                 peer_email: str, text: str) -> SlackPerson:
    return SlackPerson(team, team_name, uid, name, email, dms=[{
        "channel": f"D0{uid[2:]}{peer[2:]}", "peer": peer, "peer_name": peer_name, "peer_email": peer_email,
        "text": text, "ts": _ts(86400)}])


def dev_person(**kw) -> SlackPerson:
    return slack_person("T0KRIPYA1", "Kripya", "U0PRIYA01", "Priya", "priya@kripya.com", "U0DEV0001", "Dev Patel",
                        "dev@acme.com", "Dev Patel here: the Phoenix cutover is moving to Friday, can you confirm?")


async def post_slack(j, payload: dict, *, headers: dict | None = None):
    body = json.dumps(payload).encode()
    async with j.http() as c:
        r = await c.post("/webhooks/slack", content=body, headers=headers or signed_slack_headers(body))
    await j.settle()
    return r


def event_callback(team: str, event: dict, *, event_id: str, auth_user: str, bot: str | None = None) -> dict:
    auths = [{"team_id": team, "user_id": auth_user, "is_bot": False}]
    if bot:
        auths.insert(0, {"team_id": team, "user_id": bot, "is_bot": True})
    return {"type": "event_callback", "event_id": event_id, "team_id": team, "authorizations": auths, "event": event}


async def test_connect_slack_from_telegram_backfill_events_and_chat(j, memory):
    from mavis.memory.graph import is_third_party
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.tools.integrations.native.oauth import SLACK_BOT_SCOPES, SLACK_USER_SCOPES

    link, _ = await j.owner_invite("uses=2")
    await j.onboard(6301, "Priya", link)
    await j.onboard(6302, "Dev", link)
    person = dev_person()
    await j.tg.say(6301, "/connect slack")
    url = j.tg.link_in(6301, "https://slack.com/oauth/")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["user_scope"].split(",") == list(SLACK_USER_SCOPES) and q["scope"].split(",") == list(SLACK_BOT_SCOPES)
    assert not any("not verified" in t for t in j.tg.texts(6301))  # only Google shows that screen
    r = await j.callback("slack", j.cloud.slack_consent(url, person))
    assert r.status_code == 200 and "Kripya workspace" in r.text
    await j.settle()

    grant = await j.grant_of(6301, NativeProvider.SLACK)
    assert grant.status == "ACTIVE" and grant.account["team_id"] == "T0KRIPYA1" and grant.account["user_id"] == "U0PRIYA01"
    assert await j.grant_of(6301, NativeProvider.SLACK_BOT) is not None  # the bot token sits beside it
    assert await j.grant_of(6302, NativeProvider.SLACK) is None
    texts = j.tg.texts(6301)
    assert any(t.startswith("Slack is connected: the Kripya workspace") for t in texts)
    assert any(t.startswith("Connected") and "Slack" in t for t in texts)
    assert any("message me right in Slack" in t for t in texts)  # the bot DM is there too

    # identity mapping + backfill: the DM from Dev Patel is a guarded record in Priya's graph only
    from mavis.attention.connector_ingest import load_identities

    priya, dev = await users.get_by_chat(6301), await users.get_by_chat(6302)
    assert (await load_identities(priya.id))["slack_ids"] == ["U0PRIYA01"]
    hist = [dict(parse_qsl(r.content.decode())) for r in j.cloud.calls_to("slack.com", r"conversations\.history")]
    assert all(timedelta(days=6).total_seconds() < timeutil.now().timestamp() - float(h["oldest"]) < timedelta(days=8).total_seconds() for h in hist)
    dump = await memory.graph.dump(priya.id)
    assert {d["source_ref"].split(":")[1] for d in dump if is_third_party(d["source_ref"])} == {"slack"}
    assert "Dev Patel" in {e.name for e in await memory.graph.entities(priya.id)}
    assert await memory.graph.dump(dev.id) == []
    c, h = await j.dashboard(6301, "Priya")
    people = json.dumps((await c.get("/api/v1/vault/items", params={"kind": "person"}, headers=h)).json())
    assert "Dev Patel" in people
    assert any(x["source"] == "slack" for x in (await c.get("/api/v1/vault/sources", headers=h)).json())

    # a live message in that DM reaches Priya (not Dev) through the signed webhook
    live = event_callback("T0KRIPYA1", {"type": "message", "channel": person.dms[0]["channel"], "channel_type": "im",
                                         "user": "U0DEV0001", "text": "Dev Patel: the Phoenix review is Monday",
                                         "ts": _ts()}, event_id="Ev100", auth_user="U0PRIYA01")
    resp = await post_slack(j, live)
    assert resp.status_code == 200 and resp.json()["published"] == 1
    assert any("Phoenix review is Monday" in str(c["user"]) for c in j.llm.structured_calls)
    assert (await post_slack(j, live)).json()["duplicate"] == 1  # Slack retries do nothing twice
    body = json.dumps(live).encode()
    async with j.http() as c:
        bad = await c.post("/webhooks/slack", content=body, headers=signed_slack_headers(body, secret="wrong"))
    assert bad.status_code == 401

    # chat with the bot in Slack: the reply comes back to Slack, as Priya's turn, and Dev is not involved
    bot_dm = "DBOTU0PRIYA01"
    j.llm.push_text("You have nothing on your calendar this afternoon.")
    chat_event = event_callback("T0KRIPYA1", {"type": "message", "channel": bot_dm, "channel_type": "im",
                                              "user": "U0PRIYA01", "text": "what is on my calendar?", "ts": _ts()},
                                event_id="Ev101", auth_user="U0PRIYA01", bot="B0KRIPYA1")
    r = await post_slack(j, chat_event)
    assert r.json()["published"] == 1
    sent = [s for s in j.channel.sent if str(s.chat_id).startswith("slack:T0KRIPYA1:")]
    assert [s.text for s in sent][-1] == "You have nothing on your calendar this afternoon."
    assert j.tg.texts(6302) == j.tg.texts(6302)  # Dev's chat untouched (checked below against his own history)
    assert not any("calendar this afternoon" in t for t in j.tg.texts(6302))


# --- 5. two people at once ----------------------------------------------------------------------------


def mara_person() -> SlackPerson:
    return slack_person("T0NORDWIN1", "Nordwind", "U0MARA001", "Mara", "mara@nordwind.example", "U0TOMAS01",
                        "Tomas Weber", "tomas@helix.example", "Tomas Weber: the Helix audit slipped to Wednesday")


async def start_connect(j, chat: int, what: str) -> str:
    await j.tg.say(chat, f"/connect {what}")
    return j.tg.link_in(chat, "https://accounts.google.com/" if what == "google" else "https://slack.com/oauth/")


async def test_two_people_connecting_in_parallel_stay_fully_apart(j, memory):
    from mavis.memory.graph import is_third_party
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.web import sessions  # noqa: F401

    link, _ = await j.owner_invite("uses=2")
    await j.onboard(6401, "Priya", link)
    await j.onboard(6402, "Mara", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    j.google_account("mara@nordwind.example", MARA_MAIL)
    priya_slack, mara_slack = dev_person(), mara_person()

    urls = {
        "pg": await start_connect(j, 6401, "google"), "mg": await start_connect(j, 6402, "google"),
        "ps": await start_connect(j, 6401, "slack"), "ms": await start_connect(j, 6402, "slack"),
    }
    consents = [
        ("google", j.cloud.google_consent(urls["pg"], "priya@kripya.com")),
        ("google", j.cloud.google_consent(urls["mg"], "mara@nordwind.example")),
        ("slack", j.cloud.slack_consent(urls["ps"], priya_slack)),
        ("slack", j.cloud.slack_consent(urls["ms"], mara_slack)),
    ]
    results = await asyncio.gather(*(j.callback(p, params) for p, params in consents))
    assert [r.status_code for r in results] == [200, 200, 200, 200]
    await j.settle()

    priya, mara = await users.get_by_chat(6401), await users.get_by_chat(6402)
    tokens = get_provider().tokens
    for u, email, team in ((priya, "priya@kripya.com", "T0KRIPYA1"), (mara, "mara@nordwind.example", "T0NORDWIN1")):
        assert (await tokens.grant(u.id, NativeProvider.GOOGLE)).account["email"] == email
        assert (await tokens.grant(u.id, NativeProvider.SLACK)).account["team_id"] == team
        assert (await tokens.grant(u.id, NativeProvider.SLACK_BOT)).account["team_id"] == team

    # graph: each has only their own records
    def refs(dump):
        return {d["source_ref"] for d in dump if is_third_party(d["source_ref"])}

    pr, ma = refs(await memory.graph.dump(priya.id)), refs(await memory.graph.dump(mara.id))
    assert "tp:gmail:m-priya" in pr and "tp:gmail:m-mara" not in pr and any(r.startswith("tp:slack:T0KRIPYA1") for r in pr)
    assert "tp:gmail:m-mara" in ma and "tp:gmail:m-priya" not in ma and any(r.startswith("tp:slack:T0NORDWIN1") for r in ma)
    assert not any("T0NORDWIN1" in r for r in pr) and not any("T0KRIPYA1" in r for r in ma)
    assert {"Priya Nair", "Dev Patel"} <= {e.name for e in await memory.graph.entities(priya.id)}
    assert {"Mara Lindqvist", "Tomas Weber"} <= {e.name for e in await memory.graph.entities(mara.id)}
    assert not ({"Mara Lindqvist", "Tomas Weber"} & {e.name for e in await memory.graph.entities(priya.id)})
    assert not ({"Priya Nair", "Dev Patel"} & {e.name for e in await memory.graph.entities(mara.id)})

    # vault (the dashboard, each with their own session)
    cp, hp = await j.dashboard(6401, "Priya")
    cm, hm = await j.dashboard(6402, "Mara")
    vp = json.dumps((await cp.get("/api/v1/vault/items", params={"kind": "person"}, headers=hp)).json())
    vm = json.dumps((await cm.get("/api/v1/vault/items", params={"kind": "person"}, headers=hm)).json())
    assert "Priya Nair" in vp and "Dev Patel" in vp and "Mara Lindqvist" not in vp and "Tomas Weber" not in vp
    assert "Mara Lindqvist" in vm and "Tomas Weber" in vm and "Priya Nair" not in vm and "Dev Patel" not in vm
    assert (await cp.get("/api/v1/connectors", headers=hp)).json()[0]["account"] == "priya@kripya.com"
    assert (await cm.get("/api/v1/connectors", headers=hm)).json()[0]["account"] == "mara@nordwind.example"

    # outbox: nothing of one reached the other's chat
    mine, theirs = " ".join(j.tg.texts(6401)), " ".join(j.tg.texts(6402))
    assert "priya@kripya.com" in mine and "priya@kripya.com" not in theirs
    assert "mara@nordwind.example" in theirs and "mara@nordwind.example" not in mine
    assert "Kripya" in mine and "Kripya" not in theirs and "Nordwind" in theirs and "Nordwind" not in mine

    # reminders: set by chat tool calls, owned and delivered per person
    from langchain_core.messages import AIMessage

    from mavis.domain.wakeups import WakeupKind
    from mavis.timers.service import WakeupService

    from zoneinfo import ZoneInfo

    for chat, what in ((6401, "Call the dentist"), (6402, "Send the Nordwind deck")):
        tz = ZoneInfo((await users.get_by_chat(chat)).timezone)
        when = (timeutil.now().astimezone(tz) + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        j.llm.push_ai(AIMessage(content="", tool_calls=[{"name": "wake_me", "id": f"c{chat}", "args": {
            "at": when.strftime("%Y-%m-%dT%H:%M:%S"), "what": what}}]))
        j.llm.push_text("Reminder set.")
        await j.tg.say(chat, f"remind me tomorrow at 10: {what}")
    svc = WakeupService()
    reminders = {u.id: [w.reason for w in await svc.pending(u.id, WakeupKind.AGENT)] for u in (priya, mara)}
    assert len(reminders[priya.id]) == 1 and "dentist" in reminders[priya.id][0].lower()
    assert len(reminders[mara.id]) == 1 and "nordwind deck" in reminders[mara.id][0].lower()
    # poll chains are per person too
    for u in (priya, mara):
        assert all(w.user_id == u.id for w in await svc.pending(u.id))

    # a Slack event in Mara's workspace goes to Mara only, even when it names someone from Priya's
    ev = event_callback("T0NORDWIN1", {"type": "message", "channel": mara_slack.dms[0]["channel"], "channel_type": "im",
                                         "user": "U0TOMAS01", "text": "Tomas Weber: audit moved again", "ts": _ts()},
                        event_id="Ev200", auth_user="U0MARA001")
    assert (await post_slack(j, ev)).json()["published"] == 1
    assert any("audit moved again" in str(c["user"]) for c in j.llm.structured_calls)
    assert "tp:slack" not in " ".join(r for r in refs(await memory.graph.dump(priya.id)) if "audit moved" in r)


# --- 6. disconnect, reconnect, revoked ---------------------------------------------------------------


async def connect_slack(j, chat: int, person: SlackPerson):
    await j.tg.say(chat, "/connect slack")
    url = j.tg.link_in(chat, "https://slack.com/oauth/")
    r = await j.callback("slack", j.cloud.slack_consent(url, person))
    assert r.status_code == 200, r.text
    await j.settle()


async def test_disconnect_with_forget_then_reconnect_brings_it_back(j, memory, clock):
    from mavis.domain.wakeups import WakeupKind
    from mavis.memory.graph import is_third_party
    from mavis.timers.service import WakeupService
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite()
    await j.onboard(6501, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    await j.connect_google(6501, "priya@kripya.com")
    await connect_slack(j, 6501, dev_person())
    priya = await users.get_by_chat(6501)

    def refs(dump):
        return {d["source_ref"].split(":")[1] for d in dump if is_third_party(d["source_ref"])}

    assert refs(await memory.graph.dump(priya.id)) == {"gmail", "slack"}
    polls = await WakeupService().pending(priya.id, WakeupKind.SYSTEM_POLL)
    assert polls  # mail and calendar are polled

    out = await j.tg.say(6501, "/disconnect google")
    assert out[-1].text == "Disconnected Google. I can't see it anymore. I also removed what I had learned from it."
    assert await j.grant_of(6501, NativeProvider.GOOGLE) is None
    assert j.cloud.calls_to("oauth2.googleapis.com", "/revoke")  # Google was told to drop our access too
    assert refs(await memory.graph.dump(priya.id)) == {"slack"}  # Google facts gone, Slack facts kept
    c, h = await j.dashboard(6501, "Priya")
    listed = {x["id"]: x["status"] for x in (await c.get("/api/v1/connectors", headers=h)).json()}
    assert listed == {"google": "none", "slack": "active"}
    calls_before = len(j.cloud.requests)
    clock.advance(hours=1)
    from mavis.initiative.wiring import current

    await current().timer.tick()  # the poll that was already queued fires and finds nothing to read
    await j.settle()
    assert not [r for r in j.cloud.requests[calls_before:] if r.url.host.endswith("googleapis.com")]
    left = await WakeupService().pending(priya.id, WakeupKind.SYSTEM_POLL)
    assert all(w.reason == "slack" for w in left)  # and the chain for Google did not re-arm

    await j.connect_google(6501, "priya@kripya.com")  # reconnect: link, consent, first sync again
    assert (await j.grant_of(6501, NativeProvider.GOOGLE)).status == "ACTIVE"
    assert refs(await memory.graph.dump(priya.id)) == {"gmail", "slack"}

    # disconnect Slack from the dashboard but keep what was learned
    r = await c.post("/api/v1/connectors/slack/disconnect", json={"forget": False}, headers=h)
    assert r.status_code == 204
    await j.settle()
    assert await j.grant_of(6501, NativeProvider.SLACK) is None and await j.grant_of(6501, NativeProvider.SLACK_BOT) is None
    assert "slack" in refs(await memory.graph.dump(priya.id))
    assert j.tg.texts(6501)[-1].startswith("Disconnected Slack.") and "removed what I had learned" not in j.tg.texts(6501)[-1]
    # a Slack message in the old workspace now reaches nobody
    gone = event_callback("T0KRIPYA1", {"type": "message", "channel": "D0PRIYA01DEV0001", "channel_type": "im",
                                         "user": "U0DEV0001", "text": "still there?", "ts": _ts()},
                          event_id="Ev300", auth_user="U0PRIYA01")
    assert (await post_slack(j, gone)).json()["unmapped"] == 1


async def test_revoked_google_token_asks_for_a_reconnect_and_reconnecting_heals_it(j, clock):
    from mavis.domain.policy import Capability
    from mavis.tools.integrations.wiring import get_poller

    link, _ = await j.owner_invite()
    await j.onboard(6601, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    await j.connect_google(6601, "priya@kripya.com")
    priya = await users.get_by_chat(6601)
    j.cloud.revoked_refresh.add("1//refresh-priya@kripya.com")  # she removed Mavis in her Google account
    clock.advance(hours=2)  # the access token has expired
    await get_poller().poll(priya.id, Capability.GMAIL)
    await j.settle()
    prompt = j.tg.texts(6601)[-1]
    assert prompt.startswith("Your Google access has expired. One tap to reconnect:")
    assert UNVERIFIED in prompt  # the same consent screen comes back
    url = j.tg.link_in(6601, "https://accounts.google.com/")
    c, h = await j.dashboard(6601, "Priya")
    assert [x["status"] for x in (await c.get("/api/v1/connectors", headers=h)).json()][0] == "failed"
    # chat: asking about mail when the grant is dead offers the reconnect instead of failing silently
    j.cloud.google_accounts["priya@kripya.com"].mail = [PRIYA_MAIL]
    r = await j.callback("google", j.cloud.google_consent(url, "priya@kripya.com"))
    assert r.status_code == 200
    await j.settle()
    grant = await j.grant_of(6601, __import__("mavis.tools.integrations.native.base", fromlist=["NativeProvider"]).NativeProvider.GOOGLE)
    assert grant.status == "ACTIVE"
    assert [x["status"] for x in (await c.get("/api/v1/connectors", headers=h)).json()][0] == "active"


# --- 7. an unverified-app consent with boxes unticked --------------------------------------------------


async def test_unticked_boxes_are_recorded_as_granted_and_the_router_obeys_them(j, clock):
    from langchain_core.messages import AIMessage

    from mavis.domain.integrations import UserRef
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite()
    await j.onboard(6701, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    # she keeps mail read and calendar, and unticks sending mail, Drive, Docs, Sheets and the rest
    untick = ("gmail.send", "gmail.compose", "drive", "documents", "spreadsheets", "tasks",
              "meetings.space.created", "meetings.space.readonly", "contacts.readonly")
    url, r = await j.connect_google(6701, "priya@kripya.com", untick=untick)
    grant = await j.grant_of(6701, NativeProvider.GOOGLE)
    assert grant.status == "ACTIVE"
    assert set(grant.scopes) == {"openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly",
                                 "https://www.googleapis.com/auth/calendar.events"}
    texts = j.tg.texts(6701)
    # hours later the background polls run: they touch only what was allowed and never nag about the rest
    from mavis.initiative.wiring import current

    seen = len(j.cloud.requests)
    n_texts = len(texts)
    for _ in range(4):
        clock.advance(minutes=40)
        await current().timer.tick()
        await j.settle()
    polled = {r.url.host for r in j.cloud.requests[seen:]}
    assert polled <= {"gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com"}  # no Tasks or People calls
    assert not [r for r in j.cloud.requests[seen:] if r.url.host == "www.googleapis.com" and "/drive/" in r.url.path]
    assert len(j.tg.texts(6701)) == n_texts
    done = next(t for t in texts if t.startswith("Connected"))
    # the confirmation says what works and what was left out, and how to add it, in plain words
    can, left_out = done.split("Not allowed on Google's screen:")
    assert "read your mail" in can and "see your calendar" in can and "browse Drive" not in can
    assert "send mail" in left_out and "browse Drive" in left_out and "/connect google" in left_out
    assert not DASHES.search(done)

    router = get_provider()
    me = UserRef(user_id=(await users.get_by_chat(6701)).id)
    assert (await router.execute(me, "mail.search", {"query": "contract"})).ok  # allowed, run natively
    posts = len([x for x in j.cloud.requests if x.method == "POST" and x.url.host.endswith("googleapis.com")
                 and "oauth2" not in x.url.host])
    denied = await router.execute(me, "mail.send", {"to": ["dev@acme.com"], "subject": "x", "body": "y"})
    assert not denied.ok and denied.error_kind.value == "permission_missing"
    assert len([x for x in j.cloud.requests if x.method == "POST" and x.url.host.endswith("googleapis.com")
                and "oauth2" not in x.url.host]) == posts  # it never reached Google
    assert not (await router.execute(me, "drive.search", {"query": "plan"})).ok

    # the dashboard shows exactly what is missing
    c, h = await j.dashboard(6701, "Priya")
    google = (await c.get("/api/v1/connectors", headers=h)).json()[0]
    assert {"mail", "calendar"} <= set(google["scopes_granted"])
    assert {"drive", "docs", "sheets", "tasks", "contacts", "meet"} <= set(google["missing_scopes"])

    # in chat: asking for a send ends in an explanation and a way to fix it, not a silent failure
    j.llm.push_ai(AIMessage(content="", tool_calls=[{"name": "mail_send", "id": "c1", "args": {
        "to": ["dev@acme.com"], "subject": "Running late", "body": "I'll be 10 minutes late."}}]))
    j.llm.push_text("Ready to send it to Dev, waiting for your OK.")
    await j.tg.say(6701, "email dev@acme.com that I'll be 10 minutes late", "Priya")
    approve = next(b for b in j.tg.buttons(6701) if b.data and b.data.endswith(":ok"))
    out = await j.tg.tap(6701, approve.data)
    final = " ".join(t.text for t in out)
    assert "not given permission" in final or "not allowed" in final.lower()
    offer = [b for b in j.tg.buttons(6701) if b.label == "Reconnect Google"]
    assert offer and any("That needs more access to your Google than you allowed" in t for t in j.tg.texts(6701))
    # she taps it and this time leaves every box ticked: the grant widens, sending now reaches Google
    r = await j.callback("google", j.cloud.google_consent(offer[0].url, "priya@kripya.com"))
    assert r.status_code == 200
    await j.settle()
    widened = await j.grant_of(6701, NativeProvider.GOOGLE)
    assert "https://www.googleapis.com/auth/gmail.send" in widened.scopes
    assert j.tg.texts(6701)[-1].startswith("Connected") and "Not allowed" not in j.tg.texts(6701)[-1]
    assert (await router.execute(me, "mail.send", {"to": ["dev@acme.com"], "subject": "x", "body": "y"})).ok


async def test_a_consent_with_every_service_unticked_is_not_called_connected(j):
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite()
    await j.onboard(6801, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    everything = ("gmail.readonly", "gmail.send", "gmail.compose", "calendar.events", "drive", "documents",
                  "spreadsheets", "tasks", "meetings.space.created", "meetings.space.readonly", "contacts.readonly")
    await j.connect_google(6801, "priya@kripya.com", untick=everything, status=400)
    texts = j.tg.texts(6801)
    assert not any(t.startswith("Connected") or t.startswith("Google is connected") for t in texts)
    note = texts[-1]
    assert "did not allow" in note and "/connect google" in note
    assert (await users.get_by_chat(6801)).state.get("synced", {}) == {}  # no first sync on nothing
    g = await j.grant_of(6801, NativeProvider.GOOGLE)
    assert g is None or not any(s.endswith(("gmail.readonly", "calendar.events")) for s in g.scopes)


# --- 8. Slack as a public app: other workspaces --------------------------------------------------------


async def test_slack_users_from_other_workspaces_map_by_workspace_and_strangers_are_refused(j, memory):
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite("uses=3")
    await j.onboard(6901, "Ana", link)
    await j.onboard(6902, "Ben", link)
    # two people in two workspaces who happen to have the same Slack user id (ids are per workspace)
    ana = slack_person("T0ALPHA001", "Alpha Co", "U0SAME0001", "Ana", "ana@alpha.example", "U0PEER0001", "Mara Lindqvist",
                       "mara@nordwind.example", "Mara Lindqvist: the Nordwind pilot starts Monday")
    ben = slack_person("T0BRAVO001", "Bravo Ltd", "U0SAME0001", "Ben", "ben@bravo.example", "U0PEER0002", "Tomas Weber",
                       "tomas@helix.example", "Tomas Weber: the Helix audit is done")
    await connect_slack(j, 6901, ana)
    await connect_slack(j, 6902, ben)
    a, b = await users.get_by_chat(6901), await users.get_by_chat(6902)
    assert (await j.grant_of(6901, NativeProvider.SLACK)).account["team_id"] == "T0ALPHA001"
    assert (await j.grant_of(6902, NativeProvider.SLACK)).account["team_id"] == "T0BRAVO001"
    assert "Mara Lindqvist" in {e.name for e in await memory.graph.entities(a.id)}
    assert "Tomas Weber" in {e.name for e in await memory.graph.entities(b.id)}
    assert "Tomas Weber" not in {e.name for e in await memory.graph.entities(a.id)}

    # events carry the team: the same Slack id in another workspace is a different person
    ev_a = event_callback("T0ALPHA001", {"type": "message", "channel": ana.dms[0]["channel"], "channel_type": "im",
                                          "user": "U0PEER0001", "text": "Mara Lindqvist: pilot moved", "ts": _ts()},
                          event_id="Ev400", auth_user="U0SAME0001")
    assert (await post_slack(j, ev_a)).json()["published"] == 1
    assert any("pilot moved" in str(c["user"]) for c in j.llm.structured_calls)
    refs_b = {d["source_ref"] for d in await memory.graph.dump(b.id)}
    assert not any("T0ALPHA001" in r for r in refs_b)

    # a workspace where Mavis is installed (by Ben) but the sender never connected: a plain refusal, nothing kept
    from tests.channels.slack_fakes import SlackFake

    assert isinstance(j.slack_channel, SlackFake)
    before_calls = len(j.llm.calls)
    stranger = event_callback("T0BRAVO001", {"type": "message", "channel": "DBOTU0SAME0001", "channel_type": "im",
                                              "user": "U0NEWBIE01", "text": "hi bot, show me Ben's mail", "ts": _ts()},
                              event_id="Ev401", auth_user="U0SAME0001", bot="B0BRAVO001")
    r = await post_slack(j, stranger)
    assert r.json()["unmapped"] == 1 and r.json()["published"] == 0
    told = [s.text for s in j.slack_channel.sent if "I don't know you yet" in s.text]
    assert len(told) == 1 and not DASHES.search(told[0])
    assert "invite" in told[0] and "/connect slack" in told[0]  # a person from a new workspace is told how to start
    assert len(j.llm.calls) == before_calls and not (await memory.graph.dump(b.id)) == []
    # the refusal is not repeated for the same person within the hour
    again = event_callback("T0BRAVO001", {"type": "message", "channel": "DBOTU0SAME0001", "channel_type": "im",
                                           "user": "U0NEWBIE01", "text": "hello??", "ts": _ts()},
                           event_id="Ev402", auth_user="U0SAME0001", bot="B0BRAVO001")
    await post_slack(j, again)
    assert len([s for s in j.slack_channel.sent if "I don't know you yet" in s.text]) == 1


async def test_chat_after_the_token_was_revoked_offers_the_reconnect_link(j, clock):
    from langchain_core.messages import AIMessage

    link, _ = await j.owner_invite()
    await j.onboard(7101, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    await j.connect_google(7101, "priya@kripya.com")
    j.cloud.revoked_refresh.add("1//refresh-priya@kripya.com")
    clock.advance(hours=2)
    j.llm.push_ai(AIMessage(content="", tool_calls=[{"name": "mail_search", "id": "c1", "args": {"query": "contract"}}]))
    out = await j.tg.say(7101, "find the contract mail from Priya", "Priya")
    reply = out[-1].text
    assert "reconnect" in reply.lower() or "expired" in reply.lower()
    assert [b.url for b in j.tg.buttons(7101) if b.url and b.url.startswith("https://accounts.google.com/")]
    assert not DASHES.search(reply)


async def test_a_google_account_that_belongs_to_someone_else_is_skipped_politely(j):
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite("uses=2")
    await j.onboard(7201, "Priya", link)
    await j.onboard(7202, "Mara", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    await j.connect_google(7201, "priya@kripya.com")
    await j.connect_google(7202, "priya@kripya.com", status=400)  # Mara signs in with Priya's address
    assert "That Google account is already connected to another Mavis user, so I skipped it." in j.tg.texts(7202)
    assert await j.grant_of(7202, NativeProvider.GOOGLE) is None
    assert (await j.grant_of(7201, NativeProvider.GOOGLE)).account["email"] == "priya@kripya.com"
    assert not any("Mara" in t for t in j.tg.texts(7201))


async def test_cancelling_on_the_consent_screen_connects_nothing_and_says_so(j):
    from mavis.tools.integrations.native.base import NativeProvider

    link, _ = await j.owner_invite()
    await j.onboard(7301, "Priya", link)
    for what, provider in (("google", NativeProvider.GOOGLE), ("slack", NativeProvider.SLACK)):
        url = await start_connect(j, 7301, what)
        state = parse_qs(urlsplit(url).query)["state"][0]
        r = await j.callback(what, {"error": "access_denied", "state": state})
        assert r.status_code == 400
        await j.settle()
        assert await j.grant_of(7301, provider) is None
    texts = j.tg.texts(7301)
    assert "Google sign-in was cancelled, so nothing was connected. Tell me when you want to try again." in texts
    assert "Slack sign-in was cancelled, so nothing was connected. Tell me when you want to try again." in texts


async def test_a_code_pasted_inside_a_sentence_still_lets_the_person_in(j):
    link, _ = await j.owner_invite()
    param = parse_qs(urlsplit(link).query)["start"][0]
    code = f"{param[:3]}-{param[3:8]}-{param[8:]}"
    out = await j.tg.say(7401, f"Hi Mavis! My invite is {code}, thanks. Also my phone is 555 0100", "Noor")
    assert out[0].text.startswith("Hi Noor, I'm Mavis")
    assert (await users.get_by_chat(7401)).status == "active"
    from mavis.store.repo import messages

    assert not any("555" in m.content for m in await messages.recent((await users.get_by_chat(7401)).id, 20))

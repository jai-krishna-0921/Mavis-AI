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
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

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

    async def connect_google(self, chat: int, email: str, *, untick: tuple[str, ...] = ()):
        """/connect google: the link the bot sends, the consent screen, the browser callback, then the worker
        does the rest (connection check, announcement, first sync)."""
        await self.tg.say(chat, "/connect google")
        url = self.tg.link_in(chat, "https://accounts.google.com/")
        params = self.cloud.google_consent(url, email, untick=untick)
        r = await self.callback("google", params)
        assert r.status_code == 200, r.text
        await self.settle()
        return r

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
    assert (await users.get_by_chat(5101)).status == "active" and not any("didn't work" in o.text for o in again)
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
    for _ in range(s.invite_max_active):
        await invites.mint(created_by=None)
    full = await j.tg.say(OWNER_CHAT, "/invite new", "Owner")
    assert "at most" in full[-1].text and "open codes" in full[-1].text
    # a dashboard user is capped per person and told what to do about it
    u, _ = await users.get_or_create_by_chat(5301, "Capped")
    await users.update(u.id, status="active")
    from mavis.web import sessions

    token = await sessions.create(u.id)
    c = j.http()
    c.cookies.set(sessions.COOKIE, token)
    h = {"X-Mavis-CSRF": sessions.csrf_for(token)}
    r = await c.post("/api/v1/invites", json={}, headers=h)
    assert r.status_code == 409 and "invite" in r.json()["message"].lower()


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
    assert offer == "Want me to keep an eye on your Gmail and calendar too?"
    assert [b.label for b in j.tg.buttons(6001)] == ["Connect Google", "Later"]


# --- 3. Google ----------------------------------------------------------------------------------------


async def test_connect_google_from_telegram_end_to_end(j):
    link, _ = await j.owner_invite()
    await j.onboard(6101, "Priya", link)
    j.google_account("priya@kripya.com", PRIYA_MAIL)
    r = await j.connect_google(6101, "priya@kripya.com")
    print(j.tg.texts(6101))
    print(r.text)

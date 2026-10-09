# ruff: noqa: E501
"""Edges for the new-user journey tests: Telegram, Google, Slack and the model are fakes; everything between
them is the real app (access gate, onboarding, connect flow, OAuth, token store, first sync, records, graph,
vault, outbox, dashboard API, Slack webhook)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx

from mavis.bus.inprocess import InProcessBus
from mavis.channels.fake import FakeChannel
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, InitiativeDecision
from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory.personal_layer import LayerDraft
from tests.fakes.llm import FakeLLM
from tests.tools.integrations.native.gfake import gmail_message

SIGNING_SECRET = "slack-signing-secret-for-journey-tests"
BOT_NAME = "MavisJourneyBot"
OWNER_CHAT = 900


# --- Telegram ----------------------------------------------------------------------------------------


class Telegram:
    """Raw Telegram updates in, fake channel out, through the real intake."""

    def __init__(self, bus: InProcessBus, channel: FakeChannel) -> None:
        self.bus, self.channel, self._n = bus, channel, 1000

    def _update(self) -> int:
        self._n += 1
        return self._n

    async def settle(self) -> None:
        await asyncio.wait_for(self.bus.wait_idle(), timeout=30)
        await OutboxSender(self.channel).run_once()
        await asyncio.wait_for(self.bus.wait_idle(), timeout=30)
        await OutboxSender(self.channel).run_once()

    async def say(self, chat: int, text: str, name: str = "Someone") -> list:
        from mavis.channels.telegram_updates import ingest_update

        n = self._update()
        before = len(self.channel.sent)
        await ingest_update({"update_id": n, "message": {
            "message_id": n, "date": int(timeutil.now().timestamp()), "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "first_name": name}, "text": text}}, self.bus)
        await self.settle()
        return [s for s in self.channel.sent[before:] if s.chat_id == chat and s.kind == "text"]

    async def tap(self, chat: int, data: str) -> list:
        from mavis.channels.telegram_updates import ingest_update

        n = self._update()
        before = len(self.channel.sent)

        async def answer(_id: str) -> None:
            return None

        await ingest_update({"update_id": n, "callback_query": {
            "id": f"cb{n}", "data": data, "from": {"id": chat, "first_name": "x"},
            "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}}}}, self.bus, answer)
        await self.settle()
        return [s for s in self.channel.sent[before:] if s.chat_id == chat and s.kind == "text"]

    def texts(self, chat: int) -> list[str]:
        return [s.text for s in self.channel.sent if s.chat_id == chat and s.kind == "text"]

    def link_in(self, chat: int, prefix: str) -> str:
        for s in reversed(self.channel.sent):
            if s.chat_id == chat:
                for row in s.buttons:
                    for b in row:
                        if b.url and b.url.startswith(prefix):
                            return b.url
        raise AssertionError(f"no button link starting with {prefix} for chat {chat}")

    def buttons(self, chat: int) -> list:
        for s in reversed(self.channel.sent):
            if s.chat_id == chat and s.buttons:
                return [b for row in s.buttons for b in row]
        return []


# --- the vendors -------------------------------------------------------------------------------------


@dataclass
class GoogleAccount:
    email: str
    mail: list[dict] = field(default_factory=list)  # gmail_message(...) dicts
    events: list[dict] = field(default_factory=list)
    sub: str = ""


@dataclass
class SlackPerson:
    team_id: str
    team_name: str
    user_id: str
    display: str
    email: str
    dms: list[dict] = field(default_factory=list)  # {"channel","peer","peer_name","peer_email","text","ts"}


def _b64(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Cloud:
    """Google and Slack as MockTransport handlers: PKCE is enforced, tokens belong to one account, mail and
    history are per account, a revoked refresh token answers invalid_grant, scopes are checked on every call."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.google_accounts: dict[str, GoogleAccount] = {}
        self._codes: dict[str, dict] = {}
        self._access: dict[str, dict] = {}  # access token -> {"acct", "scopes"}
        self._refresh: dict[str, dict] = {}
        self.revoked_refresh: set[str] = set()
        self.slack_people: dict[tuple[str, str], SlackPerson] = {}
        self._slack_codes: dict[str, dict] = {}
        self._slack_tokens: dict[str, dict] = {}
        self.slack_posts: list[dict] = []
        self.gmail_queries: list[str] = []
        self._n = 0

    # -- consent (what the person does in the browser) ------------------------------------------------

    def google_consent(self, url: str, email: str, *, untick: tuple[str, ...] = ()) -> dict[str, str]:
        parts = urlsplit(url)
        assert parts.netloc == "accounts.google.com", url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["access_type"] == "offline" and q["prompt"] == "consent"
        assert q["code_challenge_method"] == "S256" and q["response_type"] == "code"
        asked = q["scope"].split()
        granted = [s for s in asked if not any(s.endswith(u) or s == u for u in untick)]
        self._n += 1
        code = f"gcode-{self._n}"
        self._codes[code] = {"email": email, "challenge": q["code_challenge"], "redirect": q["redirect_uri"],
                             "client": q["client_id"], "granted": granted}
        return {"code": code, "state": q["state"]}

    def slack_consent(self, url: str, person: SlackPerson) -> dict[str, str]:
        parts = urlsplit(url)
        assert parts.netloc == "slack.com" and parts.path == "/oauth/v2/authorize", url
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self._n += 1
        code = f"scode-{self._n}"
        self._slack_codes[code] = {"person": person, "bot_scope": q["scope"], "user_scope": q["user_scope"],
                                   "redirect": q["redirect_uri"], "client": q["client_id"]}
        return {"code": code, "state": q["state"]}

    # -- the transport --------------------------------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._google_token(request)
        if host == "oauth2.googleapis.com" and path == "/revoke":
            return httpx.Response(200, json={})
        if host == "openidconnect.googleapis.com":
            return self._userinfo(request)
        if host == "slack.com" and path.startswith("/api/"):
            return self._slack(request)
        if host.endswith("googleapis.com"):
            return self._google_api(request)
        return httpx.Response(404, json={"error": f"unrouted {host}{path}"})

    # -- Google ---------------------------------------------------------------------------------------

    def _issue(self, email: str, granted: list[str], refresh: str | None = None) -> dict:
        self._n += 1
        access = f"ya29.{self._n}.{email}"
        refresh = refresh or f"1//refresh-{email}"
        self._access[access] = {"email": email, "scopes": set(granted)}
        self._refresh[refresh] = {"email": email, "scopes": granted}
        return {"access_token": access, "refresh_token": refresh, "expires_in": 3599, "token_type": "Bearer",
                "scope": " ".join(granted)}

    def _google_token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        if form.get("grant_type") == "refresh_token":
            token = form.get("refresh_token", "")
            if token in self.revoked_refresh or token not in self._refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            info = self._refresh[token]
            body = self._issue(info["email"], info["scopes"], token)
            body.pop("refresh_token")
            return httpx.Response(200, json=body)
        c = self._codes.pop(form.get("code", ""), None)
        if c is None or form.get("client_id") != c["client"] or form.get("redirect_uri") != c["redirect"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != c["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
        return httpx.Response(200, json=self._issue(c["email"], c["granted"]))

    def _bearer(self, request: httpx.Request) -> dict | None:
        return self._access.get(request.headers.get("authorization", "").removeprefix("Bearer "))

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        who = self._bearer(request)
        if who is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"sub": f"sub-{who['email']}", "email": who["email"], "email_verified": True})

    def _google_api(self, request: httpx.Request) -> httpx.Response:
        who = self._bearer(request)
        if who is None:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials", "errors": [{"reason": "authError"}]}})
        acct = self.google_accounts.setdefault(who["email"], GoogleAccount(who["email"]))
        path = request.url.path
        granted: set[str] = who["scopes"]

        def need(*names: str) -> httpx.Response | None:
            full = {n if n.startswith("https://") else f"https://www.googleapis.com/auth/{n}" for n in names}
            if granted & full:
                return None
            return httpx.Response(403, json={"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                                       "errors": [{"reason": "insufficientPermissions"}],
                                                       "status": "PERMISSION_DENIED"}})

        if path.startswith("/gmail/v1/users/me"):
            if (denied := need("gmail.readonly", "gmail.modify", "gmail.send", "gmail.compose")) is not None:
                return denied
            rest = path.removeprefix("/gmail/v1/users/me")
            if rest == "/messages" and request.method == "GET":
                q = request.url.params.get("q", "")
                self.gmail_queries.append(q)
                after = re.search(r"after:(\d+)", q)  # the poller asks only for what is newer than its cursor
                fresh = [m for m in acct.mail if not after or int(m["internalDate"]) / 1000 > int(after.group(1))]
                return httpx.Response(200, json={"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in fresh]})
            if rest.startswith("/messages/") and request.method == "GET":
                mid = rest.rsplit("/", 1)[-1]
                found = next((m for m in acct.mail if m["id"] == mid), None)
                return httpx.Response(200, json=found) if found else httpx.Response(404, json={"error": {"code": 404}})
            if rest == "/profile":
                return httpx.Response(200, json={"emailAddress": acct.email})
        if path.startswith("/calendar/v3"):
            if (denied := need("calendar.events", "calendar.readonly", "calendar")) is not None:
                return denied
            return httpx.Response(200, json={"items": acct.events})
        return httpx.Response(200, json={})

    # -- Slack ----------------------------------------------------------------------------------------

    def _slack(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = dict(parse_qsl(request.content.decode())) if request.content and not request.headers.get("content-type", "").startswith("application/json") else {}
        if request.headers.get("content-type", "").startswith("application/json") and request.content:
            form = json.loads(request.content)
        if method == "oauth.v2.access":
            c = self._slack_codes.pop(form.get("code", ""), None)
            if c is None or form.get("redirect_uri") != c["redirect"]:
                return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
            p: SlackPerson = c["person"]
            self._n += 1
            user_tok, bot_tok = f"xoxp-{self._n}-{p.user_id}", f"xoxb-{self._n}-{p.team_id}"
            self._slack_tokens[user_tok] = {"kind": "user", "person": p, "scope": set(c["user_scope"].split(","))}
            self._slack_tokens[bot_tok] = {"kind": "bot", "person": p, "scope": set(c["bot_scope"].split(","))}
            return httpx.Response(200, json={
                "ok": True, "access_token": bot_tok, "token_type": "bot", "scope": c["bot_scope"],
                "bot_user_id": f"B{p.team_id[1:]}", "app_id": "A1", "team": {"id": p.team_id, "name": p.team_name},
                "authed_user": {"id": p.user_id, "scope": c["user_scope"], "access_token": user_tok, "token_type": "user"}})
        who = self._slack_tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if who is None:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        p = who["person"]
        if method == "conversations.open":
            return httpx.Response(200, json={"ok": True, "channel": {"id": f"DBOT{p.user_id}"}})
        if method == "auth.revoke":
            return httpx.Response(200, json={"ok": True, "revoked": True})
        if method == "conversations.list":
            return httpx.Response(200, json={"ok": True, "channels": [
                {"id": d["channel"], "is_im": True, "user": d["peer"], "is_archived": False} for d in p.dms]})
        if method in ("conversations.history", "conversations.replies"):
            d = next((x for x in p.dms if x["channel"] == form.get("channel")), None)
            msgs = [{"type": "message", "user": d["peer"], "text": d["text"], "ts": d["ts"]}] if d else []
            return httpx.Response(200, json={"ok": True, "messages": msgs, "has_more": False})
        if method == "users.info":
            d = next((x for x in p.dms if x["peer"] == form.get("user")), None)
            if form.get("user") == p.user_id:
                return httpx.Response(200, json={"ok": True, "user": {"id": p.user_id, "name": p.display.lower(), "real_name": p.display, "profile": {"display_name": p.display, "email": p.email}}})
            if d:
                return httpx.Response(200, json={"ok": True, "user": {"id": d["peer"], "name": d["peer_name"].lower(), "real_name": d["peer_name"], "profile": {"display_name": d["peer_name"], "email": d["peer_email"]}}})
            return httpx.Response(200, json={"ok": False, "error": "user_not_found"})
        if method == "chat.postMessage":
            self.slack_posts.append({"as": who["kind"], "team": p.team_id, "user": p.user_id, **form})
            return httpx.Response(200, json={"ok": True, "channel": form.get("channel"), "ts": "1.1"})
        return httpx.Response(200, json={"ok": True})

    def calls_to(self, host: str, path_re: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host and re.search(path_re, r.url.path)]


def signed_slack_headers(body: bytes, *, secret: str = SIGNING_SECRET, ts: float | None = None) -> dict[str, str]:
    stamp = str(int(ts if ts is not None else time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": sig, "Content-Type": "application/json"}


# --- the model ---------------------------------------------------------------------------------------


class JourneyLLM(FakeLLM):
    """Chat replies are scripted (push_text). Memory extraction is answered from the text itself, so a
    record's graph facts follow what the record says, whatever the order the worker learns them in."""

    def __init__(self) -> None:
        super().__init__()
        self.other_structured: list[str] = []

    people = (("Priya Nair", "Acme", "the Q3 vendor contract"), ("Dev Patel", "Phoenix", "the Phoenix cutover"),
              ("Mara Lindqvist", "Nordwind", "the Nordwind pilot"), ("Tomas Weber", "Helix", "the Helix audit"))

    async def structured(self, schema, system, user, tier=None, priority="interactive", fallback=None):
        if schema is ComposedMessage:  # the proactive "here is what I noticed" after a first sync
            self.structured_calls.append({"schema": schema, "system": system, "user": user, "tier": tier})
            return ComposedMessage(send=True, messages=["I had a look through what you just connected. Ask me about it any time."])
        if schema is InitiativeDecision:  # the live-event reasoner: nothing here is worth interrupting for
            self.structured_calls.append({"schema": schema, "system": system, "user": user, "tier": tier})
            return InitiativeDecision(ignore_reason="routine")
        if schema is LayerDraft:  # the personal layer's optional phrasing pass: no rewording
            return LayerDraft()
        if schema is not Extraction:
            self.other_structured.append(schema.__name__)
            return await super().structured(schema, system, user, tier, priority=priority, fallback=fallback)
        self.structured_calls.append({"schema": schema, "system": system, "user": user, "tier": tier})
        text = str(user)
        entities, relations = [], []
        for name, org, topic in self.people:
            if name in text:
                entities += [Entity(name=name, label="Person"), Entity(name=org, label="Organization")]
                relations += [Relation(subject=name, rel="WORKS_AT", object=org, statement=f"{name} works at {org}."),
                              Relation(subject=name, rel="RELATED_TO", object="User",
                                       statement=f"{name} wrote to the user about {topic}.")]
        return Extraction(entities=entities, relations=relations)


def mail(mid: str, sender: str, subject: str, text: str, *, ts_ms: int | None = None) -> dict:
    ts_ms = ts_ms or int((timeutil.now().timestamp() - 86400) * 1000)
    return gmail_message(mid, thread=f"t-{mid}", sender=sender, subject=subject, text=text, ts=ts_ms)

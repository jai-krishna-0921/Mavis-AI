"""An in-memory Fathom over httpx.MockTransport: meetings, summary, transcript, webhooks and signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

OWN_DOMAIN = "acme.test"


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _invitee(spec: str, own_domain: str) -> dict:
    m = re.fullmatch(r"\s*(.*?)\s*<([^>]+)>\s*", spec)
    name, email = (m[1].strip('"'), m[2]) if m else (None, spec.strip())
    domain = email.split("@")[-1]
    return {
        "name": name,
        "matched_speaker_display_name": name,
        "email": email,
        "email_domain": domain,
        "is_external": domain != own_domain,
    }


def fathom_meeting(
    recording_id: int,
    title: str,
    start: str,
    end: str,
    invitees: tuple[str, ...] = (),
    action_items: tuple[str, ...] = (),
    summary: str | None = None,
    *,
    own_domain: str = OWN_DOMAIN,
    created_at: str | None = None,
) -> dict:
    """A Meeting object shaped like the Fathom API (invitees are 'Name <email>' or a bare email)."""
    return {
        "title": title,
        "meeting_title": title,
        "meeting_type": "external",
        "recording_id": recording_id,
        "url": f"https://fathom.video/calls/{recording_id}",
        "meeting_url": f"https://meet.example.test/{recording_id}",
        "share_url": f"https://fathom.video/share/{recording_id}",
        "created_at": created_at or end,
        "scheduled_start_time": start,
        "scheduled_end_time": end,
        "recording_start_time": start,
        "recording_end_time": end,
        "calendar_invitees_domains_type": "one_or_more_external",
        "shared_with": "no_teams",
        "transcript_language": "en",
        "transcript": None,
        "default_summary": (
            {"template_name": "general", "markdown_formatted": summary} if summary is not None else None
        ),
        "action_items": [
            {
                "description": text,
                "user_generated": False,
                "completed": False,
                "recording_timestamp": "00:01:00",
                "recording_playback_url": f"https://fathom.video/calls/{recording_id}?t=60",
                "assignee": {"name": None, "email": None, "team": None},
            }
            for text in action_items
        ],
        "highlights": None,
        "calendar_invitees": [_invitee(i, own_domain) for i in invitees],
        "recorded_by": {"name": "Me", "email": f"me@{own_domain}", "email_domain": own_domain, "team": None},
        "crm_matches": None,
    }


def _error(status: int, code: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": code}, headers=headers)


class FakeFathom:
    """Routes the Fathom endpoints the connector uses. Requests are recorded; unknown ones answer 404.

    Like the real API for OAuth apps, the meetings list never carries `default_summary` or `transcript`
    (use the recordings endpoints) and asking for them is a 400; `action_items` need
    `include_action_items=true`. Every call needs a Bearer token, else 401."""

    def __init__(self) -> None:
        self.meetings: list[dict] = []
        self.transcripts: dict[int, list[dict]] = {}
        self.webhooks: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.unrouted: list[httpx.Request] = []
        self.page_size = 10
        self._scripted: list[httpx.Response] = []
        self._n = 0

    # ----- seeding and scripting

    def add_meeting(self, meeting: dict, transcript: list[tuple[str, str]] | None = None) -> dict:
        self.meetings.append(meeting)
        if transcript is not None:
            self.transcripts[meeting["recording_id"]] = [
                {
                    "speaker": {"display_name": who, "matched_calendar_invitee_email": None},
                    "text": text,
                    "timestamp": f"00:00:{i:02d}",
                }
                for i, (who, text) in enumerate(transcript)
            ]
        return meeting

    def script_429(self, n: int = 1, retry_after: int = 1) -> None:
        self._scripted += [_error(429, "RateLimited", {"Retry-After": str(retry_after)}) for _ in range(n)]

    def script_error(self, status: int, code: str = "Error", n: int = 1) -> None:
        self._scripted += [_error(status, code) for _ in range(n)]

    @staticmethod
    def sign(secret: str, webhook_id: str, ts: str | int, body: str | bytes) -> str:
        """A valid `webhook-signature` header value (Standard Webhooks, v1)."""
        key = base64.b64decode(secret.removeprefix("whsec_"))
        raw = body if isinstance(body, bytes) else body.encode()
        mac = hmac.new(key, f"{webhook_id}.{ts}.".encode() + raw, hashlib.sha256).digest()
        return "v1," + base64.b64encode(mac).decode()

    def assert_all_routed(self) -> None:
        assert not self.unrouted, "unrouted requests: " + ", ".join(
            f"{r.method} {r.url.path}" for r in self.unrouted
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def executor(self, tokens: Any = None):
        """(FathomExecutor, recorded sleeps). Imported lazily: the executor lands after this fake."""
        from mavis.tools.integrations.native.fathom import FathomExecutor

        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return FathomExecutor(tokens or _FakeTokens(), self.client(), sleep=sleep), sleeps

    # ----- transport

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = "/external/v1/"
        on_api = request.url.host == "api.fathom.ai" and request.url.path.startswith(prefix)
        routed = self._route(request, request.url.path.removeprefix(prefix[:-1])) if on_api else None
        if routed is None:
            self.unrouted.append(request)
            return _error(404, "UnroutedInFakeFathom")
        if self._scripted:
            return self._scripted.pop(0)
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _error(401, "Unauthorized")
        return routed()

    def _route(self, request: httpx.Request, path: str):
        q = request.url.params
        if request.method == "GET" and path == "/meetings":
            return lambda: self._list(q)
        if m := re.fullmatch(r"/recordings/(\d+)/(summary|transcript)", path):
            if request.method == "GET":
                return lambda: self._recording(int(m[1]), m[2])
        if request.method == "POST" and path == "/webhooks":
            return lambda: self._create_webhook(request)
        if m := re.fullmatch(r"/webhooks/([^/]+)", path):
            if request.method == "DELETE":
                return lambda: self._delete_webhook(m[1])
        return None

    def _list(self, q: httpx.QueryParams) -> httpx.Response:
        if q.get("include_summary") == "true" or q.get("include_transcript") == "true":
            return _error(400, "BadRequest")
        items = self.meetings
        if after := q.get("created_after"):
            items = [m for m in items if _iso(m["created_at"]) > _iso(after)]
        if before := q.get("created_before"):
            items = [m for m in items if _iso(m["created_at"]) < _iso(before)]
        start = int(q.get("cursor") or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        out = []
        for m in chunk:
            m = json.loads(json.dumps(m))
            m["default_summary"], m["transcript"] = None, None
            if q.get("include_action_items") != "true":
                m["action_items"] = None
            out.append(m)
        return httpx.Response(
            200, json={"limit": self.page_size, "next_cursor": str(start + self.page_size) if more else None,
                       "items": out}
        )

    def _recording(self, rid: int, kind: str) -> httpx.Response:
        meeting = next((m for m in self.meetings if m["recording_id"] == rid), None)
        if meeting is None:
            return _error(404, "NotFound")
        if kind == "summary":
            return httpx.Response(
                200, json={"summary": meeting["default_summary"] or {"template_name": None,
                                                                      "markdown_formatted": None}}
            )
        return httpx.Response(200, json={"transcript": self.transcripts.get(rid, [])})

    def _create_webhook(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content or b"{}")
        includes = ("include_action_items", "include_crm_matches", "include_summary", "include_transcript")
        if (
            not body.get("destination_url")
            or not body.get("triggered_for")
            or not any(body.get(k) for k in includes)
        ):
            return _error(400, "BadRequest")
        self._n += 1
        wid = f"wh{self._n:014d}"
        hook = {
            "id": wid,
            "url": body["destination_url"],
            "secret": "whsec_" + base64.b64encode(hashlib.sha256(wid.encode()).digest()[:24]).decode(),
            "created_at": "2026-10-17T12:00:00Z",
            "triggered_for": body["triggered_for"],
            **{k: bool(body.get(k)) for k in includes},
        }
        self.webhooks[wid] = hook
        return httpx.Response(201, json=hook)

    def _delete_webhook(self, wid: str) -> httpx.Response:
        if self.webhooks.pop(wid, None) is None:
            return _error(404, "NotFound")
        return httpx.Response(204)


class _FakeTokens:
    async def access_token(self, user_id: int, provider: Any, *, force: bool = False) -> str:
        return "tok-forced" if force else "tok"

    async def account(self, user_id: int, provider: Any) -> dict | None:
        return {}


__all__ = ["FakeFathom", "fathom_meeting"]

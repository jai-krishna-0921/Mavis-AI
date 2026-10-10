"""A fake Google over httpx.MockTransport, plus a scripted TokenSource."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.google import GoogleExecutor

from .test_gmail_mime import b64, part


class Tokens:
    def __init__(self, email: str | None = "me@orbit.test", *, revoked: bool = False) -> None:
        self.calls: list[bool] = []
        self.email, self.revoked = email, revoked

    async def access_token(self, user_id: int, provider: NativeProvider, *, force: bool = False) -> str:
        assert provider is NativeProvider.GOOGLE
        if self.revoked:
            raise ReauthRequired("invalid_grant")
        self.calls.append(force)
        return "tok-forced" if force else "tok"

    async def account(self, user_id: int, provider: NativeProvider) -> dict | None:
        return {"email": self.email} if self.email else None


def error(
    status: int,
    reason: str = "",
    message: str = "boom",
    *,
    location: str | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    err: dict[str, Any] = {
        "code": status,
        "message": message,
        "errors": [{"reason": reason, "message": message}],
    }
    if location:
        err["errors"][0]["location"] = location
    return httpx.Response(status, json={"error": err}, headers=headers)


class FakeGoogle:
    """Routes (method, path regex) to a response or a callable. Requests are recorded."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.active = self.peak = 0

    def on(self, method: str, pattern: str, response: Any) -> FakeGoogle:
        import re

        self.routes.append((method, re.compile(pattern), response))
        return self

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            for method, pattern, response in self.routes:
                if request.method == method and pattern.search(request.url.path):
                    if isinstance(response, list):  # a script: one answer per call, the last repeats
                        answer = response.pop(0) if len(response) > 1 else response[0]
                    else:
                        answer = response
                    return answer(request) if callable(answer) else answer
            return error(404, "notFound", f"no fake route for {request.method} {request.url.path}")
        finally:
            self.active -= 1

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def executor(self, tokens: Tokens | None = None) -> tuple[GoogleExecutor, list[float]]:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return GoogleExecutor(tokens or Tokens(), self.client(), sleep=sleep), sleeps

    def calls(self, method: str, pattern: str) -> list[httpx.Request]:
        import re

        return [r for r in self.requests if r.method == method and re.search(pattern, r.url.path)]


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def gmail_message(
    mid: str,
    *,
    thread: str = "t1",
    sender: str = "Alice <alice@acme.com>",
    subject: str = "Hello",
    text: str = "Body text",
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    ts: int = 1_790_000_000_000,
    extra_headers: tuple[tuple[str, str], ...] = (),
    payload: dict | None = None,
) -> dict:
    headers = [
        {
            "name": "Authentication-Results",
            "value": "mx.google.com; dkim=pass header.i=@acme.com header.s=s1;"
            " dmarc=pass (p=REJECT) header.from=acme.com",
        },
        {"name": "Received", "value": "by 2002:a1 with SMTP"},
        {"name": "From", "value": sender},
        {"name": "To", "value": "me@orbit.test"},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 05 Oct 2026 10:00:00 +0530"},
        {"name": "Message-ID", "value": f"<{mid}@mail.acme.com>"},
        *({"name": k, "value": v} for k, v in extra_headers),
    ]
    body = payload or part("text/plain", text)
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": list(labels),
        "snippet": text[:100],
        "internalDate": str(ts),
        "payload": {**body, "headers": headers + body.get("headers", [])},
    }


__all__ = ["FakeGoogle", "Tokens", "b64", "body_of", "error", "gmail_message"]

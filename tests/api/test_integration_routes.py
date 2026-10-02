import base64
import hashlib
import hmac
import json
import time

import httpx
from fastapi import FastAPI

from mavis.api.routes import connect, integrations
from mavis.bus import get_bus
from mavis.domain.events import JobKind
from mavis.domain.policy import Capability
from mavis.store.repo import connections
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.composio import ComposioProvider
from tests.tools.integrations.fakes import FakeBus

SECRET = "whsec_test"


def app_with(bus, provider=None) -> FastAPI:
    app = FastAPI()
    app.include_router(connect.router)
    app.include_router(integrations.router)
    app.dependency_overrides[get_bus] = lambda: bus
    if provider is not None:
        app.dependency_overrides[get_provider] = lambda: provider
    return app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_callback_enqueues_check_and_renders_page(db):
    bus = FakeBus()
    pid = await connections.create_pending(1, Capability.GMAIL, "", None)
    async with client(app_with(bus)) as c:
        r = await c.get(f"/connect/callback?p={pid}")
    assert r.status_code == 200 and "you can close this tab" in r.text
    [job] = bus.jobs
    assert job.kind is JobKind.CONNECTION_CHECK and job.payload == {"pending_id": pid} and job.user_id == 1


async def test_callback_unknown_pending_still_renders(db):
    bus = FakeBus()
    async with client(app_with(bus)) as c:
        r = await c.get("/connect/callback?p=999999")
    assert r.status_code == 200 and bus.jobs == []


async def test_callback_garbage_param_still_renders(db):
    bus = FakeBus()
    async with client(app_with(bus)) as c:
        r1 = await c.get("/connect/callback?p=abc")
        r2 = await c.get("/connect/callback")
    assert r1.status_code == 200 and r2.status_code == 200 and bus.jobs == []


def sign(body: bytes, secret: str = SECRET, wid: str = "m1"):
    ts = str(int(time.time()))
    msg = f"{wid}.{ts}.{body.decode()}".encode()
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest())
    return {"webhook-id": wid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig.decode()}",
            "content-type": "application/json"}


def signed_body(payload):
    body = json.dumps(payload).encode()
    return body, sign(body)


PAYLOAD = {"id": "m1", "type": "composio.trigger.message",
           "metadata": {"trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE", "user_id": "mavis-1"},
           "data": {"messageId": "x1", "subject": "Hello", "sender": "a@b.com", "labelIds": ["INBOX"]}}


async def test_webhook_publishes_events_once():
    bus = FakeBus()
    provider = ComposioProvider(api_key="k", webhook_secret=SECRET)
    body, headers = signed_body(PAYLOAD)
    async with client(app_with(bus, provider)) as c:
        r1 = await c.post("/webhooks/integrations", content=body, headers=headers)
        r2 = await c.post("/webhooks/integrations", content=body, headers=headers)
    assert r1.json() == {"accepted": 1, "received": 1}
    assert r2.json() == {"accepted": 0, "received": 1}
    assert [e.id for e in bus.events] == ["gmail:1:msg:x1"]


async def test_webhook_rejects_bad_signature_and_publishes_nothing():
    bus = FakeBus()
    provider = ComposioProvider(api_key="k", webhook_secret=SECRET)
    body, headers = signed_body(PAYLOAD)
    headers["webhook-signature"] = "v1,AAAA"
    async with client(app_with(bus, provider)) as c:
        r = await c.post("/webhooks/integrations", content=body, headers=headers)
    assert r.status_code == 401 and bus.events == []


async def test_webhook_rejects_when_secret_is_empty():
    bus = FakeBus()
    provider = ComposioProvider(api_key="k", webhook_secret="")
    body = json.dumps(PAYLOAD).encode()
    async with client(app_with(bus, provider)) as c:
        r = await c.post("/webhooks/integrations", content=body, headers=sign(body, secret=""))
    assert r.status_code == 401 and bus.events == []


async def test_webhook_bad_json_is_400():
    bus = FakeBus()
    provider = ComposioProvider(api_key="k", webhook_secret=SECRET)
    body = b"not json"
    async with client(app_with(bus, provider)) as c:
        r = await c.post("/webhooks/integrations", content=body, headers=sign(body))
    assert r.status_code == 400


async def test_webhook_non_object_body_and_non_utf8_never_500():
    bus = FakeBus()
    provider = ComposioProvider(api_key="k", webhook_secret=SECRET)
    async with client(app_with(bus, provider)) as c:
        body = b"[1, 2]"
        r1 = await c.post("/webhooks/integrations", content=body, headers=sign(body))
        r2 = await c.post("/webhooks/integrations", content=b"\xff\xfe", headers=sign(b"x"))
    assert r1.status_code == 400 and r2.status_code == 401


async def test_callback_throttles_rapid_requests(db):
    bus = FakeBus()
    pid = await connections.create_pending(1, Capability.GMAIL, "", None)
    async with client(app_with(bus)) as c:
        bodies = [(await c.get(f"/connect/callback?p={pid}")).text for _ in range(10)]
    assert len(bus.jobs) == 1 and len(set(bodies)) == 1


async def test_callback_ignores_resolved_unknown_and_expired(db):
    from datetime import timedelta

    from mavis.domain import timeutil
    from mavis.domain.integrations import PendingStatus

    bus = FakeBus()
    done = await connections.create_pending(1, Capability.GMAIL, "", None)
    await connections.resolve(done, PendingStatus.ACTIVE)
    stale = timeutil.now() - timedelta(hours=25)
    old = await connections.create_pending(1, Capability.GMAIL, "", None, now=stale)
    async with client(app_with(bus)) as c:
        pages = [(await c.get(f"/connect/callback?p={p}")).text for p in (done, old, 424242)]
    assert bus.jobs == [] and len(set(pages)) == 1

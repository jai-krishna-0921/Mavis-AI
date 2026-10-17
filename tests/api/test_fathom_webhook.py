"""POST /webhooks/fathom/{route_token}: size cap, route token, Standard Webhooks signature, replay, shape."""

import base64
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from mavis.api.routes import fathom
from mavis.bus import get_bus
from mavis.domain.events import JobKind
from mavis.worker import locks
from tests.tools.integrations.fakes import FakeBus
from tests.tools.integrations.native.fathomfake import FakeFathom, fathom_meeting

SECRET = "whsec_" + base64.b64encode(b"k" * 24).decode()
TOKEN, UID = "route-token-abc", 7


class Tokens:
    async def owner_of_key(self, provider, key):
        return UID if key == TOKEN else None

    async def reveal(self, user_id, provider):
        return (SECRET, None) if user_id == UID else None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(fathom, "_tokens", lambda: Tokens())
    locks._claims.clear()
    bus = FakeBus()
    app = FastAPI()
    app.include_router(fathom.router)
    app.dependency_overrides[get_bus] = lambda: bus
    return app, bus


def signed(body: bytes, *, wid="msg_1", ts=None, secret=SECRET) -> dict:
    ts = str(int(time.time()) if ts is None else ts)
    return {"webhook-id": wid, "webhook-timestamp": ts,
            "webhook-signature": FakeFathom.sign(secret, wid, ts, body), "content-type": "application/json"}


async def post(app, body: bytes, headers=None, token=TOKEN):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/webhooks/fathom/{token}", content=body,
                            headers=signed(body) if headers is None else headers)


def payload(rid=42) -> bytes:
    return json.dumps(fathom_meeting(rid, "Sync", "2026-10-17T10:00:00Z", "2026-10-17T10:30:00Z")).encode()


async def test_valid_signature_enqueues_once_and_replay_is_dropped(env):
    app, bus = env
    body = payload()
    assert (await post(app, body)).status_code == 204
    assert (await post(app, body)).status_code == 204  # same webhook-id: dropped quietly
    assert len(bus.jobs) == 1
    job = bus.jobs[0]
    assert job.id == f"fathom:{UID}:42" and job.kind is JobKind.FATHOM_RECORDING and job.user_id == UID
    assert job.payload["source"] == "webhook" and job.payload["meeting"]["recording_id"] == 42


async def test_bad_signature_401_nothing_enqueued(env):
    app, bus = env
    body = payload()
    wrong = signed(body, secret="whsec_" + base64.b64encode(b"x" * 24).decode())
    assert (await post(app, body, wrong)).status_code == 401
    assert (await post(app, body, {"content-type": "application/json"})).status_code == 401
    assert bus.jobs == []


async def test_stale_timestamp_401(env):
    app, bus = env
    body = payload()
    assert (await post(app, body, signed(body, ts=int(time.time()) - 301))).status_code == 401
    assert bus.jobs == []


async def test_unknown_route_token_404(env):
    app, bus = env
    assert (await post(app, payload(), token="nope")).status_code == 404
    assert bus.jobs == []


async def test_body_over_cap_413(env):
    app, bus = env
    assert (await post(app, b"x" * (1024 * 1024 + 1))).status_code == 413
    assert bus.jobs == []


async def test_non_int_recording_id_400(env):
    app, bus = env
    for bad in ("42", None, True, -1, 0, 1.5):
        body = json.dumps({"recording_id": bad}).encode()
        assert (await post(app, body, signed(body, wid=f"w{bad}"))).status_code == 400
    assert (await post(app, b"not json", signed(b"not json", wid="wjson"))).status_code == 400
    assert bus.jobs == []


async def test_non_ascii_timestamp_is_401_not_500(env):
    app, bus = env
    body = payload()
    h = signed(body)
    h["webhook-timestamp"] = "2"
    raw = [(k.encode(), v.encode()) for k, v in h.items() if k != "webhook-timestamp"]
    raw.append((b"webhook-timestamp", b"\xb2"))  # latin-1 superscript two: isdigit() is True, int() raises
    assert (await post(app, body, raw)).status_code == 401
    assert bus.jobs == []

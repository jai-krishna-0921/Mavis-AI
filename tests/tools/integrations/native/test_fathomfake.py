import base64
import hashlib
import hmac

import pytest

from .fathomfake import FakeFathom, fathom_meeting

BASE = "https://api.fathom.ai/external/v1"
H = {"Authorization": "Bearer tok"}


def test_sign_matches_standard_webhooks_reference_vector():
    # the vector published in the Standard Webhooks specification repository
    sig = FakeFathom.sign(
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "msg_p5jXN8AQM9LWM0D4loKWxJek",
        "1614265330",
        '{"test": 2432232314}',
    )
    assert sig == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_sign_matches_independent_hmac_construction():
    secret = "whsec_" + base64.b64encode(b"k" * 24).decode()
    mac = hmac.new(b"k" * 24, b"wh_1.1700000000.{}", hashlib.sha256).digest()
    assert FakeFathom.sign(secret, "wh_1", 1700000000, "{}") == "v1," + base64.b64encode(mac).decode()


async def test_meetings_paginate_and_filter_by_created_after():
    f = FakeFathom()
    for i in range(1, 6):
        f.add_meeting(fathom_meeting(i, f"M{i}", f"2026-10-0{i}T10:00:00Z", f"2026-10-0{i}T11:00:00Z"))
    f.page_size = 2
    async with f.client() as c:
        first = (await c.get(f"{BASE}/meetings", headers=H)).json()
        second = (await c.get(f"{BASE}/meetings", params={"cursor": first["next_cursor"]}, headers=H)).json()
        late = (
            await c.get(f"{BASE}/meetings", params={"created_after": "2026-10-03T12:00:00Z"}, headers=H)
        ).json()
    assert [m["recording_id"] for m in first["items"]] == [1, 2] and first["next_cursor"]
    assert [m["recording_id"] for m in second["items"]] == [3, 4]
    assert [m["recording_id"] for m in late["items"]] == [4, 5] and late["next_cursor"] is None


async def test_summary_transcript_and_oauth_limits():
    f = FakeFathom()
    f.add_meeting(
        fathom_meeting(7, "Sync", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z", summary="# Notes"),
        transcript=[("Alice", "hello")],
    )
    async with f.client() as c:
        s = (await c.get(f"{BASE}/recordings/7/summary", headers=H)).json()
        t = (await c.get(f"{BASE}/recordings/7/transcript", headers=H)).json()
        miss = await c.get(f"{BASE}/recordings/8/summary", headers=H)
        bad = await c.get(f"{BASE}/meetings", params={"include_summary": "true"}, headers=H)
        listed = (await c.get(f"{BASE}/meetings", headers=H)).json()["items"][0]
    assert s["summary"]["markdown_formatted"] == "# Notes"
    assert t["transcript"][0]["speaker"]["display_name"] == "Alice" and t["transcript"][0]["text"] == "hello"
    assert miss.status_code == 404 and bad.status_code == 400
    assert listed["default_summary"] is None and listed["transcript"] is None


async def test_meeting_builder_invitees_and_action_items():
    m = fathom_meeting(
        1, "T", "2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z",
        invitees=("Bob <bob@other.test>", "me@acme.test"), action_items=("Send deck",),
    )
    assert [i["is_external"] for i in m["calendar_invitees"]] == [True, False]
    assert m["calendar_invitees"][0]["email_domain"] == "other.test"
    assert m["action_items"][0]["description"] == "Send deck" and m["action_items"][0]["completed"] is False


async def test_webhook_create_and_delete():
    f = FakeFathom()
    async with f.client() as c:
        bad = await c.post(f"{BASE}/webhooks", json={"destination_url": "https://x.test/h"}, headers=H)
        ok = await c.post(
            f"{BASE}/webhooks",
            json={"destination_url": "https://x.test/h", "triggered_for": ["my_recordings"],
                  "include_summary": True},
            headers=H,
        )
        wid = ok.json()["id"]
        gone = await c.delete(f"{BASE}/webhooks/{wid}", headers=H)
        again = await c.delete(f"{BASE}/webhooks/{wid}", headers=H)
    assert bad.status_code == 400 and ok.status_code == 201
    assert ok.json()["secret"].startswith("whsec_") and f.webhooks == {}
    assert gone.status_code == 204 and again.status_code == 404


async def test_auth_failures_scripts_and_unrouted():
    f = FakeFathom()
    f.script_429(1)
    f.script_error(401, "Unauthorized")
    async with f.client() as c:
        a = await c.get(f"{BASE}/meetings", headers=H)
        b = await c.get(f"{BASE}/meetings", headers=H)
        noauth = await c.get(f"{BASE}/meetings")
        miss = await c.get(f"{BASE}/nothing", headers=H)
    assert a.status_code == 429 and a.headers["Retry-After"] == "1"
    assert b.status_code == 401 and noauth.status_code == 401
    assert miss.status_code == 404
    with pytest.raises(AssertionError, match="/nothing"):
        f.assert_all_routed()


async def test_wrong_host_or_prefix_is_unrouted():
    f = FakeFathom()
    async with f.client() as c:
        host = await c.get("https://api.fathom.example/external/v1/meetings", headers=H)
        prefix = await c.get("https://api.fathom.ai/v1/meetings", headers=H)
        ok = await c.get(f"{BASE}/meetings", headers=H)
    assert host.status_code == 404 and prefix.status_code == 404 and ok.status_code == 200
    assert len(f.unrouted) == 2

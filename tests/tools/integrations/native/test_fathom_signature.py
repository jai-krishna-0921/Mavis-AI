"""verify_fathom_signature: the Standard Webhooks check, on its own."""

import base64

from mavis.tools.integrations.native.fathom_api import verify_fathom_signature
from tests.tools.integrations.native.fathomfake import FakeFathom

SECRET = "whsec_" + base64.b64encode(b"s" * 24).decode()
BODY = b'{"recording_id": 1}'
NOW = 1_800_000_000


def headers(sig=None, *, ts=NOW, wid="msg_1", secret=SECRET) -> dict:
    return {"Webhook-Id": wid, "Webhook-Timestamp": str(ts),
            "Webhook-Signature": sig or FakeFathom.sign(secret, wid, ts, BODY)}


def test_valid():
    assert verify_fathom_signature(SECRET, headers(), BODY, NOW)
    assert verify_fathom_signature(SECRET, headers(), BODY, NOW + 299)


def test_one_of_several_signatures_matches():
    good = FakeFathom.sign(SECRET, "msg_1", NOW, BODY)
    other = FakeFathom.sign("whsec_" + base64.b64encode(b"o" * 24).decode(), "msg_1", NOW, BODY)
    assert verify_fathom_signature(SECRET, headers(f"{other} v2,xxx {good}"), BODY, NOW)
    assert not verify_fathom_signature(SECRET, headers(f"{other} v2,xxx"), BODY, NOW)


def test_wrong_secret_or_body():
    other = "whsec_" + base64.b64encode(b"o" * 24).decode()
    assert not verify_fathom_signature(SECRET, headers(secret=other), BODY, NOW)
    assert not verify_fathom_signature(SECRET, headers(), BODY + b" ", NOW)
    assert not verify_fathom_signature("", headers(), BODY, NOW)
    assert not verify_fathom_signature("whsec_!!notbase64", headers(), BODY, NOW)


def test_stale_timestamp():
    assert not verify_fathom_signature(SECRET, headers(), BODY, NOW + 301)
    assert not verify_fathom_signature(SECRET, headers(), BODY, NOW - 301)


def test_non_ascii_timestamp_is_rejected_not_raised():
    h = headers()
    h["Webhook-Timestamp"] = "١٧٠٠٠٠٠٠٠٠"  # Arabic-Indic digits
    assert verify_fathom_signature(SECRET, h, BODY, NOW) is False
    h["Webhook-Timestamp"] = "²"  # isdigit() True, int() raises
    assert verify_fathom_signature(SECRET, h, BODY, NOW) is False

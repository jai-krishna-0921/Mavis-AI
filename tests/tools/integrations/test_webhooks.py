import base64
import hashlib
import hmac
import json
import time

import pytest

from mavis.domain.errors import IntegrationError, WebhookVerificationError
from mavis.domain.events import EventType, Trust
from mavis.tools.integrations.composio_webhooks import parse_composio_webhook, verify_signature
from mavis.tools.integrations.normalize import normalize_calendar_event, normalize_email

SECRET = "whsec_test"
GMAIL_RAW = {
    "messageId": "abc",
    "threadId": "t1",
    "sender": "Google <no-reply@accounts.google.com>",
    "subject": "Security alert",
    "messageText": "New sign-in to your account from a Windows device",
    "labelIds": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
    "messageTimestamp": "2026-10-04T14:22:00Z",
}


def signed(payload: dict, *, wid="msg_1", ts=None, secret=SECRET):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()) if ts is None else ts)
    sig = base64.b64encode(
        hmac.new(secret.encode(), f"{wid}.{ts}.{body.decode()}".encode(), hashlib.sha256).digest()
    )
    return {"webhook-id": wid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig.decode()}"}, body


def v3(trigger_slug: str, data: dict, user="mavis-7"):
    return {
        "id": "msg_1",
        "type": "composio.trigger.message",
        "metadata": {"trigger_slug": trigger_slug, "user_id": user, "connected_account_id": "ca_1"},
        "data": data,
        "timestamp": "2026-10-04T14:22:05Z",
    }


def test_gmail_v3_payload_becomes_email_event():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW))
    [ev] = parse_composio_webhook(headers, body, SECRET)
    assert ev.id == "gmail:7:msg:abc"
    assert ev.type is EventType.EMAIL_RECEIVED
    assert ev.user_id == 7
    assert ev.trust is Trust.UNTRUSTED
    assert ev.payload["subject"] == "Security alert"
    assert ev.payload["from_address"] == "no-reply@accounts.google.com"


def test_calendar_slack_notion_payloads():
    cal = {
        "id": "ev1",
        "summary": "Interview prep",
        "updated": "2026-10-04T10:00:00Z",
        "start": {"dateTime": "2026-10-05T10:00:00+05:30"},
        "end": {"dateTime": "2026-10-05T11:00:00+05:30"},
        "attendees": [{"email": "jawahar@example.com"}],
    }
    h, b = signed(v3("GOOGLECALENDAR_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER", cal))
    [ev] = parse_composio_webhook(h, b, SECRET)
    assert ev.type is EventType.CALENDAR_CHANGED and ev.id == "gcal:7:ev1:2026-10-04T10:00:00Z"
    h, b = signed(v3("SLACK_RECEIVE_MESSAGE", {"channel": "C1", "ts": "171.2", "user": "U1", "text": "hey"}))
    [ev] = parse_composio_webhook(h, b, SECRET)
    assert ev.type is EventType.SLACK_MESSAGE and ev.id == "slack:7:C1:171.2"
    h, b = signed(v3("NOTION_PAGE_UPDATED_TRIGGER", {"id": "p1", "last_edited_time": "2026-10-04T09:00:00Z"}))
    [ev] = parse_composio_webhook(h, b, SECRET)
    assert ev.type is EventType.NOTION_CHANGED and ev.id == "notion:7:p1:2026-10-04T09:00:00Z"


def test_bad_signature_rejected():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW), secret="wrong")
    with pytest.raises(WebhookVerificationError):
        parse_composio_webhook(headers, body, SECRET)


def test_stale_timestamp_rejected():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW), ts=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError, match="stale"):
        verify_signature(SECRET, headers, body)


def test_empty_secret_rejects_everything():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW))
    with pytest.raises(WebhookVerificationError, match="not set"):
        verify_signature("", headers, body)


def test_foreign_identity_ignored():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW, user="someone-else"))
    assert parse_composio_webhook(headers, body, SECRET) == []


def test_normalize_email_camel_and_snake_agree():
    snake = {
        "message_id": "abc",
        "thread_id": "t1",
        "sender": GMAIL_RAW["sender"],
        "subject": "Security alert",
        "message_text": GMAIL_RAW["messageText"],
        "label_ids": GMAIL_RAW["labelIds"],
        "message_timestamp": "2026-10-04T14:22:00Z",
    }
    assert normalize_email(snake) == normalize_email(GMAIL_RAW)


def test_list_unsubscribe_header_detected():
    raw = {**GMAIL_RAW, "payload": {"headers": [{"name": "List-Unsubscribe", "value": "<mailto:x@y>"}]}}
    assert normalize_email(raw)["list_unsubscribe"] is True
    assert normalize_email(GMAIL_RAW)["list_unsubscribe"] is False


def test_normalize_calendar_all_day_and_epoch():
    out = normalize_calendar_event({"id": "e", "summary": "Holiday", "start": {"date": "2026-10-05"}})
    assert out["start"].startswith("2026-10-05T00:00:00")
    out = normalize_email({**GMAIL_RAW, "messageTimestamp": "1790950920000"})
    assert out["received_at"].startswith("2026-")


def test_email_event_payload_has_headers_and_from_me():
    from mavis.tools.integrations.normalize import email_event

    raw = {
        **GMAIL_RAW,
        "labelIds": ["SENT"],
        "payload": {
            "headers": [
                {"name": "List-Unsubscribe", "value": "<mailto:x@y>"},
                {"name": "X-Other", "value": "z"},
            ]
        },
    }
    ev = email_event(7, raw, "composio")
    assert ev is not None
    assert ev.payload["headers"] == {"list-unsubscribe": "<mailto:x@y>"}
    assert ev.payload["from_me"] is True


def test_calendar_and_slack_extra_keys():
    from mavis.tools.integrations.normalize import normalize_slack

    cal = normalize_calendar_event(
        {"id": "e", "summary": "Standup", "start": {"dateTime": "2026-10-05T10:00:00Z"}}
    )
    assert cal["title"] == "Standup" and cal["starts_at"] == cal["start"]
    assert normalize_slack({"channel": "C", "ts": "1", "user": "U1"})["from"] == "U1"


def test_non_ascii_signature_is_verification_error():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW))
    headers["webhook-signature"] = "v1,café"
    with pytest.raises(WebhookVerificationError):
        verify_signature(SECRET, headers, body)


def test_future_timestamp_and_malformed_timestamp_rejected():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW), ts=int(time.time()) + 3600)
    with pytest.raises(WebhookVerificationError, match="stale"):
        verify_signature(SECRET, headers, body)
    headers["webhook-timestamp"] = "abc"
    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_signature(SECRET, headers, body)


def test_zero_tolerance_rejected_explicitly():
    headers, body = signed(v3("GMAIL_NEW_GMAIL_MESSAGE", GMAIL_RAW))
    with pytest.raises(ValueError):
        verify_signature(SECRET, headers, body, tolerance_s=0)


@pytest.mark.parametrize("payload", [[1, 2], {"metadata": "x", "data": {}}, {"metadata": {}, "data": [1]}])
def test_non_object_payload_shapes_raise_integration_error(payload):
    headers, body = signed(payload)
    with pytest.raises(IntegrationError):
        parse_composio_webhook(headers, body, SECRET)


def test_provider_event_ids_are_user_scoped():
    from mavis.tools.integrations.normalize import calendar_event, email_event

    raw = {"messageId": "m1", "sender": "a <a@x.com>"}
    assert email_event(1, raw, "x").id != email_event(2, raw, "x").id
    cal = {"id": "ev1", "updated": "2026-10-04T10:00:00Z"}
    assert calendar_event(1, cal, "x").id != calendar_event(2, cal, "x").id

# ruff: noqa: E501
from mavis.tools.integrations.normalize import calendar_event, email_event, normalize_teams, teams_event


def test_outlook_event_ids():
    m = {"messageId": "ms:AAA=", "threadId": "ms:T", "sender": "A <a@b.com>", "subject": "s", "messageText": "x", "labelIds": ["INBOX"],
         "messageTimestamp": "2026-10-15T08:00:00Z"}
    assert email_event(1, m, "poller", prefix="outlook").id == "outlook:1:msg:AAA="
    assert email_event(1, {**m, "messageId": "g1"}, "poller").id == "gmail:1:msg:g1"       # default unchanged
    ev = {"id": "ms:E1", "summary": "S", "start": {"dateTime": "2026-10-16T09:00:00+00:00"}, "updated": "2026-10-15T00:00:00Z"}
    assert calendar_event(1, ev, "poller", prefix="ocal").id == "ocal:1:ms:E1:2026-10-15T00:00:00Z"


def test_teams_event():
    raw = {"chat_id": "ms:c1", "message_id": "m1", "from": "Priya", "from_id": "u1", "from_address": "p@x.com", "text": "hi",
           "at": "2026-10-15T11:00:00Z", "chat_type": "oneOnOne", "members": [], "from_me": False}
    ev = teams_event(1, raw, "poller")
    assert ev.id == "teams:1:c1:m1" and ev.payload["text"] == "hi" and ev.trust.value == "untrusted"
    assert teams_event(1, {**raw, "message_id": ""}, "poller") is None
    assert normalize_teams(raw)["chat_id"] == "c1"

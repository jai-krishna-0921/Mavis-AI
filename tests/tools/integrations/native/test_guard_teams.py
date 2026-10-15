# ruff: noqa: E501
from mavis.tools.integrations.native.guard import Mute, should_ingest_email, should_ingest_teams


def n(**kw):
    base = {"chat_id": "c", "message_id": "m", "from": "Priya", "from_id": "u1", "from_address": "priya@corp.test",
            "from_me": False, "text": "hello", "kind": "message", "chat_type": "oneOnOne"}
    return {**base, **kw}


def test_keeps_a_human_message_and_the_users_own():
    assert should_ingest_teams(n())
    assert should_ingest_teams(n(from_me=True))


def test_drops_system_bot_empty_and_muted():
    assert should_ingest_teams(n(kind="systemEventMessage")).reason == "system_event"
    assert should_ingest_teams(n(from_id="")).reason == "no_author"
    assert should_ingest_teams(n(text="  ")).reason == "empty"
    assert should_ingest_teams(n(), Mute(senders=frozenset({"priya@corp.test"}))).reason == "muted_sender"


def test_outlook_other_label_is_bulk_but_gmail_labels_are_unchanged():
    assert should_ingest_email({"labels": ["INBOX", "CATEGORY_OTHER"]}).lane == "bulk"
    assert should_ingest_email({"labels": ["INBOX", "CATEGORY_PRIMARY"]}).lane == "graph"
    assert should_ingest_email({"labels": ["CATEGORY_PROMOTIONS"]}).lane == "bulk"

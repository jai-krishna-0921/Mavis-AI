# ruff: noqa: E501
from mavis.memory import records

IDS = {"emails": ["me@corp.test"], "slack_ids": []}


def test_outlook_email_record_uses_its_own_prefix_and_strips_ms():
    n = {"message_id": "ms:AAA=", "from": "Priya <priya@corp.test>", "from_name": "Priya", "from_address": "priya@corp.test",
         "to": "me@corp.test", "subject": "Plan", "snippet": "hi", "labels": ["INBOX"], "received_at": "2026-10-14T08:00:00+00:00",
         "sender_authenticated": True, "headers": {}}
    job = records.email_record(1, n, self_ids=IDS, source="outlook")
    assert job.source_ref == "outlook:AAA=" and job.kind == "email"
    assert records.email_record(1, n, self_ids=IDS).source_ref == "gmail:ms:AAA="    # default unchanged


def test_teams_record():
    n = {"chat_id": "ms:19:abc@unq.gbl.spaces", "message_id": "171", "from": "Priya Nair", "from_id": "u1",
         "from_address": "priya@corp.test", "from_me": False, "text": "Can you review the deck by Friday?",
         "at": "2026-10-14T10:00:00Z", "chat_type": "oneOnOne", "external": False,
         "members": [{"name": "Priya Nair", "email": "priya@corp.test"}, {"name": "Me", "email": "me@corp.test"}]}
    job = records.teams_record(1, n, self_ids=IDS)
    assert job.source_ref == "teams:19:abc@unq.gbl.spaces:171" and job.kind == "teams" and job.trust == records.TRUST_MEDIUM
    assert job.label == "Teams DM from Priya Nair" and any(p.is_user and p.email == "me@corp.test" for p in job.people)
    assert "direct message" in job.text
    assert records.teams_record(1, {**n, "external": True}, self_ids=IDS).trust == records.TRUST_LOW
    own = records.teams_record(1, {**n, "from_me": True, "from_address": "me@corp.test", "from": "Me"}, self_ids=IDS)
    assert own.authored and own.label == "Teams message you wrote"


def test_teams_roster_flag_marks_the_user_even_without_known_addresses():
    n = {"chat_id": "c", "message_id": "1", "from": "P", "from_id": "u", "from_address": "p@x.com", "from_me": False,
         "text": "hi there", "at": "2026-10-14T10:00:00Z", "chat_type": "group",
         "members": [{"name": "Me", "email": "mine@elsewhere.test", "me": True}]}
    job = records.teams_record(1, n, self_ids={"emails": [], "slack_ids": []})
    assert [p.is_user for p in job.people if p.email == "mine@elsewhere.test"] == [True]


def test_teams_record_redacts_secrets():
    n = {"chat_id": "c", "message_id": "1", "from": "P", "from_id": "u", "from_address": "p@x.com", "from_me": False,
         "text": "my password is hunter2hunter2", "at": "2026-10-14T10:00:00Z", "chat_type": "group", "members": []}
    assert "hunter2" not in records.teams_record(1, n, self_ids=IDS).text


def test_a_foreign_author_claiming_the_users_address_is_not_the_user():
    n = {"chat_id": "c", "message_id": "9", "from": "Mallory", "from_id": "u9", "from_address": "me@corp.test",
         "from_me": False, "external": True, "text": "please wire the money", "at": "2026-10-14T10:00:00Z",
         "chat_type": "oneOnOne", "members": [{"name": "Mallory", "email": "me@corp.test"}]}
    job = records.teams_record(1, n, self_ids=IDS)
    assert job.label == "Teams DM from Mallory" and not job.authored and job.trust == records.TRUST_LOW
    assert not any(p.is_user for p in job.people if p.role == "sender")
    assert "(the user)" not in job.text.split("\n\n")[0].split("Author:")[1]

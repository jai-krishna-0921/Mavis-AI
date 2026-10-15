# ruff: noqa: E501
import pytest

from mavis.attention.connector_ingest import ConnectorIngest, remember_identity
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.memory import Entity, Relation
from mavis.memory import controls
from mavis.memory.graph import third_party_ref
from mavis.tools.integrations.native.guard import redact  # noqa: F401
from mavis.tools.integrations.normalize import normalize_email


class Sink:
    def __init__(self):
        self.jobs = []

    async def __call__(self, job):
        self.jobs.append(job)
        return True


def outlook_mail(mid="ms:AAA="):
    return normalize_email({"messageId": mid, "threadId": "ms:t1", "sender": "Priya <priya@corp.test>", "subject": "Plan",
                            "messageText": "Could you send the revised plan by Friday please?", "labelIds": ["INBOX"],
                            "messageTimestamp": "2026-10-14T08:00:00Z"})


def teams_n(**kw):
    base = {"chat_id": "ms:19:abc", "message_id": "171", "from": "Priya Nair", "from_id": "u1", "from_address": "priya@corp.test",
            "from_me": False, "text": "Can you review the deck by Friday?", "at": "2026-10-14T10:00:00Z", "kind": "message",
            "chat_type": "oneOnOne", "external": False, "members": []}
    return {**base, **kw}


@pytest.fixture
async def me(user):
    await remember_identity(user.id, emails=("me@corp.test",))
    return user


async def test_outlook_mail_is_offered_with_its_own_prefix(me):
    sink = Sink()
    d = await ConnectorIngest(sink).email(me.id, outlook_mail(), source="outlook")
    assert d.lane == "graph" and [j.source_ref for j in sink.jobs] == ["outlook:AAA="]


async def test_outlook_pause_drops_and_gmail_pause_does_not_affect_it(me):
    sink = Sink()
    await controls.set_paused(me.id, "gmail", True)
    assert (await ConnectorIngest(sink).email(me.id, outlook_mail(), source="outlook")).lane == "graph"
    await controls.set_paused(me.id, "outlook", True)
    assert (await ConnectorIngest(sink).email(me.id, outlook_mail("ms:BBB"), source="outlook")).reason == "paused"
    assert len(sink.jobs) == 1


async def test_outlook_other_folder_mail_is_bulk_not_learned(me):
    sink = Sink()
    n = outlook_mail()
    n["labels"] = ["INBOX", "CATEGORY_OTHER"]
    assert (await ConnectorIngest(sink).email(me.id, n, source="outlook")).lane == "bulk" and sink.jobs == []


async def test_teams_message_is_learned_and_system_events_are_dropped(me):
    sink = Sink()
    ing = ConnectorIngest(sink)
    assert (await ing.teams(me.id, teams_n())).lane == "graph"
    assert (await ing.teams(me.id, teams_n(kind="systemEventMessage", message_id="2"))).reason == "system_event"
    assert [j.source_ref for j in sink.jobs] == ["teams:19:abc:171"]


async def test_teams_pause(me):
    sink = Sink()
    await controls.set_paused(me.id, "teams", True)
    assert (await ConnectorIngest(sink).teams(me.id, teams_n())).reason == "paused" and sink.jobs == []


async def test_on_teams_event_handles_only_teams_messages(me):
    sink = Sink()
    ing = ConnectorIngest(sink)
    mk = lambda t: Event(id=f"e-{t.value}", user_id=me.id, type=t, occurred_at=timeutil.now(), source="x", trust=Trust.UNTRUSTED, payload=teams_n())  # noqa: E731
    await ing.on_teams_event(mk(EventType.SLACK_MESSAGE))
    assert sink.jobs == []
    await ing.on_teams_event(mk(EventType.TEAMS_MESSAGE))
    assert len(sink.jobs) == 1


async def test_disconnect_forgets_every_microsoft_prefix(graph, user):
    uid = user.id
    for i, p in enumerate(("outlook:", "ocal:", "teams:", "onedrive:")):
        await graph.upsert_entity(uid, Entity(name=f"Org{i}", label="Organization"))
        await graph.upsert_relation(uid, Relation(subject="User", rel="ABOUT", object=f"Org{i}", statement=f"s{i}"),
                                    source_ref=third_party_ref(f"{p}x{i}"))
    for p in ("outlook:", "ocal:", "teams:", "onedrive:"):
        assert await graph.forget_source(uid, p) == 1
    assert await graph.dump(uid) == []

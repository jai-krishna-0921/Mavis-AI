"""Two-user isolation (spec 6.1): seed users A and B with distinctive data, read everything as B."""

from __future__ import annotations

import pytest

from mavis.domain import redis_keys
from mavis.domain.memory import Extraction
from mavis.domain.messages import Role
from mavis.store import artifacts
from mavis.store.repo import messages, users

MARKERS = {"A": "zebra-quartz-41", "B": "otter-lilac-77"}


@pytest.fixture
async def two(db, memory, fake_llm):
    a, _ = await users.get_or_create_by_chat(5001, "Priya")
    b, _ = await users.get_or_create_by_chat(7302, "Tomas")
    for u, key in ((a, "A"), (b, "B")):
        await messages.log(u.id, Role.USER, f"my code word is {MARKERS[key]}", event_id=f"iso:{u.id}")
        fake_llm.push_structured(Extraction())
        await memory.learn(u.id, f"I keep the spare key under the {MARKERS[key]} pot",
                           source_ref=f"iso:{u.id}")
    return a, b


async def test_repo_reads_and_recall_never_return_the_other_user(two, memory):
    a, b = two
    rows = await messages.recent(b.id, 50)
    assert all(MARKERS["A"] not in r.content for r in rows)
    ctx = await memory.recall(b.id, "where is the spare key")
    assert MARKERS["A"] not in ctx.model_dump_json()


@pytest.mark.parametrize("uid", [3, 41, 907])
def test_artifact_guard_rejects_other_prefixes(settings, uid):
    mine = artifacts.user_dir(uid, 12) / "chart.png"
    assert artifacts.guard(uid, mine) == mine.resolve()
    for bad in (artifacts.user_dir(uid + 1) / "x.pdf", settings.artifacts_dir / "x.pdf",
                artifacts.user_dir(uid) / ".." / f"u{uid + 1}" / "y.csv"):
        with pytest.raises(PermissionError):
            artifacts.guard(uid, bad)


@pytest.mark.parametrize("uid,area", [(1, "mbox"), (22, "lease"), (333, "spend")])
def test_user_keys_match_the_deletion_pattern(uid, area):
    import fnmatch

    key = redis_keys.user_key(area, uid, "chat")
    assert fnmatch.fnmatch(key, redis_keys.user_pattern(uid))
    assert not fnmatch.fnmatch(key, redis_keys.user_pattern(uid + 1))


def test_neighborhood_query_constrains_every_node():
    from mavis.memory.neo4j_graph import q_neighborhood

    for hops in (1, 2, 3):
        assert "all(n IN nodes(p) WHERE n.user_id = $u)" in q_neighborhood(hops)


async def test_workspace_guard_survives_a_second_process(fake_redis):
    from mavis.tools.integrations import workspace_guard as wg

    await wg.record_created(77, ["doc-1", "sheet-2"])
    wg._created.clear()  # a different worker process has no memory of it
    assert await wg.created_by(77) == {"doc-1", "sheet-2"}


async def test_outbox_never_sends_another_users_file(db, channel, settings):
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.domain.messages import Outbound
    from mavis.store.repo import outbox

    a, _ = await users.get_or_create_by_chat(5101, "Priya")
    b, _ = await users.get_or_create_by_chat(7102, "Tomas")
    artifacts.user_dir(a.id).mkdir(parents=True, exist_ok=True)
    secret = artifacts.user_dir(a.id) / "taxes.pdf"
    secret.write_bytes(b"pdf")
    mine = artifacts.user_dir(b.id)
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "ok.pdf").write_bytes(b"pdf")
    await outbox.enqueue_now(Outbound(user_id=b.id, text="x", document_path=str(secret), dedupe_key="leak"))
    await outbox.enqueue_now(Outbound(user_id=b.id, text="ok", document_path=str(mine / "ok.pdf"),
                                      dedupe_key="fine"))
    await OutboxSender(channel).run_once()
    await OutboxSender(channel).run_once()  # the refused row is failed; the next one is released
    assert [m.path for m in channel.sent if m.kind == "document"] == [str(mine / "ok.pdf")]


async def test_drive_upload_refuses_a_file_in_another_users_directory(db, settings):
    from mavis.tools.integrations.workspace_tools import _artifact_file

    a, _ = await users.get_or_create_by_chat(5201, "Priya")
    b, _ = await users.get_or_create_by_chat(7202, "Tomas")
    artifacts.user_dir(a.id).mkdir(parents=True, exist_ok=True)
    f = artifacts.user_dir(a.id) / "deck.pptx"
    f.write_bytes(b"PK")
    assert _artifact_file(str(f), a.id)[1] == 2
    assert _artifact_file(str(f), b.id)[1] is None


async def test_workspace_guard_without_redis_keeps_the_local_copy(db):
    from mavis.tools.integrations import workspace_guard as wg

    wg._created.clear()
    await wg.record_created(5, ["f1"])
    await wg.record_created(None, ["ignored"])
    assert await wg.created_by(5) == {"f1"} and await wg.created_by(6) == set()

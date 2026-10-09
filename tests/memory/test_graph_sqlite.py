from sqlalchemy import select

from mavis.domain.memory import Entity, Relation
from mavis.store import db as dbm
from mavis.store.models import GraphEdge


def rel(s, r, o, st, conf=0.8):
    return Relation(subject=s, rel=r, object=o, statement=st, confidence=conf)


async def seed(graph):
    await graph.upsert_entity(1, Entity(name="Jawahar", label="Person", aliases=["Jawa"]))
    await graph.upsert_entity(1, Entity(name="Siemens", label="Organization"))
    await graph.upsert_entity(1, Entity(name="Pune", label="Place"))
    await graph.upsert_relation(1, rel("User", "FRIEND_OF", "Jawahar", "Jawahar is the user's friend."))
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Siemens", "Jawahar works at Siemens."))
    await graph.upsert_relation(1, rel("Siemens", "LOCATED_IN", "Pune", "Siemens office is in Pune."))


async def test_upsert_entity_returns_key_and_merges_aliases(graph):
    k1 = await graph.upsert_entity(1, Entity(name="Jawahar", label="person", aliases=["Jawa"]))
    k2 = await graph.upsert_entity(1, Entity(name="jawahar", label="Person", aliases=["JR"]))
    assert k1 == k2 == "Person:jawahar"
    [e] = await graph.entities(1)
    assert e.name == "Jawahar" and set(e.aliases) == {"Jawa", "JR"}


async def test_user_entity_is_special(graph):
    assert await graph.upsert_entity(1, Entity(name="me", label="Person")) == "User:user"
    assert await graph.entities(1) == []  # the User node is not listed as an entity


async def test_neighborhood_hops(graph):
    await seed(graph)
    two = await graph.neighborhood(1, ["Jawa"], hops=2)
    assert set(two) == {
        "Jawahar is the user's friend.", "Jawahar works at Siemens.", "Siemens office is in Pune.",
    }
    one = await graph.neighborhood(1, ["Jawahar"], hops=1)
    assert "Siemens office is in Pune." not in one
    assert await graph.neighborhood(1, ["Nobody"]) == []


async def test_relation_upsert_is_idempotent(graph):
    await seed(graph)
    await graph.upsert_relation(1, rel("User", "friend of", "Jawahar", "Jawahar is a close friend.", 0.95))
    dump = await graph.dump(1)
    friend = [d for d in dump if d["relation"] == "FRIEND_OF"]
    assert len(friend) == 1 and friend[0]["statement"] == "Jawahar is a close friend."


async def test_single_valued_relation_closes_previous(graph):
    await seed(graph)
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Bosch", "Jawahar now works at Bosch."))
    current = {d["object"] for d in await graph.dump(1) if d["relation"] == "WORKS_AT"}
    assert current == {"Bosch"}
    async with dbm.Session() as s:
        rows = list(await s.scalars(select(GraphEdge).where(GraphEdge.rel == "WORKS_AT")))
    assert len(rows) == 2 and sum(r.valid_to is not None for r in rows) == 1


async def test_unknown_relation_type_falls_back(graph):
    await graph.upsert_relation(1, rel("User", "ADORES", "Biryani", "The user adores biryani."))
    [d] = await graph.dump(1)
    assert d["relation"] == "RELATED_TO" and d["object"] == "Biryani"


async def test_users_are_isolated(graph):
    await seed(graph)
    assert await graph.neighborhood(2, ["Jawahar"]) == []
    assert await graph.dump(2) == []


async def test_forget_deletes_edges_and_nodes(graph):
    await seed(graph)
    removed = await graph.forget(1, "jawahar")
    assert removed >= 2
    assert all("Jawahar" not in d["statement"] for d in await graph.dump(1))
    assert "Jawahar" not in {e.name for e in await graph.entities(1)}


async def test_merge_entities_repoints_edges(graph):
    await seed(graph)
    await graph.upsert_entity(1, Entity(name="Jawahar R", label="Person"))
    await graph.upsert_relation(1, rel("Jawahar R", "SKILLED_AT", "Python", "Jawahar R is good at Python."))
    await graph.merge_entities(1, keep="Jawahar", drop="Jawahar R", label="Person")
    names = {e.name: e for e in await graph.entities(1)}
    assert "Jawahar R" not in names and "Jawahar R" in names["Jawahar"].aliases
    assert any(d["subject"] == "Jawahar" and d["relation"] == "SKILLED_AT" for d in await graph.dump(1))


async def test_ranking_prefers_confident_older_edge(graph):
    from datetime import timedelta

    from mavis.memory.graph import edge_score
    from mavis.store.db import utcnow

    now = utcnow()
    assert edge_score(0.9, now - timedelta(days=5), now) > edge_score(0.2, now, now)
    await graph.upsert_relation(1, rel("User", "PREFERS", "Tea", "User prefers tea.", 0.95))
    await graph.upsert_relation(1, rel("User", "DISLIKES", "Rain", "User dislikes rain.", 0.1))
    async with dbm.Session() as s:
        for e in await s.scalars(select(GraphEdge).where(GraphEdge.rel == "PREFERS")):
            e.valid_from = now - timedelta(days=5)
        await s.commit()
    assert (await graph.neighborhood(1, ["User"], limit=1)) == ["User prefers tea."]


async def test_forget_escapes_like_wildcards(graph):
    await graph.upsert_relation(1, rel("User", "PREFERS", "Tea", "User prefers tea."))
    await graph.upsert_relation(1, rel("User", "DISLIKES", "Rain", "User dislikes 100% rain."))
    assert await graph.forget(1, "%") == 1
    assert [d["relation"] for d in await graph.dump(1)] == ["PREFERS"]


# --- a LEARN deferred by a busy model applies its facts with their SOURCE time ---------------------------
from datetime import UTC, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


async def _rows(rel_name):
    async with dbm.Session() as s:
        rows = list(await s.scalars(select(GraphEdge).where(GraphEdge.rel == rel_name)
                                    .order_by(GraphEdge.valid_from)))
    return rows


@pytest.mark.parametrize("gap_days", [1, 30])
@pytest.mark.parametrize("rel_name", ["LOCATED_IN", "WORKS_AT", "STUDIES_AT"])
async def test_late_older_fact_is_history_not_current(graph, rel_name, gap_days):
    """turn 1 'I live in Pune' deferred, turn 2 'moved to Delhi' applied, turn 1 retried later."""
    older, newer = T0, T0 + timedelta(days=gap_days)
    await graph.upsert_relation(1, rel("User", rel_name, "Delhi", "Moved to Delhi."), at=newer)
    await graph.upsert_relation(1, rel("User", rel_name, "Pune", "Lives in Pune."), at=older)
    assert {d["object"] for d in await graph.dump(1) if d["relation"] == rel_name} == {"Delhi"}
    pune, delhi = await _rows(rel_name)
    assert pune.dst_key.endswith("pune") or "Pune" in pune.statement
    assert pune.valid_to is not None and pune.valid_to.replace(tzinfo=UTC) == newer  # closed interval
    assert delhi.valid_to is None


async def test_in_order_facts_supersede_at_the_source_time(graph):
    await graph.upsert_relation(1, rel("User", "LOCATED_IN", "Pune", "Lives in Pune."), at=T0)
    await graph.upsert_relation(1, rel("User", "LOCATED_IN", "Delhi", "Moved to Delhi."),
                                at=T0 + timedelta(days=3))
    pune, delhi = await _rows("LOCATED_IN")
    assert pune.valid_to.replace(tzinfo=UTC) == T0 + timedelta(days=3)
    assert delhi.valid_from.replace(tzinfo=UTC) == T0 + timedelta(days=3) and delhi.valid_to is None


async def test_retrying_the_same_older_fact_twice_is_idempotent(graph):
    await graph.upsert_relation(1, rel("User", "LOCATED_IN", "Delhi", "Moved to Delhi."),
                                at=T0 + timedelta(days=2))
    for _ in range(2):
        await graph.upsert_relation(1, rel("User", "LOCATED_IN", "Pune", "Lives in Pune."), at=T0)
    assert len(await _rows("LOCATED_IN")) == 2 + 1 - 1  # Delhi current, Pune history, no repeat rows
    assert {d["object"] for d in await graph.dump(1)} >= {"Delhi"}
    assert [r.valid_to is None for r in await _rows("LOCATED_IN")].count(True) == 1


async def test_a_third_party_write_never_overwrites_a_user_edge(graph):
    await graph.upsert_entity(1, Entity(name="Meera", label="Person"))
    await graph.upsert_relation(1, rel("User", "FRIEND_OF", "Meera", "Meera is the user's friend."), "turn:9")
    await graph.upsert_relation(1, rel("User", "FRIEND_OF", "Meera", "Meera is a vendor contact."), "tp:gmail:m1")
    [edge] = [d for d in await graph.dump(1) if d["object"] == "Meera"]
    assert edge["statement"] == "Meera is the user's friend." and edge["source_ref"] == "turn:9"


async def test_a_third_party_single_valued_fact_never_ends_a_user_fact(graph):
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Siemens", "Jawahar works at Siemens."), "turn:3")
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Acme", "Jawahar joined Acme."), "tp:gmail:m2")
    dump = await graph.dump(1)
    assert [d["statement"] for d in dump if d["relation"] == "WORKS_AT"] == ["Jawahar works at Siemens."]


async def test_third_party_edges_still_update_each_other_and_the_user_may_overwrite_them(graph):
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Siemens", "Jawahar works at Siemens."), "tp:gmail:m1")
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Acme", "Jawahar joined Acme."), "tp:gmail:m2")
    assert [d["object"] for d in await graph.dump(1) if d["relation"] == "WORKS_AT"] == ["Acme"]
    await graph.upsert_relation(1, rel("Jawahar", "WORKS_AT", "Acme", "Jawahar works at Acme, he told me."), "turn:5")
    [edge] = [d for d in await graph.dump(1) if d["relation"] == "WORKS_AT"]
    assert edge["source_ref"] == "turn:5"

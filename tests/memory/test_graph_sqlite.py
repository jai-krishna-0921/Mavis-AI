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

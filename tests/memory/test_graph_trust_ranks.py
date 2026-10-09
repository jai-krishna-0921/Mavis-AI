# ruff: noqa: E501
"""Graph trust: the user's words beat their own records, which beat third-party records; the new forget
operations are exact and per user."""

from __future__ import annotations

import pytest

from mavis.domain.memory import Entity, Relation
from mavis.memory.graph import (
    SELF_AUTHORED_PREFIX,
    Fact,
    is_derived,
    is_self_authored,
    is_third_party,
    self_authored_ref,
    source_rank,
    third_party_ref,
)


def test_source_namespaces_and_ranks():
    assert source_rank("chat:1") == 2 and source_rank("") == 2 and source_rank("vault:correction:identity:x") == 2
    assert source_rank(self_authored_ref("gmail:a")) == 1 and source_rank(third_party_ref("gmail:a")) == 0
    assert is_self_authored("sa:gmail:a") and not is_third_party("sa:gmail:a") and is_derived("sa:gmail:a") and is_derived("tp:x") and not is_derived("chat")
    f = Fact("x", "sa:gmail:abc")
    assert f.self_authored and f.origin == "gmail:abc" and not f.third_party and SELF_AUTHORED_PREFIX == "sa:"


async def put(graph, uid, subject, rel, obj, statement, ref):
    await graph.upsert_entity(uid, Entity(name=obj, label="Organization"))
    await graph.upsert_relation(uid, Relation(subject=subject, rel=rel, object=obj, statement=statement), source_ref=ref)


async def statements(graph, uid):
    return {d["statement"]: d["source_ref"] for d in await graph.dump(uid)}


async def test_lower_sources_never_rewrite_higher_ones(graph, user):
    uid = user.id
    await put(graph, uid, "User", "WORKS_AT", "Northwind", "You work at Northwind.", "chat:1")
    await put(graph, uid, "User", "WORKS_AT", "Northwind", "A mail says Northwind.", self_authored_ref("gmail:1"))
    await put(graph, uid, "User", "WORKS_AT", "Northwind", "A stranger says Northwind.", third_party_ref("gmail:2"))
    assert await statements(graph, uid) == {"You work at Northwind.": "chat:1"}
    await put(graph, uid, "User", "WORKS_AT", "Zeus", "A mail says Zeus.", self_authored_ref("gmail:3"))  # single-valued: the user's edge stays
    assert list(await statements(graph, uid)) == ["You work at Northwind."]


async def test_own_records_beat_third_party_and_the_user_beats_both(graph, user):
    uid = user.id
    await put(graph, uid, "User", "WORKS_AT", "Acme", "A stranger says Acme.", third_party_ref("gmail:1"))
    await put(graph, uid, "User", "WORKS_AT", "Beta", "Your mail says Beta.", self_authored_ref("gmail:2"))
    assert await statements(graph, uid) == {"Your mail says Beta.": "sa:gmail:2"}
    await put(graph, uid, "User", "WORKS_AT", "Acme", "A stranger insists on Acme.", third_party_ref("gmail:3"))
    assert list(await statements(graph, uid)) == ["Your mail says Beta."]
    await put(graph, uid, "User", "WORKS_AT", "Gamma", "You told me Gamma.", "chat:5")
    assert await statements(graph, uid) == {"You told me Gamma.": "chat:5"}


async def test_forget_source_removes_both_namespaces_and_only_the_prefix(graph, user):
    uid = user.id
    await put(graph, uid, "User", "ABOUT", "Deck", "tp deck", third_party_ref("gmail:1"))
    await put(graph, uid, "User", "ABOUT", "Plan", "sa plan", self_authored_ref("gmail:2"))
    await put(graph, uid, "User", "ABOUT", "Roadmap", "slack roadmap", self_authored_ref("slack:T1:C1:1"))
    await put(graph, uid, "User", "ABOUT", "Mine", "user fact", "chat:1")
    assert await graph.forget_source(uid, "gmail:") == 2
    assert set(await statements(graph, uid)) == {"slack roadmap", "user fact"}
    assert {e.name for e in await graph.entities(uid)} == {"Roadmap", "Mine"}  # orphans went with their edges


async def test_forget_fact_is_exact_and_drops_orphans(graph, user):
    uid = user.id
    await put(graph, uid, "User", "ABOUT", "Deck", "deck", "chat:1")
    await put(graph, uid, "User", "PURSUING", "Deck", "pursuing deck", "chat:2")
    assert await graph.forget_fact(uid, "User", "ABOUT", "Deck") == 1
    assert set(await statements(graph, uid)) == {"pursuing deck"}
    assert await graph.forget_fact(uid, "User", "ABOUT", "Nothing") == 0
    assert await graph.forget_fact(uid, "User", "PURSUING", "Deck") == 1
    assert await graph.entities(uid) == []


async def test_forget_entity_by_name_or_alias_never_the_user(graph, user):
    uid = user.id
    await graph.upsert_entity(uid, Entity(name="Meera Iyer", label="Person", aliases=["meera@vendorco.in"]))
    await graph.upsert_relation(uid, Relation(subject="Meera Iyer", rel="WORKS_AT", object="Vendorco", statement="Meera works at Vendorco."), source_ref="tp:gmail:1")
    await graph.upsert_relation(uid, Relation(subject="User", rel="KNOWS", object="Meera Iyer", statement="You know Meera."), source_ref="chat:1")
    assert await graph.forget_entity(uid, "User") == 0
    assert await graph.forget_entity(uid, "meera@vendorco.in") == 2
    assert await graph.dump(uid) == [] and await graph.entities(uid) == []
    assert await graph.forget_entity(uid, "Nobody") == 0


@pytest.mark.parametrize("ref", ["", "chat:1"])
def test_user_statements_rank_highest(ref):
    assert source_rank(ref) == 2

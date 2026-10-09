"""Neo4j parity for the trust ranks and the exact forget operations, against a recording fake driver."""

from mavis.domain.memory import Relation
from mavis.memory import neo4j_graph as q
from tests.memory.test_graph_neo4j_queries import FakeDriver

REL = Relation(subject="User", rel="WORKS_AT", object="Siemens", statement="s")


def store(responder):
    drv = FakeDriver(responder)
    return q.Neo4jGraphStore("x", "u", "p", driver=drv), drv


def finder(query, p):
    return [{"key": "k:" + p["norm"]}] if query == q.Q_FIND_KEY else None


async def test_forget_source_covers_third_party_and_self_authored_prefixes():
    st, drv = store(lambda query, p: [{"c": 2}] if query == q.Q_FORGET_SOURCE else [])
    assert await st.forget_source(1, "gmail:") == 4
    prefixes = [c[1]["p"] for c in drv.calls if c[0] == q.Q_FORGET_SOURCE]
    assert prefixes == ["tp:gmail:", "sa:gmail:"] and drv.calls[-1][0] == q.Q_DROP_ORPHANS


async def test_forget_fact_targets_one_triple_and_cleans_orphans():
    def respond(query, p):
        if query == q.Q_FIND_KEY:
            return [{"key": "k:" + p["norm"]}]
        return [{"c": 1}] if query == q.Q_FORGET_FACT else []

    st, drv = store(respond)
    assert await st.forget_fact(1, "User", "works at", "Siemens") == 1
    call = next(c for c in drv.calls if c[0] == q.Q_FORGET_FACT)
    assert call[1] == {"u": 1, "src": "User:user", "dst": "k:siemens", "rel": "WORKS_AT"}
    assert drv.calls[-1][0] == q.Q_DROP_ORPHANS
    missing, drv2 = store(lambda query, p: [])
    assert await missing.forget_fact(1, "User", "WORKS_AT", "Nobody") == 0
    assert not [c for c in drv2.calls if c[0] == q.Q_FORGET_FACT]


async def test_forget_entity_never_the_user_and_needs_a_match():
    st, drv = store(lambda query, p: [])
    assert await st.forget_entity(1, "me") == 0 and drv.calls == []
    assert await st.forget_entity(1, "Nobody") == 0
    assert not [c for c in drv.calls if c[0] == q.Q_FORGET_ENTITY]


def writes(drv):
    creates = lambda query: "CREATE (" in query and "ON CREATE" not in query  # noqa: E731
    return [c for c in drv.calls if "SET r.statement" in c[0] or creates(c[0])]


async def test_a_self_authored_edge_does_not_overwrite_a_user_edge():
    def respond(query, p):
        if query == q.Q_FIND_KEY:
            return [{"key": "k:" + p["norm"]}]
        if "RETURN b.key AS dst" in query:
            return [{"dst": "k:siemens", "src": "chat:1"}]  # the user's own edge
        return []

    st, drv = store(respond)
    await st.upsert_relation(1, REL, source_ref="sa:gmail:1")
    assert not writes(drv)
    await st.upsert_relation(1, REL, source_ref="tp:gmail:1")
    assert not writes(drv)

import pytest

from zento.memory import neo4j_graph as q


def test_upsert_entity_uses_sanitised_label():
    query = q.q_upsert_entity("person")
    assert "SET n:Person" in query
    assert "$key" in query and "$aliases" in query
    assert "MERGE (n:Entity {user_id:$u, key:$key})" in query


def test_injection_in_label_is_neutralised():
    query = q.q_upsert_entity("Person) DETACH DELETE n //")
    assert "DETACH DELETE" not in query
    assert "SET n:Topic" in query


def test_relation_builders_sanitise_type():
    assert "[r:FRIEND_OF]" in q.q_update_current_edge("friend of")
    assert "[r:RELATED_TO" in q.q_create_edge("FRIEND_OF]->() DETACH DELETE n //")
    assert "DETACH DELETE" not in q.q_create_edge("FRIEND_OF]->() DETACH DELETE n //")


def test_close_single_valued_only_touches_current_other_targets():
    query = q.q_close_single_valued("WORKS_AT")
    assert "[r:WORKS_AT]" in query
    assert "r.valid_to IS NULL" in query and "b.key <> $dst" in query
    assert "SET r.valid_to = datetime()" in query


def test_create_edge_sets_valid_from_and_params_only():
    query = q.q_create_edge("WORKS_AT")
    assert "valid_from:datetime()" in query
    for p in ("$statement", "$confidence", "$source_ref", "$src", "$dst", "$u"):
        assert p in query


@pytest.mark.parametrize("hops,expected", [(0, "*1..1"), (2, "*1..2"), (9, "*1..3")])
def test_neighborhood_hops_are_clamped(hops, expected):
    query = q.q_neighborhood(hops)
    assert expected in query
    assert "r.valid_to IS NULL" in query
    assert "LIMIT $limit" in query


def test_store_constructs_without_connecting():
    class Dummy:
        pass

    store = q.Neo4jGraphStore("bolt://x", "neo4j", "pw", driver=Dummy())
    assert store is not None


# --- parity helpers and flow tests with a recording fake driver ---------------------------


class _Rec:
    def __init__(self, data):
        self._d = data

    def data(self):
        return self._d


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield _Rec(r)

        return gen()


class FakeDriver:
    def __init__(self, responder):
        self.calls: list[tuple[str, dict]] = []
        self._responder = responder

    def session(self):
        drv = self

        class S:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def run(self_, query, **params):
                drv.calls.append((query, params))
                return _Result(drv._responder(query, params))

        return S()


def test_edge_score_and_rank_candidates():
    from datetime import UTC, datetime, timedelta

    from zento.memory.graph import edge_score

    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert edge_score(1.0, now, now) == 1.0
    assert edge_score(1.0, now - timedelta(days=30), now) == pytest.approx(0.5)
    rows = [
        {"st": "new-low", "conf": 0.2, "vf": now},
        {"st": "old-high", "conf": 1.0, "vf": now - timedelta(days=10)},
    ]
    assert q.rank_candidates(rows, 1, now) == ["old-high"]


def test_plan_dedupe_drops_self_loops_and_duplicates_keeping_newest():
    edges = [
        {"id": "1", "src": "a", "rel": "R", "dst": "b"},
        {"id": "2", "src": "a", "rel": "R", "dst": "b"},
        {"id": "3", "src": "a", "rel": "R", "dst": "a"},
        {"id": "4", "src": "a", "rel": "S", "dst": "b"},
    ]
    assert q.plan_dedupe(edges) == ["2", "3"]


def test_cypher_clauses_for_parity():
    assert "coalesce(r.source_ref" in q.q_update_current_edge("WORKS_AT")
    assert "r.confidence AS conf" in q.q_neighborhood(2) and "r.valid_from AS vf" in q.q_neighborhood(2)
    assert "elementId(r) IN $ids" in q.Q_DELETE_EDGES


async def test_merge_is_noop_when_keep_missing():
    drv = FakeDriver(lambda query, p: [])
    await q.Neo4jGraphStore("x", "u", "p", driver=drv).merge_entities(1, "Jawahar", "Jawa R", "Person")
    assert [c[0] for c in drv.calls] == [q.Q_KEY_EXISTS]
    assert drv.calls[0][1] == {"u": 1, "key": "Person:jawahar"}


async def test_merge_repoints_then_dedupes():
    def respond(query, p):
        if query == q.Q_KEY_EXISTS:
            return [{"key": p["key"]}]
        if query == q.Q_DROP_EDGES:
            return [
                {
                    "t": "WORKS_AT", "outgoing": True,
                    "other": "Organization:siemens", "props": {"statement": "s"},
                }
            ]
        if query == q.Q_CURRENT_EDGES:
            return [
                {"id": "e1", "src": "Person:jawahar", "rel": "WORKS_AT", "dst": "Organization:siemens"},
                {"id": "e2", "src": "Person:jawahar", "rel": "WORKS_AT", "dst": "Organization:siemens"},
            ]
        return []

    drv = FakeDriver(respond)
    await q.Neo4jGraphStore("x", "u", "p", driver=drv).merge_entities(1, "Jawahar", "Jawa R", "Person")
    queries = [c[0] for c in drv.calls]
    assert queries.index(q.Q_DELETE_NODE) < queries.index(q.Q_CURRENT_EDGES) < queries.index(q.Q_DELETE_EDGES)
    assert drv.calls[-1][1] == {"u": 1, "ids": ["e2"]}


async def test_neighborhood_resolves_user_alias_and_ranks():
    def respond(query, p):
        if "RETURN r.statement AS st" in query:
            return [{"st": "a", "conf": 0.5, "vf": None}]
        return []

    drv = FakeDriver(respond)
    out = await q.Neo4jGraphStore("x", "u", "p", driver=drv).neighborhood(1, ["me"], limit=5)
    assert out == ["a"]
    nb = [c for c in drv.calls if "RETURN r.statement" in c[0]][0]
    assert nb[1] == {"u": 1, "keys": ["User:user"], "limit": 20}


async def test_single_valued_closed_only_when_creating_new_edge():
    from zento.domain.memory import Relation

    rel = Relation(subject="User", rel="WORKS_AT", object="Siemens", statement="s")

    def respond(query, p):
        if query == q.Q_FIND_KEY:
            return [{"key": "k:" + p["norm"]}]
        if "RETURN count(r) AS c" in query:
            return [{"c": updated}]
        return []

    for updated in (1, 0):
        drv = FakeDriver(respond)
        await q.Neo4jGraphStore("x", "u", "p", driver=drv).upsert_relation(1, rel, "src")
        closed = any("SET r.valid_to" in c[0] for c in drv.calls)
        assert closed == (updated == 0)

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

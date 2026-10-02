from mavis.domain.memory import Entity, Extraction, Relation
from mavis.memory.resolver import resolve
from tests.memory.fakes import HashEmbedder, TableEmbedder


def x(entities, relations=()):
    return Extraction(entities=entities, relations=list(relations))


async def test_alias_match_maps_to_canonical():
    existing = [Entity(name="Jawahar", label="Person", aliases=["Jawa"])]
    res = await resolve(
        x([Entity(name="jawa", label="Person")],
          [Relation(subject="me", rel="FRIEND_OF", object="jawa", statement="Jawa is my friend.")]),
        existing, HashEmbedder())
    assert res.mapping["jawa"] == "Jawahar"
    assert res.relations[0].subject == "User" and res.relations[0].object == "Jawahar"
    assert [e.name for e in res.entities] == ["Jawahar"]


async def test_embedding_similarity_same_label_merges_and_adds_alias():
    emb = TableEmbedder({"Jawahar R": [1.0, 0.1, 0.0, 0.0], "Jawahar": [1.0, 0.0, 0.0, 0.0]})
    res = await resolve(x([Entity(name="Jawahar R", label="Person")]),
                        [Entity(name="Jawahar", label="Person")], emb)
    assert res.mapping["Jawahar R"] == "Jawahar"
    assert "Jawahar R" in res.entities[0].aliases


async def test_similar_but_different_label_is_not_merged():
    emb = TableEmbedder({"Siemens": [1.0, 0.0, 0.0, 0.0], "Siemens Office": [1.0, 0.05, 0.0, 0.0]})
    res = await resolve(x([Entity(name="Siemens Office", label="Place")]),
                        [Entity(name="Siemens", label="Organization")], emb)
    assert res.mapping.get("Siemens Office", "Siemens Office") == "Siemens Office"
    assert res.entities[0].name == "Siemens Office"


async def test_new_entity_kept_and_relation_names_without_entities_still_canonicalised():
    existing = [Entity(name="Jawahar", label="Person")]
    res = await resolve(
        x([Entity(name="Teamcenter", label="Topic")],
          [Relation(subject="JAWAHAR", rel="SKILLED_AT", object="Teamcenter",
                    statement="J knows Teamcenter.")]),
        existing, HashEmbedder())
    assert [e.name for e in res.entities] == ["Teamcenter"]
    assert res.relations[0].subject == "Jawahar"


async def test_no_existing_entities_skips_embedding():
    class Never:
        dim = 4

        async def embed(self, texts):
            raise AssertionError("no embedding needed")

    res = await resolve(x([Entity(name="Jawahar", label="Person")]), [], Never())
    assert [e.name for e in res.entities] == ["Jawahar"]


async def test_same_name_different_label_exact_match_does_not_merge():
    res = await resolve(x([Entity(name="Siemens", label="Place")]),
                        [Entity(name="Siemens", label="Organization")], HashEmbedder())
    assert [(e.name, e.label) for e in res.entities] == [("Siemens", "Place")]

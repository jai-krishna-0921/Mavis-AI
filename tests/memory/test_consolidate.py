from tests.memory.fakes import TableEmbedder
from zento.domain.errors import LLMError
from zento.domain.memory import Entity, Relation
from zento.llm import models
from zento.memory.consolidate import ProfileDraft, consolidate, merge_duplicates
from zento.memory.profile import ProfileCard
from zento.store.repo import profile as profile_repo


async def test_merge_duplicates_same_label_only(graph):
    await graph.upsert_entity(1, Entity(name="Jawahar", label="Person"))
    await graph.upsert_entity(1, Entity(name="Jawahar R", label="Person"))
    await graph.upsert_entity(1, Entity(name="Jawahar Labs", label="Organization"))
    emb = TableEmbedder(
        {"Jawahar": [1, 0, 0, 0], "Jawahar R": [1, 0.05, 0, 0], "Jawahar Labs": [1, 0.02, 0, 0]}
    )
    merged = await merge_duplicates(1, graph, emb)
    assert merged == 1
    assert {e.name for e in await graph.entities(1)} == {"Jawahar", "Jawahar Labs"}


async def test_consolidate_rewrites_profile_preserving_flags(memory, user, fake_llm):
    await profile_repo.save(user.id, ProfileCard(timezone="Asia/Kolkata", flags={"track_mood": False}))
    await memory.graph.upsert_relation(user.id, Relation(subject="User", rel="PURSUING", object="Job hunt",
                                                         statement="Jai is looking for a job."))
    fake_llm.push_structured(ProfileDraft(name="Jai", tone="casual, swears a bit",
                                          goals=["Land a job"] + [f"g{i}" for i in range(10)]))
    result = await consolidate(user.id, memory)
    card = await profile_repo.get(user.id)
    assert result == {"merged": 0, "profile_rewritten": True}
    assert card.name == "Jai" and card.tone == "casual, swears a bit"
    assert len(card.goals) == 8
    assert card.flags == {"track_mood": False} and card.timezone == "Asia/Kolkata"


async def test_consolidate_skips_llm_without_facts(memory, user, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("no facts, no rewrite")

    monkeypatch.setattr(models, "structured", never)
    assert await consolidate(user.id, memory) == {"merged": 0, "profile_rewritten": False}


async def test_consolidate_tolerates_llm_error(memory, user, monkeypatch):
    await memory.graph.upsert_relation(user.id, Relation(subject="User", rel="KNOWS", object="Amma",
                                                         statement="Jai knows Amma."))

    async def boom(*a, **k):
        raise LLMError("down")

    monkeypatch.setattr(models, "structured", boom)
    assert (await consolidate(user.id, memory))["profile_rewritten"] is False

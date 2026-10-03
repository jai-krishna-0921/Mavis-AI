import pytest
from qdrant_client import AsyncQdrantClient

from mavis.attention.index import OBS, AttentionIndex, point_id
from tests.memory.fakes import HashEmbedder

TS = "2026-10-03T00:00:00+00:00"


@pytest.fixture
async def index():
    client = AsyncQdrantClient(location=":memory:")
    yield AttentionIndex(client, HashEmbedder())
    await client.close()


async def test_novelty_and_search_are_per_user(index):
    v = await index.embed("travel from visaoffice: appointment confirmed")
    assert await index.novelty(1, v) == 1.0
    pid = await index.add_observation(1, 10, "travel", v, TS)
    assert pid == point_id(1, OBS, 10)
    assert await index.novelty(1, v) == 0.0
    assert await index.search(1, "visaoffice appointment", min_score=0.3) == [10]
    assert await index.search(2, "visaoffice appointment", min_score=0.3) == []
    assert await index.novelty(2, v) == 1.0
    assert await index.search(1, "   ") == []


async def test_prefs_near_and_prefs_are_not_observations(index):
    v = await index.embed("newsletter from examplenews: weekly digest")
    await index.add_pref(1, 5, "mute", "newsletter", v)
    hits = await index.prefs_near(
        1, await index.embed("newsletter from examplenews: weekly digest issue 2"), 0.5
    )
    assert [(h.sentiment, h.kind) for h in hits] == [("mute", "newsletter")] and hits[0].score > 0.8
    assert await index.prefs_near(1, await index.embed("security from example: new sign-in"), 0.8) == []
    assert await index.novelty(1, v) == 1.0


async def test_delete(index):
    v = await index.embed("receipt or order from exampleshop: your order")
    pid = await index.add_observation(1, 3, "receipt_or_order", v, TS)
    await index.delete([pid])
    await index.delete([])
    assert await index.novelty(1, v) == 1.0

import pytest

from mavis.domain.memory import Entity
from mavis.memory import spotter as spotter_mod
from mavis.memory.spotter import EntitySpotter, SpotterCache

ENTS = [
    Entity(name="Jawahar", label="Person", aliases=["Jawa"]),
    Entity(name="Siemens", label="Organization"),
    Entity(name="Teamcenter", label="Topic"),
]


@pytest.fixture(params=[True, False], ids=["ahocorasick", "regex"])
def backend(request, monkeypatch):
    if request.param and not spotter_mod._HAS_AC:
        pytest.skip("pyahocorasick not installed")
    monkeypatch.setattr(spotter_mod, "_HAS_AC", request.param)


def test_word_boundaries_and_aliases(backend):
    sp = EntitySpotter(ENTS)
    assert sp.spot("Did Jawa reply? Siemens called about Teamcenter.") == ["Jawahar", "Siemens", "Teamcenter"]
    assert sp.spot("Jawaharlal Nehru was a PM") == []
    assert sp.spot("JAWAHAR!!") == ["Jawahar"]


def test_empty_spotter(backend):
    assert EntitySpotter([]).spot("anything") == []


async def test_cache_invalidation():
    class G:
        def __init__(self):
            self.calls = 0

        async def entities(self, user_id):
            self.calls += 1
            return ENTS

    g = G()
    cache = SpotterCache(g)
    await cache.get(1)
    await cache.get(1)
    assert g.calls == 1
    cache.invalidate(1)
    await cache.get(1)
    assert g.calls == 2

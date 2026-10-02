from datetime import UTC, datetime, timedelta

from zento.domain.loops import Loop, LoopKind
from zento.domain.memory import Entity
from zento.memory.recall import assemble, recall, render_loop
from zento.memory.spotter import SpotterCache
from zento.memory.tokens import estimate_tokens


def test_assemble_orders_and_budgets():
    facts = [f"fact {i} " + "x" * 100 for i in range(100)]
    ctx = assemble("Name: Jai", ["loop a"], facts, ["episode"], budget=300)
    assert ctx.profile == "Name: Jai"
    assert ctx.loops == ["loop a"]
    assert 0 < len(ctx.facts) < 100
    assert estimate_tokens(ctx.render()) <= 330  # headers add a little on top of the item budget


def test_assemble_dedupes_episodes_against_facts():
    ctx = assemble("", [], ["Jawahar is my friend."], ["Jawahar is my friend.", "Other"], budget=1200)
    assert ctx.episodes == ["Other"]


def test_render_loop_in_user_tz():
    loop = Loop(id=1, user_id=1, kind=LoopKind.COMMITMENT, title="Interview prep with Jawahar",
                due_at=datetime(2026, 10, 5, 4, 30, tzinfo=UTC))
    expected = "Interview prep with Jawahar (commitment, due Mon 05 Oct 10:00)"
    assert render_loop(loop, "Asia/Kolkata") == expected


class _Graph:
    def __init__(self, fail=False):
        self.fail = fail
        self.asked: list[list[str]] = []

    async def entities(self, user_id):
        return [Entity(name="Jawahar", label="Person", aliases=["Jawa"])]

    async def neighborhood(self, user_id, names, hops=2, limit=25):
        if self.fail:
            raise RuntimeError("neo4j down")
        self.asked.append(names)
        return ["Jawahar WORKS_AT Siemens"]


class _Vector:
    def __init__(self, fail=False):
        self.fail = fail

    async def search(self, user_id, query, k=6, min_score=0.35):
        if self.fail:
            raise RuntimeError("qdrant down")
        return ["Talked to Jawahar about the interview"]


class _Loops:
    def __init__(self):
        self.calls = []

    async def active(self, user_id, entities=None, due_within: timedelta | None = None):
        self.calls.append(entities)
        return [Loop(id=1, user_id=user_id, kind=LoopKind.COMMITMENT, title="Call Jawahar")]


async def _run(graph, vector, loops=None):
    return await recall(1, "Did Jawa reply?", profile="Name: Jai", tz="UTC", spotters=SpotterCache(graph),
                        graph=graph, vector=vector, loops=loops)


async def test_recall_assembles_all_sources():
    graph, loops = _Graph(), _Loops()
    ctx = await _run(graph, _Vector(), loops)
    assert graph.asked == [["Jawahar"]]
    assert loops.calls == [["Jawahar"]]
    assert ctx.facts == ["Jawahar WORKS_AT Siemens"]
    assert ctx.episodes == ["Talked to Jawahar about the interview"]
    assert ctx.loops == ["Call Jawahar (commitment)"]
    assert ctx.profile == "Name: Jai"


async def test_recall_survives_store_failure():
    ctx = await _run(_Graph(fail=True), _Vector())
    assert ctx.facts == [] and ctx.episodes == ["Talked to Jawahar about the interview"]
    ctx = await _run(_Graph(), _Vector(fail=True))
    assert ctx.episodes == [] and ctx.facts == ["Jawahar WORKS_AT Siemens"]
    ctx = await _run(_Graph(fail=True), _Vector(fail=True))
    assert ctx.profile == "Name: Jai" and ctx.facts == [] and ctx.episodes == []


async def test_recall_survives_spotter_and_loops_failure():
    class BadLoops:
        async def active(self, *a, **k):
            raise RuntimeError("db down")

    class BadEntities(_Graph):
        async def entities(self, user_id):
            raise RuntimeError("boom")

    ctx = await _run(_Graph(), _Vector(), BadLoops())
    assert ctx.loops == [] and ctx.facts
    ctx = await _run(BadEntities(), _Vector())
    assert ctx.facts == [] and ctx.episodes

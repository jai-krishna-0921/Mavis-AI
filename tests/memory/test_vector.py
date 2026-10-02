from mavis.memory.vector import QdrantVectorStore


async def test_add_and_search_returns_relevant_first(vector):
    await vector.add(1, ["Interview prep with Jawahar on Monday", "I love biryani"], kind="fact")
    hits = await vector.search(1, "interview prep with Jawahar", min_score=0.2)
    assert hits[0] == "Interview prep with Jawahar on Monday"
    assert "I love biryani" not in hits


async def test_search_is_scoped_to_user(vector):
    await vector.add(1, ["Jai is preparing for interviews"], kind="fact")
    await vector.add(2, ["Someone else is preparing for interviews"], kind="fact")
    hits = await vector.search(2, "preparing for interviews", min_score=0.1)
    assert hits == ["Someone else is preparing for interviews"]


async def test_add_is_idempotent_on_same_text(vector):
    await vector.add(1, ["Jawahar is my friend", "jawahar is my friend  "], kind="fact")
    await vector.add(1, ["Jawahar is my friend"], kind="fact")
    assert await vector.count(1) == 1


async def test_empty_inputs_are_noops(vector):
    await vector.add(1, ["", "   "], kind="fact")
    assert await vector.count(1) == 0
    assert await vector.search(1, "   ") == []


async def test_forget_removes_matching_points(vector):
    await vector.add(1, ["Jawahar is my friend", "Teamcenter came up in the interview"], kind="fact")
    removed = await vector.forget(1, "jawahar")
    assert removed == 1
    assert await vector.count(1) == 1
    assert await vector.search(1, "Jawahar friend", min_score=0.1) == []


def test_point_id_is_deterministic_and_normalised():
    a = QdrantVectorStore.point_id(1, "Jawahar is my friend")
    b = QdrantVectorStore.point_id(1, "  jawahar IS my friend ")
    c = QdrantVectorStore.point_id(2, "Jawahar is my friend")
    assert a == b
    assert a != c

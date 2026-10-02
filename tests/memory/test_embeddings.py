import pytest

from tests.memory.fakes import HashEmbedder
from zento.memory import embeddings
from zento.memory.embeddings import cosine, get_embedder, set_embedder
from zento.memory.tokens import estimate_tokens


def test_estimate_tokens_rounds_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_cosine_basic_and_zero_vector():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_set_embedder_overrides_default():
    fake = HashEmbedder()
    set_embedder(fake)
    try:
        assert get_embedder() is fake
    finally:
        set_embedder(None)
    assert isinstance(get_embedder(), embeddings.FastEmbedder)


def test_fast_embedder_is_lazy():
    e = embeddings.FastEmbedder("BAAI/bge-small-en-v1.5", "/tmp/zento-models-never-used")
    assert e._model is None  # constructing must not download anything


async def test_hash_embedder_similarity():
    e = HashEmbedder()
    a, b, c = await e.embed(["interview prep with Jawahar", "Jawahar interview prep", "biryani recipe"])
    assert cosine(a, b) > 0.8  # 3 shared of 4/3 tokens => 0.866
    assert cosine(a, c) < 0.3

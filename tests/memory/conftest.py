import pytest

from tests.memory.fakes import HashEmbedder
from zento.memory.embeddings import set_embedder
from zento.memory.graph import SqliteGraphStore
from zento.memory.vector import QdrantVectorStore


@pytest.fixture
def embedder():
    e = HashEmbedder()
    set_embedder(e)
    yield e
    set_embedder(None)


@pytest.fixture
async def vector(embedder):
    store = QdrantVectorStore(embedder, location=":memory:")
    await store.init()
    yield store
    await store.close()


@pytest.fixture
async def graph(db):
    g = SqliteGraphStore()
    await g.init()
    return g

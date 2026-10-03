from datetime import UTC, datetime, timedelta

import pytest
from qdrant_client import AsyncQdrantClient

from mavis.attention.digest import Digest, wants_inbox
from mavis.attention.index import AttentionIndex
from mavis.store.repo import attention as repo
from tests.memory.fakes import HashEmbedder

NOON = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)


@pytest.fixture
async def index():
    client = AsyncQdrantClient(location=":memory:")
    yield AttentionIndex(client, HashEmbedder())
    await client.close()


async def add(user_id: int, mid: str, *, at=NOON, done=True, **fields):
    obs, _ = await repo.insert_pending(
        user_id,
        mid,
        thread_id="",
        origin=repo.ORIGIN_LIVE,
        sender_domain="x.in",
        sender_name="",
        received_at=at,
        payload={},
    )
    if done:
        await repo.finish(obs.id, **fields)
    return obs


@pytest.mark.parametrize(
    "text,expected",
    [
        ("any Gmail updates?", True),
        ("anything from the visa office?", True),
        ("did I pay anyone big this week", True),
        ("check my inbox", True),
        ("what did I miss", True),
        ("how are you", False),
        ("I love this song", False),
    ],
)
def test_wants_inbox(text, expected):
    assert wants_inbox(text) is expected


async def test_context_is_empty_without_inbox_or_intent(user, clock, index):
    clock.set(NOON)
    digest = Digest(index)
    assert await digest.context(user.id, "any gmail updates?") == ""
    await add(user.id, "m1", verdict="log", summary="account update from x: hi")
    assert await digest.context(user.id, "how are you") == ""


async def test_context_renders_counts_lines_feedback_and_hits(user, clock, index):
    clock.set(NOON)
    await add(
        user.id,
        "ask",
        verdict="ask",
        kind="money_movement",
        feedback="disputed",
        summary="money movement from examplebank: debit alert",
        action="confirm the payment",
    )
    await add(user.id, "l1", verdict="log", summary="receipt or order from exampleshop: order shipped")
    await add(user.id, "l2", verdict="log", summary="account update from examplecloud: plan renews")
    await add(user.id, "d1", verdict="dropped", summary="newsletter from exampledeals: sale")
    await add(user.id, "p1", done=False)
    old = await add(
        user.id,
        "old",
        at=NOON - timedelta(days=3),
        verdict="log",
        kind="travel",
        summary="travel from visaoffice: appointment confirmed",
    )
    await index.add_observation(
        user.id,
        old.id,
        "travel",
        await index.embed("travel from visaoffice: appointment confirmed"),
        (NOON - timedelta(days=3)).isoformat(),
    )
    out = await Digest(index).context(user.id, "any email from visaoffice appointment")
    assert "Emails seen in the last 24h: 4. Needing attention: 1. Routine, handled quietly: 3." in out
    assert "Arrived but not read yet: 1." in out
    assert "they said it was NOT them" in out and "(asks: confirm the payment)" in out
    assert "visaoffice" in out and "<untrusted" in out
    assert out.index("<untrusted") > out.index("Emails seen")
    assert "Never read out links" in out

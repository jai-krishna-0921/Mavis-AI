import asyncio

import pytest

from mavis.agents import context_hooks, simple_turn


@pytest.fixture(autouse=True)
def _clean():
    context_hooks.clear_context_providers()
    yield
    context_hooks.clear_context_providers()


async def test_gather_skips_slow_and_failing_providers(monkeypatch):
    monkeypatch.setattr(context_hooks, "PROVIDER_TIMEOUT_S", 0.05)

    async def good(user_id, text):
        return f"## Inbox\nuser {user_id} asked: {text}"

    async def slow(user_id, text):
        await asyncio.sleep(1)
        return "too late"

    async def broken(user_id, text):
        raise RuntimeError("boom")

    async def empty(user_id, text):
        return ""

    for fn in (good, slow, broken, empty, good):
        context_hooks.register_context_provider(fn)
    assert len(context_hooks.CONTEXT_PROVIDERS) == 4
    assert await context_hooks.gather_context(7, "any updates?") == "## Inbox\nuser 7 asked: any updates?"


async def test_no_providers_is_empty():
    assert await context_hooks.gather_context(1, "hi") == ""


async def test_build_context_includes_providers(user, fake_memory):
    async def inbox(user_id, text):
        return "## What you've seen in their inbox\nnothing notable"

    context_hooks.register_context_provider(inbox)
    out = await simple_turn.build_context(user.id, "any gmail updates?")
    assert "## What you've seen in their inbox" in out

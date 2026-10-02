"""Test doubles shared across phases."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from tests.fakes.llm import FakeLLM, FakeToolChatModel

__all__ = ["FakeLLM", "FakeToolChatModel", "wait_until"]


async def wait_until(predicate: Callable[[], bool | Awaitable[bool]], timeout: float = 3.0) -> None:  # noqa: ASYNC109
    """Poll `predicate` (sync or async) until truthy, else fail the test after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.01)

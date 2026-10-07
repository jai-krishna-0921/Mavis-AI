"""Hook: what recently failed and needs the user's decision, for the reasoner and composer prompts.

Empty by default. The provider (failed/partial tasks and failed approvals, hotfix H1) is wired after merge
with `set_provider`. A failing provider never blocks a ping: it reads as nothing to add.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger()
HEADING = "Recently failed (needs your decision)"

Provider = Callable[[int], Awaitable[str]]
_provider: Provider | None = None


def set_provider(fn: Provider | None) -> None:
    global _provider
    _provider = fn


async def lines(user_id: int) -> str:
    if _provider is None:
        return ""
    try:
        return (await _provider(user_id)).strip()
    except Exception:  # noqa: BLE001 - grounding is additive; never block a ping or a decision on it
        log.exception("initiative.recent_failures_failed", user=user_id)
        return ""


async def section(user_id: int) -> str:
    """'## Recently failed ...' plus its lines, or '' when there is nothing."""
    found = await lines(user_id)
    return f"## {HEADING} (computed, trusted)\n{found}" if found else ""

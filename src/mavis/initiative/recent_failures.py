"""What recently failed and needs the user's decision, for the reasoner and composer prompts.

The default provider renders policy.outcomes.recently_failed (failed approvals, failed or partial background
tasks; hotfix H1) in the user's timezone. Items that came from third-party content are wrapped as untrusted
and the section says so, so callers carry that into their own trust decision. A failing provider never
blocks a ping or a decision: it reads as nothing to add.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.domain import timeutil
from mavis.policy import outcomes
from mavis.store.repo import users

log = structlog.get_logger()
HEADING = outcomes.HEADING

Provider = Callable[[int], Awaitable[tuple[str, bool]]]


async def _from_outcomes(user_id: int) -> tuple[str, bool]:
    """(lines, whether any of them is third-party text)."""
    items = await outcomes.recently_failed(user_id)
    if not items:
        return "", False
    tz = (await users.get(user_id)).timezone
    text, untrusted = outcomes.render_recently_failed(items, timeutil.now(), tz, source="initiative")
    return text.partition("\n")[2], untrusted  # the section adds its own heading


_provider: Provider = _from_outcomes


def set_provider(fn: Provider | None) -> None:
    """Swap the provider (tests); None restores the default."""
    global _provider
    _provider = fn or _from_outcomes


async def section(user_id: int) -> tuple[str, bool]:
    """('## Recently failed ...' plus its lines, untrusted), or ('', False) when there is nothing."""
    try:
        found, untrusted = await _provider(user_id)
    except Exception:  # noqa: BLE001 - grounding is additive; never block a ping or a decision on it
        log.exception("initiative.recent_failures_failed", user=user_id)
        return "", False
    found = found.strip()
    if not found:
        return "", False
    trust = "contains third-party text" if untrusted else "trusted"
    return f"## {HEADING} (computed, {trust})\n{found}", untrusted

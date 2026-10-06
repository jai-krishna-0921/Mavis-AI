"""Keep a rolling summary of conversation that has scrolled out of working memory."""

from __future__ import annotations

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from mavis.config import get_settings
from mavis.domain.timefmt import absolute_time
from mavis.llm import models as llm
from mavis.store.repo import summaries as summaries_repo
from mavis.store.repo import users

log = structlog.get_logger()
WINDOW = 20
BATCH = 20

_SYSTEM = (
    "You maintain a running summary of a chat between a user and their personal assistant. "
    "Merge the previous summary with the new messages into at most 120 words. Keep names, dates, "
    "commitments, feelings and open questions. Third person, past tense, no preamble. Each message starts "
    "with the time it was written. Write absolute dates (\"Sun 4 Oct\"), never relative words like today, "
    "tomorrow or tonight: resolve them against the time of the message they appear in."
)


async def _timezone(user_id: int) -> str:
    try:
        return (await users.get(user_id)).timezone or get_settings().default_timezone
    except Exception:  # noqa: BLE001 - a summary must not fail on a missing user row
        return get_settings().default_timezone


async def maybe_summarize(user_id: int) -> bool:
    previous = await summaries_repo.latest(user_id)
    pending = await summaries_repo.messages_outside_window(
        user_id, previous.upto_message_id if previous else 0, WINDOW
    )
    if len(pending) < BATCH:
        return False
    tz = await _timezone(user_id)
    transcript = "\n".join(f"[{absolute_time(m.created_at, tz)}] {m.role}: {m.content}" for m in pending)
    prompt = f"Previous summary:\n{previous.summary if previous else '(none)'}\n\nNew messages:\n{transcript}"
    try:
        text = await llm.complete(
            [SystemMessage(_SYSTEM), HumanMessage(prompt)], llm.Tier.FAST, 0.2,
            name="memory:summarize", priority="best_effort",
        )
    except Exception as exc:  # noqa: BLE001 - summarising is best-effort
        log.warning("memory.summarize_failed", error=str(exc), user_id=user_id)
        return False
    await summaries_repo.add(user_id, pending[-1].id, text)
    return True

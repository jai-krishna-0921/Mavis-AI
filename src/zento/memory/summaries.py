"""Keep a rolling summary of conversation that has scrolled out of working memory."""

from __future__ import annotations

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from zento.llm import models as llm
from zento.llm.tracing import callbacks
from zento.store.repo import summaries as summaries_repo

log = structlog.get_logger()
WINDOW = 20
BATCH = 20

_SYSTEM = (
    "You maintain a running summary of a chat between a user and their personal assistant. "
    "Merge the previous summary with the new messages into at most 120 words. Keep names, dates, "
    "commitments, feelings and open questions. Third person, past tense, no preamble."
)


async def maybe_summarize(user_id: int) -> bool:
    previous = await summaries_repo.latest(user_id)
    pending = await summaries_repo.messages_outside_window(
        user_id, previous.upto_message_id if previous else 0, WINDOW
    )
    if len(pending) < BATCH:
        return False
    transcript = "\n".join(f"{m.role}: {m.content}" for m in pending)
    prompt = f"Previous summary:\n{previous.summary if previous else '(none)'}\n\nNew messages:\n{transcript}"
    try:
        resp = await llm.chat_model(llm.Tier.FAST, 0.2).ainvoke(
            [SystemMessage(_SYSTEM), HumanMessage(prompt)],
            config={"callbacks": callbacks(), "run_name": "memory:summarize"},
        )
    except Exception as exc:  # noqa: BLE001 - summarising is best-effort
        log.warning("memory.summarize_failed", error=str(exc), user_id=user_id)
        return False
    text = str(resp.content).strip()
    if not text:
        return False
    await summaries_repo.add(user_id, pending[-1].id, text)
    return True

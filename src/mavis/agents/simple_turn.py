"""Phase 1 conversational turn: persona + last 20 messages -> FAST model -> bubbles in the outbox.

Replaced by agents/conversation.py in Phase 4 (registered with replace=True).
"""

from __future__ import annotations

import asyncio

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from mavis.agents import clarify, commands, persona
from mavis.bus import get_bus
from mavis.channels import presence
from mavis.domain.events import Event, Job, JobKind
from mavis.domain.messages import Outbound, Role
from mavis.initiative import wiring
from mavis.llm import models as llm
from mavis.memory.service import get_memory
from mavis.store.db import Session, utcnow
from mavis.store.models import Message
from mavis.store.repo import messages, outbox, users
from mavis.store.repo import summaries as summaries_repo

log = structlog.get_logger(__name__)


async def _initiative_hook(name: str, call) -> None:
    """Initiative bookkeeping around a turn is best-effort: it must never break the user's reply."""
    try:
        await call(wiring.current())
    except Exception:
        log.warning("simple_turn.initiative_hook_failed", hook=name, exc_info=True)

HISTORY_LIMIT = 20
START_HINT = (
    "The user just opened the chat with /start. Greet them warmly, introduce yourself in one line, "
    "and ask what's on their plate right now."
)


def user_text(event: Event) -> str:
    text = (event.payload.get("text") or "").strip()
    file = event.payload.get("file")
    if file:
        note = f"[sent a file: {file.get('file_name', 'file')}]"
        text = f"{text}\n{note}".strip() if text else note
    return text


def _to_langchain(history: list[Message]) -> list[BaseMessage]:
    return [HumanMessage(m.content) if m.role == Role.USER.value else AIMessage(m.content) for m in history]


async def build_context(user_id: int, text: str, hint: str = "") -> str:
    """Hint + rolling summary + recalled memory for the system prompt. Never raises."""
    parts = [hint] if hint else []
    try:
        memory = get_memory()
        recall, summary = await asyncio.gather(memory.recall(user_id, text), summaries_repo.latest(user_id))
        if summary:
            parts.append(f"## Earlier in our conversation\n{summary.summary}")
        parts.append(recall.render())
    except Exception:
        log.warning("simple_turn.recall_failed", exc_info=True)
    return "\n\n".join(p for p in parts if p.strip())


def _previous_reply(history: list[Message]) -> str | None:
    """The last assistant message before the most recent user message."""
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return None
    return next((m.content for m in reversed(history[:last_user]) if m.role == Role.ASSISTANT.value), None)


def _clarified_request(history: list[Message]) -> str | None:
    """The user message a clarifying question was about, if the last assistant reply was one."""
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return None
    before = history[:last_user]
    j = max((i for i, m in enumerate(before) if m.role == Role.ASSISTANT.value), default=None)
    if j is None or not clarify.is_day_question(before[j].content):
        return None
    return next((m.content for m in reversed(before[:j]) if m.role == Role.USER.value), None)


async def enqueue_learn(
    user_id: int, event: Event, text: str, previous_reply: str | None, original: str | None = None
) -> None:
    convo = f"Mavis: {previous_reply}\nUser: {text}" if previous_reply else text
    if original:
        convo = f"User: {original}\n{convo}"
    await get_bus().enqueue(Job(
        id=f"learn:{event.id}", user_id=user_id, kind=JobKind.LEARN,
        payload={"text": convo, "source_ref": event.id, "trust": event.trust.value, "conversation": True},
    ))


async def run_turn(event: Event) -> None:
    user = await users.get(event.user_id)
    text = user_text(event)
    await messages.log(user.id, Role.USER, text, event_id=event.id)
    await _initiative_hook("quiet.on_user_message", lambda i: i.quiet.on_user_message(user.id))
    await _initiative_hook("routines.on_user_message", lambda i: i.routines.on_user_message(user))
    await _initiative_hook("executor.release_deferred", lambda i: i.executor.release_deferred(user))
    if await commands.run_command(event):
        return

    # Retry after the reply was enqueued: don't call the LLM again (it could split differently).
    enqueued = await outbox.texts_with_dedupe_prefix(f"reply:{event.id}:")
    if enqueued:
        await messages.log(user.id, Role.ASSISTANT, "\n\n".join(enqueued), event_id=f"reply:{event.id}")
        await _initiative_hook("quiet.after_assistant_message",
                               lambda i: i.quiet.after_assistant_message(user.id, enqueued[-1]))
        # The first attempt may have died before enqueuing LEARN, so enqueue again. The bus does not
        # dedupe by job id; the LEARN handler skips a source_ref already recorded as processed.
        history = await messages.recent(user.id, HISTORY_LIMIT)
        await enqueue_learn(user.id, event, text, _previous_reply(history), _clarified_request(history))
        return

    history = await messages.recent(user.id, HISTORY_LIMIT)
    previous = _previous_reply(history)
    # A reply to our day question is the answer, not a new ambiguous request.
    answering = previous is not None and clarify.is_day_question(previous)
    question = None if answering else clarify.day_clarification(text, user.timezone, event.occurred_at)
    if question is not None:
        async with Session() as s:
            key = f"reply:{event.id}:0"
            await outbox.enqueue(s, Outbound(user_id=user.id, text=question, dedupe_key=key))
            await s.commit()
        await messages.log(user.id, Role.ASSISTANT, question, event_id=f"reply:{event.id}")
        await _initiative_hook("quiet.after_assistant_message",
                               lambda i: i.quiet.after_assistant_message(user.id, question))
        # The request still carries information (people, titles); the hooks skip its ambiguous time.
        await enqueue_learn(user.id, event, text, previous, _clarified_request(history))
        return

    hint = START_HINT if event.payload.get("command") == "start" else ""
    previous_reply = previous
    async with presence.typing(user.telegram_chat_id):  # refreshed until the reply is queued
        context = await build_context(user.id, text, hint)
        prompt: list[BaseMessage] = [SystemMessage(persona.system_prompt(user, utcnow(), context=context))]
        prompt += _to_langchain(history)

        reply = await llm.complete(prompt, llm.Tier.FAST, name="simple_turn")
        bubbles = persona.split_bubbles(reply) or [reply]

        async with Session() as s:
            for i, bubble in enumerate(bubbles):
                key = f"reply:{event.id}:{i}"
                await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=key))
            await s.commit()
    await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles), event_id=f"reply:{event.id}")
    await enqueue_learn(user.id, event, text, previous_reply, _clarified_request(history))
    await _initiative_hook("quiet.after_assistant_message",
                           lambda i: i.quiet.after_assistant_message(user.id, bubbles[-1]))

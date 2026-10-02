"""Phase 1 conversational turn: persona + last 20 messages -> FAST model -> bubbles in the outbox.

Replaced by agents/conversation.py in Phase 4 (registered with replace=True).
"""

from __future__ import annotations

import contextlib

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from zento.agents import persona
from zento.channels import get_channel
from zento.domain.events import Event
from zento.domain.messages import Outbound, Role
from zento.llm import models as llm
from zento.store.db import Session, utcnow
from zento.store.models import Message
from zento.store.repo import messages, outbox, users

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


async def run_turn(event: Event) -> None:
    user = await users.get(event.user_id)
    text = user_text(event)
    await messages.log(user.id, Role.USER, text, event_id=event.id)

    if user.telegram_chat_id is not None:
        with contextlib.suppress(Exception):
            await get_channel().send_typing(user.telegram_chat_id)

    history = await messages.recent(user.id, HISTORY_LIMIT)
    hint = START_HINT if event.payload.get("command") == "start" else ""
    prompt: list[BaseMessage] = [SystemMessage(persona.system_prompt(user, utcnow(), context=hint))]
    prompt += _to_langchain(history)

    reply = await llm.complete(prompt, llm.Tier.FAST, name="simple_turn")
    bubbles = persona.split_bubbles(reply) or [reply]

    async with Session() as s:
        for i, bubble in enumerate(bubbles):
            key = f"reply:{event.id}:{i}"
            await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=key))
        await s.commit()
    await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles), event_id=f"reply:{event.id}")

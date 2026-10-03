"""Shared helpers for a chat turn (moved out of agents/simple_turn.py for the Phase 4 conversation).

History, recall context, connection lines, name lookup, taint markers on logged replies, the LEARN job
and the best-effort initiative hook wrapper.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from mavis.agents import clarify, context_hooks
from mavis.bus import get_bus
from mavis.domain.events import Event, Job, JobKind, Trust
from mavis.domain.messages import TAINT_SUFFIX, Role
from mavis.initiative import wiring
from mavis.memory.service import get_memory
from mavis.store.models import Message
from mavis.store.repo import profile as profile_repo
from mavis.store.repo import summaries as summaries_repo

log = structlog.get_logger(__name__)


async def initiative_hook(name: str, call) -> None:
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
RESTART_HINT = (
    "The user sent /start again in the middle of an ongoing chat. Welcome them back in one short line. "
    "Do not introduce yourself again."
)


def user_text(event: Event) -> str:
    text = (event.payload.get("text") or "").strip()
    file = event.payload.get("file")
    if file:
        note = f"[sent a file: {file.get('file_name', 'file')}]"
        text = f"{text}\n{note}".strip() if text else note
    return text


def to_langchain(history: list[Message]) -> list[BaseMessage]:
    return [HumanMessage(m.content) if m.role == Role.USER.value else AIMessage(m.content) for m in history]


async def build_context_ex(user_id: int, text: str, hint: str = "") -> tuple[str, bool]:
    """Hint + rolling summary + recalled memory + hook context for the system prompt. Never raises.

    The bool is True when hook context (the inbox digest, ...) was included. That block is derived from
    third-party content, so the whole turn is tainted, exactly as if the model had read an email.
    """
    parts = [hint] if hint else []
    try:
        memory = get_memory()
        recall, summary = await asyncio.gather(memory.recall(user_id, text), summaries_repo.latest(user_id))
        if summary:
            parts.append(f"## Earlier in our conversation\n{summary.summary}")
        parts.append(recall.render())
    except Exception:
        log.warning("simple_turn.recall_failed", exc_info=True)
    extra = await context_hooks.gather_context(user_id, text)  # never raises
    if extra.strip():
        parts.append(extra)
    return "\n\n".join(p for p in parts if p.strip()), bool(extra.strip())


async def build_context(user_id: int, text: str, hint: str = "") -> str:
    return (await build_context_ex(user_id, text, hint))[0]


# TAINT_SUFFIX (domain.messages): a reply written after the model read untrusted tool output is logged
# with it (no schema change). Learn text that includes it is learned at untrusted trust.


def reply_event_id(event_id: str, tainted: bool) -> str:
    return f"reply:{event_id}{TAINT_SUFFIX if tainted else ''}"


def is_tainted(message: Message) -> bool:
    return bool(message.event_id and message.event_id.endswith(TAINT_SUFFIX))


def previous_message(history: list[Message]) -> Message | None:
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return None
    return next((m for m in reversed(history[:last_user]) if m.role == Role.ASSISTANT.value), None)


def previous_reply(history: list[Message]) -> str | None:
    """The last assistant message before the most recent user message."""
    prev = previous_message(history)
    return prev.content if prev is not None else None


def previous_tainted(history: list[Message]) -> bool:
    """Any assistant message since the previous user message is tainted (a tainted reply followed by an
    approval prompt, a proactive ping, ...): the current turn sees it in its prompt."""
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return False
    for m in reversed(history[:last_user]):
        if m.role == Role.USER.value:
            break
        if is_tainted(m):
            return True
    return False


def window_tainted(history: list[Message]) -> bool:
    """Any assistant message in the replayed history window is tainted. The whole window goes into the
    prompt (to_langchain), so a tainted reply several turns back can still steer this turn."""
    return any(m.role == Role.ASSISTANT.value and is_tainted(m) for m in history)


def clarified_request(history: list[Message]) -> str | None:
    """The user message a clarifying question was about, if the last assistant reply was one."""
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return None
    before = history[:last_user]
    j = max((i for i, m in enumerate(before) if m.role == Role.ASSISTANT.value), default=None)
    if j is None or not clarify.is_day_question(before[j].content):
        return None
    return next((m.content for m in reversed(before[:j]) if m.role == Role.USER.value), None)


CONNECTION_TIMEOUT_S = 1.5
CONNECTION_RETRY_AFTER_S = 60.0
_STATE_NAMES = {"ACTIVE": "connected", "INITIATED": "pending", "FAILED": "needs reconnecting"}
_failed_until: dict[int, float] = {}  # user_id -> monotonic time before which we do not ask again


async def connection_states(user_id: int) -> dict[str, str]:
    """Per-capability link state for the persona. Best effort: {} means unknown.

    A failure or timeout is remembered for CONNECTION_RETRY_AFTER_S so a slow or down provider is not
    hit (and waited on) every turn.
    """
    if time.monotonic() < _failed_until.get(user_id, 0.0):
        return {}
    try:
        from mavis.tools.integrations import get_connection_cache, get_provider

        if not getattr(get_provider(), "configured", True):
            return {}  # no provider credentials: we cannot know, so the persona says unknown
        states = await asyncio.wait_for(get_connection_cache().status(user_id), CONNECTION_TIMEOUT_S)
        return {slug: _STATE_NAMES.get(str(getattr(st, "value", st)), "not connected")
                for slug, st in states.items()}
    except Exception:  # noqa: BLE001 - the prompt must never depend on integrations being up
        _failed_until[user_id] = time.monotonic() + CONNECTION_RETRY_AFTER_S
        log.debug("simple_turn.connection_state_failed", exc_info=True)
        return {}


async def known_name(user_id: int) -> str | None:
    try:
        return (await profile_repo.get(user_id)).name
    except Exception:  # noqa: BLE001
        return None


async def enqueue_learn(
    user_id: int, event: Event, text: str, previous_reply: str | None, original: str | None = None,
    *, tainted: bool = False,
) -> None:
    """`tainted`: the turn or the included previous reply saw untrusted tool output: learn as untrusted."""
    trust = Trust.UNTRUSTED.value if tainted else event.trust.value
    convo = f"Mavis: {previous_reply}\nUser: {text}" if previous_reply else text
    if original:
        convo = f"User: {original}\n{convo}"
    await get_bus().enqueue(Job(
        id=f"learn:{event.id}", user_id=user_id, kind=JobKind.LEARN,
        payload={"text": convo, "source_ref": event.id, "trust": trust, "conversation": True},
    ))

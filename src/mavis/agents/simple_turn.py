"""Conversational turn: persona + last 20 messages -> FAST model with READ-ONLY tools -> bubbles.

The model may look things up (mail search/read, calendar, web, memory, task list) through a small
bounded react loop. Nothing outward or approval-gated is exposed here: sending, creating, forgetting
and standing rules wait for the Phase 4 conversation graph and its approval flow. A small-talk turn
is still exactly one model call (the model answers without asking for a tool).

Replaced by agents/conversation.py in Phase 4 (registered with replace=True).
"""

from __future__ import annotations

import asyncio
import time

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from mavis.agents import clarify, commands, persona
from mavis.agents.react import react_loop
from mavis.bus import get_bus
from mavis.channels import presence
from mavis.domain.errors import ConnectionRequired
from mavis.domain.events import Event, Job, JobKind, Trust
from mavis.domain.messages import Outbound, Role
from mavis.domain.policy import RiskClass
from mavis.initiative import wiring
from mavis.llm import models as llm
from mavis.memory.service import get_memory
from mavis.policy.risk import UNTRUSTED_NOTE
from mavis.store.db import Session, utcnow
from mavis.store.models import Message
from mavis.store.repo import messages, outbox, users
from mavis.store.repo import profile as profile_repo
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


# A reply written after the model read untrusted tool output (an email, a web page) is logged with this
# event_id suffix (no schema change). Learn text that includes it is learned at untrusted trust.
TAINT_SUFFIX = ":tainted"


def _reply_event_id(event_id: str, tainted: bool) -> str:
    return f"reply:{event_id}{TAINT_SUFFIX if tainted else ''}"


def is_tainted(message: Message) -> bool:
    return bool(message.event_id and message.event_id.endswith(TAINT_SUFFIX))


def _previous_message(history: list[Message]) -> Message | None:
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=None)
    if last_user is None:
        return None
    return next((m for m in reversed(history[:last_user]) if m.role == Role.ASSISTANT.value), None)


def _previous_reply(history: list[Message]) -> str | None:
    """The last assistant message before the most recent user message."""
    prev = _previous_message(history)
    return prev.content if prev is not None else None


def _previous_tainted(history: list[Message]) -> bool:
    prev = _previous_message(history)
    return prev is not None and is_tainted(prev)


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


async def _known_name(user_id: int) -> str | None:
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


# --- read-only tools for chat turns ---------------------------------------------------------------

# Explicit allowlist, and every entry must also be a plain READ tool (checked at selection time), so a
# catalog change can never slip an outward, destructive or memory-writing tool into chat.
CHAT_TOOL_NAMES = (
    "mail_search", "mail_read",
    "calendar_list", "calendar_find", "calendar_free_slots",
    "web_search", "web_extract",
    "what_do_you_know", "list_tasks",
)
CHAT_MAX_STEPS = 4  # tool rounds; then the model must answer with what it has
CHAT_DEADLINE_S = 40.0  # after this, no more tool rounds: answer now
CHAT_TOOL_TIMEOUT_S = 20.0  # per tool call (provider round trips), well inside the turn deadline
WRAP_UP_FALLBACK = "I couldn't finish checking that just now. Want me to try again?"

TOOLS_GUIDE = (
    "Looking things up\n"
    "- You have read-only tools: search and read their Gmail, check their calendar and free time, search "
    "the web and read a page, recall what you know, list background tasks. Use them when the answer "
    "depends on their email, calendar or the web, instead of saying you don't have it in front of you.\n"
    "- For an email: mail_search first (it returns message ids), then mail_read with the right message_id "
    "for the full text. Don't call a tool for small talk.\n"
    "- These tools only read. You cannot send, reply, create events or change anything from here; if "
    "they ask, say that needs their OK and is coming next.\n"
    f"- {UNTRUSTED_NOTE}"
)


def chat_tools(user_id: int) -> list[BaseTool]:
    """The READ-only tools a chat turn may use. Never raises: no tools means a plain reply."""
    try:
        from mavis.tools.registry import get_registry

        registry = get_registry()
        safe = []
        for name in CHAT_TOOL_NAMES:
            try:
                tool = registry.get(name)
            except KeyError:
                continue
            if tool.risk is RiskClass.READ and tool.risk_fn is None:
                safe.append(name)
        return registry.for_agent("conversation", user_id, names=safe)
    except Exception:  # noqa: BLE001 - tools are an extra; the turn must still answer
        log.warning("simple_turn.tools_unavailable", exc_info=True)
        return []


def _connect_hint(exc: ConnectionRequired) -> str:
    from mavis.tools.integrations.actions import DISPLAY_NAMES

    name = DISPLAY_NAMES.get(exc.capability, exc.capability.value)
    word = "calendar" if exc.capability.value == "googlecalendar" else exc.capability.value
    return f"I need your {name} linked for that. Send /connect {word} and I'll take it from there."


async def _connect_prompt(event: Event, user_id: int, exc: ConnectionRequired) -> list[str]:
    """Hand a missing/expired link to the connect flow (button prompt). Returns the texts it sent.

    If the flow itself fails, a plain /connect hint goes out instead: never an error.
    """
    try:
        from mavis.tools.integrations.wiring import get_connect_flow

        flow = get_connect_flow()
        with flow.reply_scope(event.id) as scope:
            await flow.start(user_id, exc.capability, exc.reason, revoked=exc.revoked)
        if scope.texts:
            return scope.texts
    except Exception:  # noqa: BLE001 - the user still gets told what to do
        log.warning("simple_turn.connect_flow_failed", capability=exc.capability.value, exc_info=True)
    hint = _connect_hint(exc)
    async with Session() as s:
        await outbox.enqueue(s, Outbound(user_id=user_id, text=hint, dedupe_key=f"reply:{event.id}:0"))
        await s.commit()
    return [hint]


async def run_turn(event: Event) -> None:
    user = await users.get(event.user_id)
    text = user_text(event)
    await messages.log(user.id, Role.USER, text, event_id=event.id)
    await _initiative_hook("quiet.on_user_message", lambda i: i.quiet.on_user_message(user.id))
    await _initiative_hook("routines.on_user_message", lambda i: i.routines.on_user_message(user))
    await _initiative_hook("executor.release_deferred", lambda i: i.executor.release_deferred(user))
    await _initiative_hook("loops.on_user_message", lambda i: i.loops.on_user_message(user.id, text))
    if await commands.run_command(event):
        return

    # Retry after the reply was enqueued: don't call the LLM again (it could split differently).
    enqueued = await outbox.texts_with_dedupe_prefix(f"reply:{event.id}:")
    if enqueued:
        # Whether the first attempt read untrusted output is unknown unless it logged: assume it did.
        tainted = not await messages.exists(_reply_event_id(event.id, False))
        if tainted:
            await messages.log(user.id, Role.ASSISTANT, "\n\n".join(enqueued),
                               event_id=_reply_event_id(event.id, True))
        await _initiative_hook("quiet.after_assistant_message",
                               lambda i: i.quiet.after_assistant_message(user.id, enqueued[-1]))
        # The first attempt may have died before enqueuing LEARN, so enqueue again. The bus does not
        # dedupe by job id; the LEARN handler skips a source_ref already recorded as processed.
        history = await messages.recent(user.id, HISTORY_LIMIT)
        await enqueue_learn(user.id, event, text, _previous_reply(history), _clarified_request(history),
                            tainted=tainted or _previous_tainted(history))
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
        await enqueue_learn(user.id, event, text, previous, _clarified_request(history),
                            tainted=_previous_tainted(history))
        return

    hint = ""
    if event.payload.get("command") == "start":
        recent_assistant = any(
            m.role == Role.ASSISTANT.value for m in persona.recent_messages(history, utcnow())
        )
        hint = RESTART_HINT if recent_assistant else START_HINT
    previous_reply = previous
    async with presence.typing(user.telegram_chat_id):  # refreshed until the reply is queued
        context, connections, card_name = await asyncio.gather(
            build_context(user.id, text, hint), connection_states(user.id), _known_name(user.id)
        )
        now = utcnow()
        known_name = user.name or card_name
        recent = persona.recent_messages(history, now)
        system = persona.system_prompt(
            user, now, context=context, connections=connections, known_name=known_name,
            ask_name=persona.should_ask_name(known_name, history, now, user.timezone),
            prior_turns=max(len(recent) - 1, 0),  # the current message is already in history
        )
        tools = chat_tools(user.id)
        if tools:
            system = f"{system}\n\n{TOOLS_GUIDE}"
        prompt: list[BaseMessage] = [SystemMessage(system)]
        prompt += _to_langchain(history)

        connect_texts: list[str] = []
        try:
            result = await react_loop(
                tools, prompt, CHAT_MAX_STEPS, tier=llm.Tier.FAST, temperature=0.6, name="simple_turn",
                wrap_up=True, deadline_s=CHAT_DEADLINE_S, tool_timeout_s=CHAT_TOOL_TIMEOUT_S,
            )
        except ConnectionRequired as exc:
            result = None
            connect_texts = await _connect_prompt(event, user.id, exc)
        if result is None:
            # The connect prompt (with buttons) is already queued under the flow's own dedupe keys.
            await messages.log(user.id, Role.ASSISTANT, "\n\n".join(connect_texts),
                               event_id=f"reply:{event.id}")
            await enqueue_learn(user.id, event, text, previous_reply, _clarified_request(history),
                                tainted=_previous_tainted(history))
            await _initiative_hook("quiet.after_assistant_message",
                                   lambda i: i.quiet.after_assistant_message(user.id, connect_texts[-1]))
            return
        if result.tools_called:
            log.info("simple_turn.tools", tools=result.tools_called, steps=result.steps,
                     tainted=result.tainted, wrapped_up=result.wrapped_up)
        reply = result.text or WRAP_UP_FALLBACK
        bubbles = persona.split_bubbles(reply) or [reply]

        async with Session() as s:
            for i, bubble in enumerate(bubbles):
                key = f"reply:{event.id}:{i}"
                await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=key))
            await s.commit()
    await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles),
                       event_id=_reply_event_id(event.id, result.tainted))
    await enqueue_learn(user.id, event, text, previous_reply, _clarified_request(history),
                        tainted=result.tainted or _previous_tainted(history))
    await _initiative_hook("quiet.after_assistant_message",
                           lambda i: i.quiet.after_assistant_message(user.id, bubbles[-1]))

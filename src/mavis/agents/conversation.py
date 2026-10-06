"""Conversational turn: persona + recent history -> one tool-calling PA agent -> bubbles.

There is no router (preflight F3). The model answers in text (small talk is exactly one model call) or
calls tools from a bounded per-turn set (`registry.select`, at most CHAT_TOOL_LIMIT):
- reads: mail, calendar, web search, memory, task list;
- self-only writes: remember, wake_me, track_loop, mail_draft, start_task, connect_account;
- approval-gated actions: mail_send, mail_reply, calendar_create_event with guests, forget,
  add_policy_rule (and cancel_task, wake_me, track_loop once the turn read third-party content).
  These are queued, never run here: after the reply goes out, queued approvals are attached to an
  APPROVAL task whose approval gate sends the Approve / Edit / Cancel prompt (Phase 4 Task 9).

Taint: once the model has read untrusted tool output, or when the previous reply it sees was written
from such output, trusted writes downgrade (remember is kept as unverified, wake_me / track_loop /
cancel_task queue for approval), standing rules stop auto-approving, a started task is tainted, and
the turn learns at untrusted trust. web_extract is never offered in chat: a planted email must not be
able to send data out through a URL.

A text reply to an approval prompt ("ok", "send it", "cancel", "make it shorter") is a pre-check before
the agent: it only applies when the prompt is among the last 2 assistant messages (recency gate).
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import timedelta

import structlog
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import BaseTool

from mavis.agents import clarify, commands, persona
from mavis.agents.react import react_loop
from mavis.agents.task_dispatch import enqueue_run
from mavis.agents.turn_support import (
    HISTORY_LIMIT,
    RESTART_HINT,
    START_HINT,
    build_context_ex,
    clarified_request,
    connection_states,
    enqueue_learn,
    initiative_hook,
    known_name,
    previous_reply,
    previous_tainted,
    reply_event_id,
    to_langchain,
    user_text,
    window_tainted,
)
from mavis.channels import presence
from mavis.channels.formatting import strip_verbatim
from mavis.domain.errors import ConnectionRequired
from mavis.domain.events import Event
from mavis.domain.messages import Outbound, Role
from mavis.domain.tasks import ApprovalStatus, TaskKind, TaskOrigin
from mavis.domain.timefmt import strip_stamps
from mavis.llm import models as llm
from mavis.policy import approvals as approval_flow
from mavis.policy.risk import UNTRUSTED_NOTE
from mavis.store.db import Session, utcnow
from mavis.store.models import Message, PendingApproval
from mavis.store.repo import approvals, messages, outbox, tasks, users
from mavis.tools.chat_tools import TurnInfo, current_turn

log = structlog.get_logger(__name__)

# What the turn did, for Phase 7 metrics: SMALL_TALK, DIRECT_TOOL, TASK, CONNECT or APPROVAL_REPLY.
current_route: ContextVar[str | None] = ContextVar("current_route", default=None)

CHAT_TOOL_LIMIT = 8
CHAT_ALWAYS = ("start_task", "connect_account", "pending")
CHAT_EXCLUDED = frozenset({"web_extract"})  # URL fetches would let injected text exfiltrate data
# Offered together: mail_search returns short previews only, so without mail_read a question about an
# email (one a brief mentioned, say) cannot be answered from its text; mail_read needs search's ids.
CHAT_COMPANIONS = {"mail_search": "mail_read", "mail_read": "mail_search"}
CHAT_MAX_STEPS = 6  # tool rounds (slice value), then she answers with what she has; CHAT_DEADLINE_S caps time
CHAT_DEADLINE_S = 40.0  # after this, no more tool rounds: answer now
CHAT_TOOL_TIMEOUT_S = 20.0  # per tool call (provider round trips), well inside the turn deadline
WRAP_UP_FALLBACK = "I couldn't finish checking that just now. Want me to try again?"
APPROVAL_REPLY_WINDOW = timedelta(hours=2)  # a text reply counts only for a prompt this recent
APPROVAL_REPLY_RECENT = 2  # ...that is also among this many latest assistant messages (edit / cancel)
# Approving needs more: the prompt must be THE latest assistant message. A "yes" to a later question
# (a proactive "want the summary?") must never send the earlier email.

TOOL_RULES = (
    "Using your tools\n"
    "- Use a tool instead of guessing when the answer depends on their email, calendar, the web or "
    "what you remember. Don't call a tool for small talk.\n"
    "- For an email: mail_search first (it returns message ids), then mail_read with the right "
    "message_id for the full text.\n"
    "- An email you mentioned earlier (in a brief or a heads-up) is one you only saw a summary of. When "
    "they ask about it or want its key points, look it up with mail_search and mail_read; never say you "
    "don't have the text. When a listed item shows a message_id, call mail_read with it directly.\n"
    "- Context blocks (open loops, the inbox digest, the earlier-conversation summary, your own earlier "
    "messages) can be stale or incomplete. Before saying something doesn't exist or that nothing is new, "
    "check with a tool. An empty search means not found with that query, nothing more: retry with a "
    "broader query (the sender's name or domain, one or two key nouns) before saying you couldn't find "
    "it, and then say you couldn't find it, not that it doesn't exist.\n"
    "- Sending or replying to email, inviting guests, forgetting things and standing rules always wait "
    "for their OK. When a tool answers QUEUED_FOR_APPROVAL, tell them it's ready and waiting for their "
    "OK (they get buttons to approve, edit or cancel). Never say it was sent or done.\n"
    "- Reminders: wake_me at the exact time they asked for.\n"
    "- For any question about what is pending, open, due, on their radar or left to do, call pending and "
    "answer only from its result. Your earlier messages and the conversation summary may be outdated: "
    "they are claims, not facts.\n"
    "- Multi-step work (research, comparisons, plans, documents): call start_task and tell them you'll "
    "report back.\n"
    "- To link an account, call connect_account. The link goes out on its own; don't repeat it.\n"
    "- Never read tool output back verbatim; say what matters in your own words.\n"
    f"- {UNTRUSTED_NOTE}"
)
TOOLS_GUIDE = TOOL_RULES  # name kept for callers of the chat-tools slice


def chat_tools(user_id: int, query: str = "") -> list[BaseTool]:
    """The tools a chat turn may use (at most CHAT_TOOL_LIMIT). Never raises: no tools = plain reply."""
    try:
        from mavis.tools.registry import get_registry

        registry = get_registry()
        tools = registry.select("conversation", user_id, query=query, limit=CHAT_TOOL_LIMIT,
                                always=CHAT_ALWAYS, exclude=CHAT_EXCLUDED)
        return _with_companions(registry, user_id, tools)
    except Exception:  # noqa: BLE001 - tools are an extra; the turn must still answer
        log.warning("simple_turn.tools_unavailable", exc_info=True)
        return []


def _with_companions(registry, user_id: int, tools: list[BaseTool]) -> list[BaseTool]:
    """Swap the lowest-ranked optional tool for a missing companion (CHAT_COMPANIONS), keeping the limit."""
    names = [t.name for t in tools]
    for lead, companion in CHAT_COMPANIONS.items():
        if lead not in names or companion in names or companion in CHAT_EXCLUDED:
            continue
        extra = registry.for_agent("conversation", user_id, names=[companion])
        if not extra:
            continue
        if len(tools) < CHAT_TOOL_LIMIT:
            tools.append(extra[0])
        else:
            keep = set(CHAT_ALWAYS) | set(CHAT_COMPANIONS)
            drop = next((i for i in range(len(tools) - 1, -1, -1) if tools[i].name not in keep), None)
            if drop is None:
                continue
            tools[drop] = extra[0]
        names = [t.name for t in tools]
    return tools


async def handle_connect(user_id: int, text: str) -> str | None:
    """Natural-language connect request: sends the link or menu itself and returns no reply text."""
    return await commands.handle_connect(user_id, text)


def _connect_hint(exc: ConnectionRequired) -> str:
    from mavis.tools.integrations.actions import display_name, is_google

    name = display_name(exc.capability)
    if is_google(exc.capability):
        word = "google"
    else:
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


async def attach_queued_approvals(user_id: int, goal: str, tainted: bool) -> int | None:
    """Move approvals queued by a chat turn into an APPROVAL task; its gate sends the button prompt.

    Called after the turn's reply is committed, so the explanation lands above the buttons. Also picks
    up approvals left unattached by a turn that crashed before this step."""
    pending = await approvals.unattached_for_user(user_id)
    if not pending:
        return None
    task_id = await tasks.create(user_id, goal=goal[:2000], kind=TaskKind.APPROVAL, origin=TaskOrigin.USER,
                                 tainted=tainted)
    await approvals.attach([a.id for a in pending], task_id)
    await enqueue_run(task_id, user_id)
    log.info("conversation.approvals_queued", task_id=task_id, approvals=[a.id for a in pending])
    return task_id


def _read_untrusted(tools_called: list[str]) -> bool:
    """This turn itself read third-party output (an email, a web result).

    Not `ReactResult.tainted`, which also carries the taint the turn started with: marking a reply
    tainted only because the one before it was would keep every later turn tainted for good."""
    try:
        from mavis.tools.registry import get_registry

        registry = get_registry()
        return any(registry.get(name).untrusted_output for name in tools_called)
    except KeyError:
        return True  # unknown tool: assume the worst


def _route_for(tools_called: list[str]) -> str:
    if "start_task" in tools_called:
        return "TASK"
    if "connect_account" in tools_called:
        return "CONNECT"
    return "DIRECT_TOOL" if tools_called else "SMALL_TALK"


# --- text replies to an approval prompt ------------------------------------------------------------


def _recent_assistant(history: list[Message], n: int) -> list[str]:
    """The last `n` assistant messages before the newest user message (the one being answered)."""
    last_user = max((i for i, m in enumerate(history) if m.role == Role.USER.value), default=len(history))
    before = [m.content for m in history[:last_user] if m.role == Role.ASSISTANT.value]
    return before[-n:]


def _shown(approval: PendingApproval, recent: list[str]) -> bool:
    preview = strip_verbatim(approval.preview or "").strip()  # history keeps text as written
    return bool(preview) and any(preview in text for text in recent)


async def approval_awaiting_reply(user_id: int, history: list[Message]) -> PendingApproval | None:
    """The approval this message may be answering, or None (then it is an ordinary chat message).

    - AWAITING_EDIT (they tapped Edit): when the edit question or the prompt is still recent.
    - PENDING: prompted within APPROVAL_REPLY_WINDOW and its prompt is among the last
      APPROVAL_REPLY_RECENT assistant messages. A stale prompt far back in the chat never matches,
      so a later "ok" about something else cannot approve it.
    With several approvals waiting, apply_reply asks which one instead of guessing.
    """
    waiting = [a for a in await approvals.open_for_user(user_id)
               if a.status in (ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT)]
    if not waiting:
        return None
    recent = _recent_assistant(history, APPROVAL_REPLY_RECENT)
    editing = [a for a in waiting if a.status == ApprovalStatus.AWAITING_EDIT
               and (_shown(a, recent) or any(approval_flow.EDIT_QUESTION in t for t in recent))]
    if editing:
        return editing[-1]
    cutoff = utcnow() - APPROVAL_REPLY_WINDOW
    prompted = [a for a in waiting if a.status == ApprovalStatus.PENDING and a.prompted_at is not None
                and a.prompted_at >= cutoff and _shown(a, recent)]
    return prompted[-1] if prompted else None


def prompt_is_latest(approval: PendingApproval, history: list[Message]) -> bool:
    """The approval's prompt (or, after Edit was tapped, the edit question) is the newest assistant
    message, with nothing (proactive or otherwise) after it."""
    latest = _recent_assistant(history, 1)
    if _shown(approval, latest):
        return True
    return approval.status == ApprovalStatus.AWAITING_EDIT and any(
        approval_flow.EDIT_QUESTION in t for t in latest)


async def _approval_reply(event: Event, user_id: int, text: str, history: list[Message]) -> bool:
    approval = await approval_awaiting_reply(user_id, history)
    if approval is None:
        return False
    interp = await approval_flow.interpret_reply(approval, text)
    if interp.decision == "approve" and not prompt_is_latest(approval, history):
        log.info("conversation.approve_not_latest", approval_id=approval.id)
        return False  # something was said after the prompt: this "yes" may answer that instead
    ack = await approval_flow.apply_reply(approval, interp)
    if ack is None:  # unrelated: an ordinary message after all
        return False
    async with Session() as s:
        await outbox.enqueue(s, Outbound(user_id=user_id, text=ack, dedupe_key=f"reply:{event.id}:0"))
        await s.commit()
    await messages.log(user_id, Role.ASSISTANT, ack, event_id=f"reply:{event.id}")
    await enqueue_learn(user_id, event, text, previous_reply(history), tainted=previous_tainted(history))
    await initiative_hook("quiet.after_assistant_message",
                          lambda i: i.quiet.after_assistant_message(user_id, ack))
    current_route.set("APPROVAL_REPLY")
    log.info("conversation.approval_reply", approval_id=approval.id, decision=interp.decision)
    return True


# --- the turn --------------------------------------------------------------------------------------


async def run_turn(event: Event) -> None:
    text = user_text(event)
    current_route.set(None)
    if not text:
        return
    user = await users.get(event.user_id)
    await messages.log(user.id, Role.USER, text, event_id=event.id)
    await initiative_hook("quiet.on_user_message", lambda i: i.quiet.on_user_message(user.id))
    await initiative_hook("routines.on_user_message", lambda i: i.routines.on_user_message(user))
    await initiative_hook("executor.release_deferred", lambda i: i.executor.release_deferred(user))
    await initiative_hook("loops.on_user_message", lambda i: i.loops.on_user_message(user.id, text))
    if await commands.run_command(event):
        current_route.set("CONNECT")
        return

    # Retry after the reply was enqueued: don't call the LLM again (it could split differently).
    enqueued = await outbox.texts_with_dedupe_prefix(f"reply:{event.id}:")
    if enqueued:
        # Whether the first attempt read untrusted output is unknown unless it logged: assume it did.
        tainted = not await messages.exists(reply_event_id(event.id, False))
        if tainted:
            await messages.log(user.id, Role.ASSISTANT, "\n\n".join(enqueued),
                               event_id=reply_event_id(event.id, True))
        await initiative_hook("quiet.after_assistant_message",
                              lambda i: i.quiet.after_assistant_message(user.id, enqueued[-1]))
        # The first attempt may have died before enqueuing LEARN, so enqueue again. The bus does not
        # dedupe by job id; the LEARN handler skips a source_ref already recorded as processed.
        history = await messages.recent(user.id, HISTORY_LIMIT)
        await enqueue_learn(user.id, event, text, previous_reply(history), clarified_request(history),
                            tainted=tainted or previous_tainted(history))
        await attach_queued_approvals(user.id, text, tainted=tainted)  # it may have died before this
        return

    history = await messages.recent(user.id, HISTORY_LIMIT)
    previous = previous_reply(history)
    # A reply to our day question is the answer, not a new ambiguous request.
    answering = previous is not None and clarify.is_day_question(previous)
    question = None if answering else clarify.day_clarification(text, user.timezone, event.occurred_at)
    if question is not None:
        async with Session() as s:
            key = f"reply:{event.id}:0"
            await outbox.enqueue(s, Outbound(user_id=user.id, text=question, dedupe_key=key))
            await s.commit()
        await messages.log(user.id, Role.ASSISTANT, question, event_id=f"reply:{event.id}")
        await initiative_hook("quiet.after_assistant_message",
                              lambda i: i.quiet.after_assistant_message(user.id, question))
        # The request still carries information (people, titles); the hooks skip its ambiguous time.
        await enqueue_learn(user.id, event, text, previous, clarified_request(history),
                            tainted=previous_tainted(history))
        return

    if await _approval_reply(event, user.id, text, history):
        return

    hint = ""
    if event.payload.get("command") == "start":
        recent_assistant = any(
            m.role == Role.ASSISTANT.value for m in persona.recent_messages(history, utcnow())
        )
        hint = RESTART_HINT if recent_assistant else START_HINT
    async with presence.typing(user.telegram_chat_id):  # refreshed until the reply is queued
        (context, hooked), connections, card_name = await asyncio.gather(
            build_context_ex(user.id, text, hint, tz=user.timezone), connection_states(user.id),
            known_name(user.id),
        )
        # Start tainted when third-party content is already in the prompt: any replayed assistant message
        # was written from it, or hook context (the inbox digest) was added. The react loop then applies
        # the taint rules (outward tools and start_task need approval, a started task is tainted).
        carried_taint = window_tainted(history) or hooked
        # LEARN sees only the user's text and the previous reply, so its trust keeps the per-turn rule.
        learn_taint = previous_tainted(history) or hooked
        now = utcnow()
        name = user.name or card_name
        recent = persona.recent_messages(history, now)
        system = persona.system_prompt(
            user, now, context=context, connections=connections, known_name=name,
            ask_name=persona.should_ask_name(name, history, now, user.timezone),
            prior_turns=max(len(recent) - 1, 0),  # the current message is already in history
        )
        tools = chat_tools(user.id, query=f"{text}\n{previous or ''}")
        if tools:
            system = f"{system}\n\n{TOOL_RULES}"
        prompt: list[BaseMessage] = [SystemMessage(system)]
        prompt += to_langchain(history, now, user.timezone)

        connect_texts: list[str] = []
        token = current_turn.set(TurnInfo(event_id=event.id))
        try:
            result = await react_loop(
                tools, prompt, CHAT_MAX_STEPS, tier=llm.Tier.FAST, temperature=0.6, name="simple_turn",
                tainted=carried_taint, wrap_up=True, deadline_s=CHAT_DEADLINE_S,
                tool_timeout_s=CHAT_TOOL_TIMEOUT_S,
            )
        except ConnectionRequired as exc:
            result = None
            connect_texts = await _connect_prompt(event, user.id, exc)
        finally:
            current_turn.reset(token)
        if result is None:
            # The connect prompt (with buttons) is already queued under the flow's own dedupe keys.
            await messages.log(user.id, Role.ASSISTANT, "\n\n".join(connect_texts),
                               event_id=f"reply:{event.id}")
            await enqueue_learn(user.id, event, text, previous, clarified_request(history),
                                tainted=learn_taint)
            # Anything the same step queued (an email to send, say) still gets its prompt.
            await attach_queued_approvals(user.id, text, tainted=carried_taint)
            await initiative_hook("quiet.after_assistant_message",
                                  lambda i: i.quiet.after_assistant_message(user.id, connect_texts[-1]))
            current_route.set("CONNECT")
            return
        if result.tools_called:
            log.info("simple_turn.tools", tools=result.tools_called, steps=result.steps,
                     tainted=result.tainted, wrapped_up=result.wrapped_up,
                     queued=result.queued_approvals)
        # replayed messages carry stamps (T1); one echoed at the start of a line is not content
        reply = strip_stamps(result.text or "").strip() or WRAP_UP_FALLBACK
        bubbles = persona.split_bubbles(reply) or [reply]

        async with Session() as s:
            for i, bubble in enumerate(bubbles):
                key = f"reply:{event.id}:{i}"
                await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=key))
            await s.commit()
    # This turn's own untrusted input marks the reply: a tool read, or the digest it was shown.
    read_untrusted = _read_untrusted(result.tools_called) or result.read_untrusted or hooked
    await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles),
                       event_id=reply_event_id(event.id, read_untrusted))
    await enqueue_learn(user.id, event, text, previous, clarified_request(history),
                        tainted=read_untrusted or learn_taint)
    await attach_queued_approvals(user.id, text, tainted=result.tainted)
    await initiative_hook("quiet.after_assistant_message",
                          lambda i: i.quiet.after_assistant_message(user.id, bubbles[-1]))
    current_route.set(_route_for(result.tools_called))

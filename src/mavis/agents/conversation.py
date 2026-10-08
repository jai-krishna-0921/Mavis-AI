"""Conversational turn: persona + recent history -> one tool-calling PA agent -> bubbles.

There is no router (preflight F3). The model answers in text (small talk is exactly one model call) or
calls tools from a bounded per-turn set (`registry.select`, at most CHAT_TOOL_LIMIT):
- reads: mail, calendar, web search, memory, task list;
- self-only writes: remember, wake_me, track_loop, mail_draft, start_task, connect_account;
- approval-gated actions: mail_send, mail_reply, calendar_create_event with guests, forget,
  add_policy_rule (and cancel_task, wake_me, track_loop once the turn read third-party content).
  These are queued, never run here: after the reply goes out, queued approvals are attached to an
  APPROVAL task whose approval gate sends the Approve / Edit / Cancel prompt (Phase 4 Task 9).

Taint: once the model has read untrusted tool output, or when a replayed reply was written from such
output, standing rules stop auto-approving outward actions, a started task is tainted, and the turn
learns at untrusted trust. Self-only writes (remember, wake_me, track_loop, start_task, cancel_task...)
are judged by what can steer THIS turn only: its own untrusted reads, hook context and the reply just
before the user's message (track 1 T1.1). Then remember is kept as unverified and the others queue for
approval, unless their wording comes from the user's own message this turn (domain.terms). web_extract
is never offered in chat: a planted email must not be able to send data out through a URL.

A text reply to an approval prompt ("ok", "send it", "cancel", "make it shorter") is a pre-check before
the agent: it only applies when the prompt is among the last 2 assistant messages (recency gate).
"""

from __future__ import annotations

import asyncio
import dataclasses
from contextvars import ContextVar
from datetime import timedelta

import structlog
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from mavis.agents import claims, clarify, commands, persona, reactions, register
from mavis.agents.react import ReactResult, _text_of, react_loop
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
    tainted_texts,
    to_langchain,
    user_text,
    window_tainted,
)
from mavis.channels import presence
from mavis.channels.formatting import strip_verbatim
from mavis.config import get_settings
from mavis.domain.errors import ConnectionRequired, LLMError
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
from mavis.tools.registry import CARD_RESULT_PREFIXES

log = structlog.get_logger(__name__)

# What the turn did, for Phase 7 metrics: SMALL_TALK, DIRECT_TOOL, TASK, CONNECT or APPROVAL_REPLY.
current_route: ContextVar[str | None] = ContextVar("current_route", default=None)

CHAT_TOOL_LIMIT = 10
# each only when available; track_loop and wake_me carry agreements and reminders (LEARN does not)
CHAT_ALWAYS = ("start_task", "pending", "web_search", "track_loop", "wake_me")
CHAT_EXCLUDED = frozenset({"web_extract"})  # URL fetches would let injected text exfiltrate data
# Sends the user a link by itself: offered only when they ask to connect something (commands.wants_connect).
CONNECT_TOOL = "connect_account"
# Tool focus (track 1 T1.4): the tools of approvals queued, executed or failed within the last FOCUS_TURNS
# user turns stay offered whatever the new message's words ("hi", then "do it without the guest"), and
# a running background task keeps the tools that manage it.
FOCUS_TURNS = 3
FOCUS_APPROVAL_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT, ApprovalStatus.RESOLVING,
                           ApprovalStatus.EXECUTED, ApprovalStatus.FAILED)
TASK_FOCUS_TOOLS = ("list_tasks", "cancel_task")
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
    "for their OK. When a tool answers QUEUED_FOR_APPROVAL, a card with the action and its buttons goes "
    "to them by itself: don't repeat it or ask them to approve or tap anything, and never say it was sent "
    "or done. Only a tool call makes a card; never promise one you didn't queue.\n"
    "- Saying you'll do something is not doing it: call its tool in this same turn, or offer and ask "
    "instead of announcing it. Never say something is done, set or waiting unless a tool said so.\n"
    "- Reminders: wake_me at the exact time they asked for.\n"
    "- When they agree to something you suggested (\"yes\", \"do that\", \"the second one\") or ask you "
    "to remember or remind them of something, call track_loop or wake_me in this same turn with the "
    "concrete item from the conversation, written out in full. Nothing else saves it for them.\n"
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
# Added only when web_search is offered. Web results are untrusted (they taint the turn), so search only
# when the question needs the outside world, not for everything.
WEB_RULE = (
    "- Use web_search when they ask about a specific named real-world person, organisation, product, place, "
    "price, event or news, about anything live or current (prices, scores, weather, opening hours, "
    "what's new), when they want links, product options or where to buy something, or when they ask you "
    "to look something up; then answer from what it finds, with the links it found when they want links. "
    "Anything the user told you needs no search. Otherwise answer normally. If the search finds nothing "
    "clear, say you're not sure. Never invent a biography, and never say you can't browse or look things "
    "up: you can, with web_search.\n"
    "- Links: give only URLs that web_search or web_extract returned, copied exactly. Never build a link "
    "yourself (a shop search page, a guessed product URL). A result marked as a search or listing page is "
    "not a product link: don't call it one. When they want product links and the results hold only search "
    "pages, say so, share the best product page you did find, search again with the product's name, or "
    "open a result with web_extract. Never say a link goes to the exact model unless the result is that "
    "product's own page, and give a price only when the result shows it."
)


# Added only when a tool that creates events is offered (hotfix4 H6).
EVENT_TOOLS = frozenset({"calendar_create_event"})
DURATION_RULE = (
    "- Creating an event or a block when they gave only a start time: don't ask how long. Leave the length "
    "out, so it gets the default of {minutes} minutes, and mention that length in a few words; they can "
    "change it."
)


def chat_tools(user_id: int, query: str = "", *, focus: tuple[str, ...] = (),
               connect: bool = False) -> list[BaseTool]:
    """The tools a chat turn may use (at most CHAT_TOOL_LIMIT, plus any `focus` tools, which are always
    offered). connect_account only when `connect` (the user asked to link an account). Never raises: no
    tools = plain reply."""
    try:
        from mavis.tools.registry import get_registry

        registry = get_registry()
        always = tuple(dict.fromkeys((*CHAT_ALWAYS, *((CONNECT_TOOL,) if connect else ()), *focus)))
        exclude = CHAT_EXCLUDED if connect else CHAT_EXCLUDED | {CONNECT_TOOL}
        tools = registry.select("conversation", user_id, query=query, limit=CHAT_TOOL_LIMIT,
                                always=always, exclude=exclude)
        return _with_companions(registry, user_id, tools)
    except Exception:  # noqa: BLE001 - tools are an extra; the turn must still answer
        log.warning("simple_turn.tools_unavailable", exc_info=True)
        return []


async def focus_tools(user_id: int, history: list[Message]) -> tuple[str, ...]:
    """Tools the conversation is about, whatever the new message says: those of approvals queued,
    executed or failed since the FOCUS_TURNS-th user message before this one, and the task-management
    tools while a background task runs. Never raises (focus is an extra)."""
    try:
        sent = [m.created_at for m in history if m.role == Role.USER.value]
        prior = sent[:-1][-FOCUS_TURNS:]  # the newest user message is the one being answered
        since = prior[0] if prior else (sent[-1] if sent else utcnow())
        names = [a.tool for a in await approvals.touched_since(user_id, since, FOCUS_APPROVAL_STATUSES)]
        if any(t.kind == TaskKind.TASK for t in await tasks.active_for_user(user_id)):
            names += TASK_FOCUS_TOOLS
        return tuple(dict.fromkeys(names))
    except Exception:  # noqa: BLE001
        log.warning("simple_turn.focus_failed", exc_info=True)
        return ()


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
    from mavis.tools.integrations.actions import display_name

    word = commands.connect_word(exc.capability)
    return (f"I need your {display_name(exc.capability)} linked for that. Send /connect {word} and I'll "
            "take it from there.")


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


async def attach_queued_approvals(user_id: int, goal: str, tainted: bool,
                                  turn_ref: str | None = None) -> int | None:
    """Move approvals queued by a chat turn into an APPROVAL task; its gate sends the button prompt.

    Called after the turn's reply is committed, so the explanation lands above the buttons. Also picks
    up approvals left unattached by a turn that crashed before this step."""
    pending = await approvals.unattached_for_user(user_id)
    if not pending:
        return None
    # turn_ref links the approvals to the chat turn that queued them (a failed one blocks that turn's loops)
    task_id = await tasks.create(user_id, goal=goal[:2000], kind=TaskKind.APPROVAL, origin=TaskOrigin.USER,
                                 tainted=tainted, turn_ref=turn_ref)
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


def _is_card(message: ToolMessage) -> bool:
    return _text_of(message.content).startswith(CARD_RESULT_PREFIXES)


SUBSTANTIVE_CHARS = 140


def substantive(prose: str) -> bool:
    """Prose that answers the user rather than pointing at a card: longer than a nudge, or carrying figures.
    A card is additive to such a reply, never a replacement for it."""
    return len(prose) > SUBSTANTIVE_CHARS or any(ch.isdigit() for ch in prose)


def card_only(result: ReactResult, prose: str = "") -> bool:
    """Every tool result of the turn is an approval card (queued, or an updated card shown again) and the
    model's prose only points at it ("tap Approve"): the card says all there is to say. Substantive prose
    (an analysis, an answer) is kept: the card follows it."""
    outputs = [m for m in result.messages if isinstance(m, ToolMessage)]
    return bool(outputs) and all(_is_card(m) for m in outputs) and not substantive(prose)


def _card_shown(result: ReactResult) -> bool:
    return bool(result.queued_approvals) or any(
        isinstance(m, ToolMessage) and _is_card(m) for m in result.messages)


CLAIM_DEADLINE_S = CHAT_DEADLINE_S / 2  # the re-prompt's own budget: it follows a whole turn


async def bind_claims(result: ReactResult, tools: list[BaseTool], text: str, user_id: int, *,
                      self_tainted: bool, sources: list[str] | None = None) -> ReactResult:
    """Action claims are bound to what the turn did (track 1 T1.4, agents.claims).

    A reply that talks about acting while no action tool ran, or points at an approval card that does not
    exist, is re-prompted once with the same tools ("call the tool or say you won't"). The answer to the
    re-prompt is sent as the model wrote it: code never deletes sentences from a reply."""
    reply = strip_stamps(result.text or "").strip()
    if not tools or not reply:
        return result
    from mavis.tools.registry import get_registry

    registry = get_registry()

    def risk_of(name: str):
        tool = registry.find(name)
        return tool.risk if tool is not None else None

    waiting = tuple(dict.fromkeys(a.tool for a in await approvals.open_for_user(user_id)))
    found = claims.check(reply, text, tools, tools_called=result.tools_called,
                         card_shown=_card_shown(result), waiting_tools=waiting, risk_of=risk_of)
    if not found.reprompt:
        return result
    log.info("simple_turn.claim_reprompt", ui_claim=found.ui_claim, tools=found.tools)
    try:
        again = await react_loop(
            tools, [*result.messages, found.note()], CHAT_MAX_STEPS, tier=llm.Tier.FAST, temperature=0.6,
            name="simple_turn_claims", tainted=result.tainted,
            self_tainted=self_tainted or result.read_untrusted, user_words=text, wrap_up=True,
            untrusted_sources=None if result.read_untrusted else sources,
            deadline_s=CLAIM_DEADLINE_S, tool_timeout_s=CHAT_TOOL_TIMEOUT_S,
        )
    except LLMError:
        log.warning("simple_turn.claim_reprompt_failed", exc_info=True)
        again = None
    # replace(): fields this merge does not know about come from the final answer
    merged = result if again is None else dataclasses.replace(
        again, text=again.text or result.text, steps=result.steps + again.steps,
        tools_called=[*result.tools_called, *again.tools_called],
        queued_approvals=[*result.queued_approvals, *again.queued_approvals],
        unqueued_approvals=[*result.unqueued_approvals, *again.unqueued_approvals],
        tainted=result.tainted or again.tainted, read_untrusted=result.read_untrusted or again.read_untrusted,
        wrapped_up=again.wrapped_up,
    )
    return merged


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


def _replies_to_card(approval: PendingApproval, event: Event) -> bool:
    """The user used Telegram's reply on the card's own message."""
    replied = str(event.payload.get("reply_to_text") or "")
    preview = strip_verbatim(approval.preview or "").strip()
    return bool(replied and preview and preview in replied)


def _edit_question_is_latest(history: list[Message]) -> bool:
    return any(approval_flow.EDIT_QUESTION in t for t in _recent_assistant(history, 1))


YES_QUIET_S = 60  # no other assistant message this close before a yes that approves a sensitive card


def _self_only_card(approval: PendingApproval) -> bool:
    from mavis.tools.registry import SELF_ONLY, get_registry

    tool = get_registry().find(approval.tool)
    return tool is not None and tool.risk in SELF_ONLY


def _plain_yes_may_approve(approval: PendingApproval, history: list[Message]) -> bool:
    """May a bare text "yes" approve this card? Self-only cards: yes (the card is the newest message).
    Outward, spending and destructive cards: only when the yes cannot be about something else, i.e. the
    user wrote it after the card was shown, nothing else was said by us in the minute before it, the message
    before the card was not a question (the yes may answer that), the card is clean and was not sent to Edit.
    Otherwise the user is asked to tap Approve. A Telegram reply to the card is always accepted."""
    if _self_only_card(approval):
        return True
    if approval.status != ApprovalStatus.PENDING or approval.tainted or approval.prompted_at is None:
        return False
    users_ = [m for m in history if m.role == Role.USER.value]
    if not users_:
        return False
    said_at = users_[-1].created_at
    if said_at <= approval.prompted_at:
        return False
    before = [m for m in history if m.created_at < said_at and m.role == Role.ASSISTANT.value]
    preview = strip_verbatim(approval.preview or "").strip()
    card = next((m for m in reversed(before) if preview and preview in m.content), None)
    if card is None:
        return False
    others = [m for m in before if m is not card]
    if any((said_at - m.created_at).total_seconds() < YES_QUIET_S for m in others):
        return False
    earlier = [m for m in others if m.created_at <= card.created_at]
    return not (earlier and earlier[-1].content.rstrip().endswith("?"))


TAP_TO_APPROVE = "That one needs a tap, not a text. Tap Approve on the card if you want me to go ahead."


async def _approval_reply(event: Event, user_id: int, text: str, history: list[Message]) -> bool:
    """A text message that decides the card waiting on the user, or False (an ordinary turn).

    Who may decide: a plain yes / no (approval_flow.quick_decision; yes only while the card is the newest
    message). A free-text CHANGE is an edit of the card only when it is plausibly one: the message after
    tapping Edit (the edit question is the newest message), or sent as a reply to the card itself. Anything
    else a pending card sees is a normal message, whatever the model would make of it."""
    approval = await approval_awaiting_reply(user_id, history)
    if approval is None:
        return False
    quick = approval_flow.quick_decision(text)
    on_card = _replies_to_card(approval, event)
    if quick is None:
        editing = approval.status == ApprovalStatus.AWAITING_EDIT and _edit_question_is_latest(history)
        if not (editing or on_card):
            log.info("conversation.card_reply_not_an_edit", approval_id=approval.id)
            return False
    interp = quick or await approval_flow.interpret_reply(approval, text)
    if interp.decision == "approve" and not (prompt_is_latest(approval, history) or on_card):
        log.info("conversation.approve_not_latest", approval_id=approval.id)
        return False  # something was said after the prompt: this "yes" may answer that instead
    waiting = [a for a in await approvals.open_for_user(user_id)
               if a.status in (ApprovalStatus.PENDING, ApprovalStatus.AWAITING_EDIT)]
    if (interp.decision == "approve" and not on_card and len(waiting) == 1  # several: apply_reply asks which
            and not _plain_yes_may_approve(approval, history)):
        log.info("conversation.yes_needs_tap", approval_id=approval.id)
        await approval_flow.say(user_id, TAP_TO_APPROVE, approval_flow.approval_buttons(approval.id),
                                dedupe_key=f"reply:{event.id}:tap")
        current_route.set("APPROVAL_REPLY")
        return True
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


def _reaction_target(event: Event, chat_id: int | None) -> tuple[int, int] | None:
    """(chat_id, message_id) when this turn answers a Telegram message that can carry a reaction."""
    message_id = event.payload.get("message_id")
    if event.source != "telegram" or message_id is None or chat_id is None:
        return None
    return chat_id, int(message_id)


async def _settle_reaction(event: Event, user_id: int, chat_id: int | None, mood: str | None) -> None:
    """Replace the "seen" cue with the mood reaction, or clear it (T1.3). Best effort."""
    target = _reaction_target(event, chat_id)
    if target is not None:
        await reactions.apply(user_id, *target, mood, event.id)


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
        await _settle_reaction(event, user.id, user.telegram_chat_id, None)
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
        await attach_queued_approvals(user.id, text, tainted=tainted,  # it may have died before this
                                      turn_ref=event.id)
        await _settle_reaction(event, user.id, user.telegram_chat_id, None)  # no-op if already settled
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
        await _settle_reaction(event, user.id, user.telegram_chat_id, None)
        return

    if await _approval_reply(event, user.id, text, history):
        await _settle_reaction(event, user.id, user.telegram_chat_id, None)
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
        # Self-only actions (start_task, wake_me, track_loop, ...) are judged by what can steer THIS turn:
        # the reply just before the user's message and the hook context, plus this turn's own reads.
        self_taint = previous_tainted(history) or hooked
        # LEARN sees only the user's text and the previous reply, so its trust keeps the per-turn rule.
        learn_taint = previous_tainted(history) or hooked
        # What an argument must not copy: the tainted replies in the window. Hook context (the inbox digest)
        # has no text here, so then only an argument made of the user's own words is theirs.
        sources = None if hooked else tainted_texts(history)
        now = utcnow()
        name = user.name or card_name
        recent = persona.recent_messages(history, now)
        their_register = register.measure(register.user_texts(history, now),
                                          brief=await register.standing_brief(user.id))
        system = persona.system_prompt(
            user, now, context=context, connections=connections, known_name=name,
            ask_name=persona.should_ask_name(name, history, now, user.timezone),
            prior_turns=max(len(recent) - 1, 0),  # the current message is already in history
            register_line=register.prompt_line(their_register),
        )
        tools = chat_tools(user.id, query=f"{text}\n{previous or ''}",
                           focus=await focus_tools(user.id, history), connect=commands.wants_connect(text))
        if tools:
            system = f"{system}\n\n{TOOL_RULES}"
            if any(t.name == "web_search" for t in tools):
                system = f"{system}\n{WEB_RULE}"
            if any(t.name in EVENT_TOOLS for t in tools):
                minutes = get_settings().default_event_minutes
                system = f"{system}\n{DURATION_RULE.format(minutes=minutes)}"
        reacting = _reaction_target(event, user.telegram_chat_id) is not None
        if reacting:
            system = f"{system}\n\n{reactions.REACTION_RULE}"
        prompt: list[BaseMessage] = [SystemMessage(system)]
        # past reactions replay as their turn's marker line, so the history shows the format in use
        prompt += to_langchain(history, now, user.timezone,
                               reactions=await reactions.landed(user.id) if reacting else None)
        if reacting:
            prompt.append(SystemMessage(reactions.REACTION_REMINDER))

        connect_texts: list[str] = []
        token = current_turn.set(TurnInfo(event_id=event.id))
        try:
            result = await react_loop(
                tools, prompt, CHAT_MAX_STEPS, tier=llm.Tier.FAST, temperature=0.6, name="simple_turn",
                tainted=carried_taint, self_tainted=self_taint, wrap_up=True,
                # the profile name is written only from trusted learning: a term the user owns
                user_words=f"{text} {card_name or ''}",
                untrusted_sources=sources,
                deadline_s=CHAT_DEADLINE_S, tool_timeout_s=CHAT_TOOL_TIMEOUT_S,
            )
            result = await bind_claims(result, tools, text, user.id, self_tainted=self_taint,
                                        sources=sources)
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
            await attach_queued_approvals(user.id, text, tainted=carried_taint, turn_ref=event.id)
            await initiative_hook("quiet.after_assistant_message",
                                  lambda i: i.quiet.after_assistant_message(user.id, connect_texts[-1]))
            current_route.set("CONNECT")
            await _settle_reaction(event, user.id, user.telegram_chat_id, None)
            return
        if result.tools_called:
            log.info("simple_turn.tools", tools=result.tools_called, steps=result.steps,
                     tainted=result.tainted, wrapped_up=result.wrapped_up,
                     queued=result.queued_approvals)
        # replayed messages carry stamps (T1); one echoed at the start of a line is not content
        # the optional mood reaction rides on the reply as a marker line (T1.3); it never reaches the text
        reply, mood = reactions.split_reaction(register.mask_slurs(strip_stamps(result.text or "")))
        reply = reply.strip() or WRAP_UP_FALLBACK
        reply = commands.canonical_commands(reply)  # /connect_google -> the command that exists
        if register.unmirrored(their_register, reply):  # formal, upset or never swore: no swearing (T1.2)
            reply = await register.tone_down(reply)
        bubbles = persona.split_bubbles(reply) or [reply]
        if card_only(result, reply):
            # The card (preview + buttons, rendered by code) is the only prompt: no prose bubble repeats it.
            log.info("simple_turn.card_only", queued=result.queued_approvals)
            bubbles = []

        async with Session() as s:
            for i, bubble in enumerate(bubbles):
                key = f"reply:{event.id}:{i}"
                await outbox.enqueue(s, Outbound(user_id=user.id, text=bubble, dedupe_key=key))
            await s.commit()
    # This turn's own untrusted input marks the reply: a tool read, or the digest it was shown.
    read_untrusted = _read_untrusted(result.tools_called) or result.read_untrusted or hooked
    if bubbles:
        await messages.log(user.id, Role.ASSISTANT, "\n\n".join(bubbles),
                           event_id=reply_event_id(event.id, read_untrusted))
    await enqueue_learn(user.id, event, text, previous, clarified_request(history),
                        tainted=read_untrusted or learn_taint)
    await attach_queued_approvals(user.id, text, tainted=result.tainted, turn_ref=event.id)
    if bubbles:
        await initiative_hook("quiet.after_assistant_message",
                              lambda i: i.quiet.after_assistant_message(user.id, bubbles[-1]))
    current_route.set(_route_for(result.tools_called))
    await _settle_reaction(event, user.id, user.telegram_chat_id, mood)

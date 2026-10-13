"""Slack DMs waiting on the user's reply: after REPLY_AFTER unanswered, Mavis says so and offers a draft.

A DM from someone else opens an entry in users.state[OWED_KEY] and books one wakeup; the user's own message in
that DM closes it (the wakeup then finds nothing to do). The nudge is an ordinary proactive message, so quiet
hours, the daily budget and the user's /channel choice (Telegram, Slack or both) apply. Its "Draft a reply"
button starts a normal chat turn that reads the DM and queues the reply as a card: the Slack text is
third-party content, so sending it always waits for a tap.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import Button
from mavis.domain.wakeups import WakeupKind
from mavis.store.repo import users

log = structlog.get_logger()

OWED_KEY = "slack_owed"  # users.state: {channel id: {"team", "from", "ts", "notified"}}
REPLY_AFTER = timedelta(hours=2)
FORGET_AFTER = timedelta(days=3)  # an unanswered DM older than this is not raised any more
PREFIX = "sr:"
DRAFT, SKIP = "draft", "skip"
_DM_TYPES = frozenset({"im", "dm"})
_NAME = re.compile(r"[^\w .'-]+")


def is_dm(payload: dict[str, Any]) -> bool:
    kind, channel = str(payload.get("channel_type") or ""), str(payload.get("channel") or "")
    return kind in _DM_TYPES or channel.startswith("D")


async def _close(user_id: int, channel: str) -> None:
    await users.modify_nested(user_id, OWED_KEY, lambda cur: {k: v for k, v in cur.items() if k != channel})


def sender_name(payload: dict[str, Any]) -> str:
    """A display name safe to put in Mavis's own words: letters and spaces, short (it is third-party text)."""
    raw = str(payload.get("user_name") or payload.get("from") or "someone")
    name = " ".join(_NAME.sub(" ", raw).split())[:40]
    return name or "someone"


def _ts_time(ts: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


async def _mavis_dm(user_id: int) -> str:
    """The channel id of the user's DM with Mavis itself, which is chat, not a DM to answer."""
    from mavis.channels.routing import slack_chat_for
    from mavis.channels.slack import parse_chat

    route = await slack_chat_for(user_id)
    try:
        return parse_chat(route).channel if route else ""
    except ValueError:
        return ""


async def on_slack_event(event: Event) -> None:
    """Track the DMs the user owes a reply: open on theirs, close on the user's own."""
    if event.type is not EventType.SLACK_MESSAGE:
        return
    p = event.payload
    channel, ts = str(p.get("channel") or ""), str(p.get("ts") or "")
    bot = p.get("bot_id") or str(p.get("subtype") or "").startswith("bot")
    if not channel or not ts or not is_dm(p) or bot:
        return
    if p.get("thread_ts") and p.get("thread_ts") != ts:
        return  # a thread reply inside a DM: the DM itself is what is owed
    if channel == await _mavis_dm(event.user_id):
        return
    if p.get("from_me"):
        await _close(event.user_id, channel)
        return
    sent = _ts_time(ts)
    if sent is None or timeutil.now() - sent > FORGET_AFTER:
        return
    name = sender_name(p)
    opened = False

    def open_entry(cur: dict) -> dict:
        nonlocal opened
        if channel in cur:  # still waiting on the first unanswered message: keep its time
            return cur
        opened = True
        return {**cur, channel: {"team": str(p.get("team") or ""), "from": name, "ts": ts, "notified": False}}

    await users.modify_nested(event.user_id, OWED_KEY, open_entry)
    if not opened:
        return
    from mavis.timers.service import WakeupService

    await WakeupService().wake_me(
        event.user_id, sent + REPLY_AFTER, f"Slack DM from {name} unanswered",
        kind=WakeupKind.SYSTEM_SLACK_REPLY, payload={"channel": channel, "ts": ts}, scale=False,
        dedupe_key=f"slack_reply:{channel}:{ts}")


async def catch_up(provider: Any, user_id: int, *, limit: int = 20) -> int:
    """DMs already waiting when Slack is connected (or this ships): the newest message of each recent DM, when
    it is someone else's, opens an entry as if it had just arrived. Returns how many were opened."""
    from mavis.domain.integrations import UserRef
    from mavis.tools.integrations.native.slack_events import build_event, conversation_order
    from mavis.tools.integrations.normalize import extract_list

    res = await provider.execute(UserRef(user_id=user_id), "slack.channels", {})
    if not res.ok:
        return 0
    dms = [c for c in conversation_order(extract_list(res.data, "channels", "data.channels"))
           if c.get("kind") == "dm"][:limit]
    before = len(await _owed_now(user_id))
    oldest = f"{(timeutil.now() - FORGET_AFTER).timestamp():.6f}"  # older DMs are never raised
    for chan in dms:
        cid = str(chan.get("id") or "")
        page = await provider.execute(UserRef(user_id=user_id), "slack.history",
                                      {"channel": cid, "limit": 1, "oldest": oldest})
        rows = ((page.data or {}).get("messages") or []) if page.ok else []
        messages = [m for m in rows if isinstance(m, dict)]
        if not messages:
            continue
        newest = messages[0]
        event = build_event(user_id, newest, cid, "catch_up", from_me=bool(newest.get("from_me")),
                            channel_type="dm", channel_name=str(chan.get("name") or ""))
        if event is not None and not newest.get("from_me"):
            await on_slack_event(event)
    return len(await _owed_now(user_id)) - before


async def _owed_now(user_id: int) -> dict:
    return (await users.get_state(user_id)).get(OWED_KEY) or {}


def nudge_buttons(channel: str) -> list[list[Button]]:
    return [[Button(label="Draft a reply", data=f"{PREFIX}{DRAFT}:{channel}"),
             Button(label="Leave it", data=f"{PREFIX}{SKIP}:{channel}")]]


async def on_wakeup(user_id: int, reason: str, payload: dict[str, Any]) -> None:
    """Still unanswered: tell the user, with the offer to draft a reply."""
    channel, ts = str(payload.get("channel") or ""), str(payload.get("ts") or "")
    entry = ((await users.get_state(user_id)).get(OWED_KEY) or {}).get(channel)
    if not isinstance(entry, dict) or entry.get("ts") != ts or entry.get("notified"):
        return  # they replied (or it was already raised)
    sent = _ts_time(ts)
    if sent is None or timeutil.now() - sent > FORGET_AFTER:
        return
    from mavis.domain.decisions import NotifyIntent
    from mavis.initiative.wiring import current

    user = await users.get(user_id)
    hours = max(1, round((timeutil.now() - sent).total_seconds() / 3600))
    who = entry.get("from") or "Someone"
    intent = (f"{who} sent them a Slack DM about {hours} hour(s) ago and they have "
              "not replied. Say that plainly in one or two short lines and offer to draft a reply they can "
              "send (the buttons do it). Do not guess what the DM says.")
    sent_ok = await current().executor.notify(
        user, NotifyIntent(urgency=3, intent=intent, dedupe_key=f"slack_reply:{channel}:{ts}"),
        untrusted=True, buttons=nudge_buttons(channel))
    if sent_ok:
        await users.modify_nested(user_id, OWED_KEY, lambda cur: {
            **cur, channel: {**cur[channel], "notified": True}} if channel in cur else cur)
    log.info("slack_replies.nudged", user_id=user_id, sent=sent_ok)


async def on_button(event: Event, data: str) -> None:
    """Draft: run a normal chat turn about that DM, answered where the button was tapped. Leave it: forget."""
    from mavis.bus import get_bus

    _, action, channel = (data.split(":", 2) + ["", ""])[:3]
    entry = ((await users.get_state(event.user_id)).get(OWED_KEY) or {}).get(channel) or {}
    await _close(event.user_id, channel)
    if action != DRAFT:
        return
    name = str(entry.get("from") or "them")
    text = (f"Draft a reply to the Slack DM {name} sent me (Slack conversation {channel}): read what they "
            "wrote, write a short reply in my voice, and queue it to send in that DM for my approval.")
    body: dict[str, Any] = {"text": text}
    if event.payload.get("reply_chat"):
        body["reply_chat"] = event.payload["reply_chat"]
    turn = Event(id=f"{event.id}:draft", user_id=event.user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source=event.source, payload=body, trust=Trust.USER)
    await get_bus().publish(turn)


def register() -> None:
    from mavis.agents.buttons import register_button_handler
    from mavis.timers.system import register_system_wakeup
    from mavis.worker.runner import register_event_handler

    register_event_handler(EventType.SLACK_MESSAGE, on_slack_event)
    register_system_wakeup(WakeupKind.SYSTEM_SLACK_REPLY.value, on_wakeup, with_payload=True)
    register_button_handler(PREFIX, on_button)

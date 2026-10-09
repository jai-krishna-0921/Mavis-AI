"""Owner and user commands handled before routing, never through the LLM (spec 11). An event gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from mavis.agents.commands import parse_command
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Outbound, Role
from mavis.store.models import User
from mavis.store.repo import messages, outbox, users
from mavis.worker.gates import register_event_gate

log = structlog.get_logger(__name__)
CommandFn = Callable[[Event, User, list[str]], Awaitable[str | None]]
_owner: dict[str, CommandFn] = {}
_user: dict[str, CommandFn] = {}


def register_owner_command(name: str, fn: CommandFn) -> None:
    _owner[name] = fn


def register_user_command(name: str, fn: CommandFn) -> None:
    _user[name] = fn


def is_owner(user: User) -> bool:
    return user.tier == "owner" and user.telegram_chat_id in get_settings().owner_telegram_chat_ids


def parse_kv(args: list[str]) -> tuple[dict[str, str], list[str]]:
    kv: dict[str, str] = {}
    rest: list[str] = []
    for a in args:
        k, sep, v = a.partition("=")
        if sep and k.isidentifier():
            kv[k.lower()] = v
        else:
            rest.append(a)
    return kv, rest


async def command_gate(event: Event) -> bool:
    if event.type is not EventType.USER_MESSAGE:
        return True
    parsed = parse_command(str(event.payload.get("text", "")))
    if parsed is None:
        return True
    name, args = parsed
    user = await users.get(event.user_id)
    fn = _owner.get(name) if is_owner(user) else None
    fn = fn or _user.get(name)
    if fn is None:
        return True  # not ours: agents.commands (connect, ...) and the chat turn handle it
    reply = await fn(event, user, args)
    if reply:
        await outbox.enqueue_now(Outbound(user_id=user.id, text=reply, dedupe_key=f"cmd:{event.id}"))
        await messages.log(user.id, Role.ASSISTANT, reply, event_id=f"reply:{event.id}")
    return False


def register_command_gate() -> None:
    register_event_gate("commands", command_gate, order=20)

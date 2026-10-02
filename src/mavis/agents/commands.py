"""Slash commands handled before routing, plus the CONNECT route's natural-language handler."""

from __future__ import annotations

import re

from mavis.domain.events import Event
from mavis.domain.messages import Role
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import DISPLAY_NAMES, INTEGRATION_CAPABILITIES
from mavis.tools.integrations.connect_flow import ConnectFlow

NOT_CONFIGURED_TEXT = "Connections aren't set up on this Mavis yet."
COMMANDS = ("connect", "connections", "disconnect")

_KEYWORDS: tuple[tuple[re.Pattern[str], Capability], ...] = (
    (re.compile(r"\b(g?cal(endar)?|meetings?|schedule)\b", re.I), Capability.CALENDAR),
    (re.compile(r"\b(gmail|e-?mails?|mail|inbox)\b", re.I), Capability.GMAIL),
    (re.compile(r"\bslack\b", re.I), Capability.SLACK),
    (re.compile(r"\bnotion\b", re.I), Capability.NOTION),
)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def capability_from_text(text: str) -> Capability | None:
    for pattern, capability in _KEYWORDS:
        if pattern.search(text or ""):
            return capability
    return None


def _flow(flow: ConnectFlow | None) -> ConnectFlow:
    if flow is not None:
        return flow
    from mavis.tools.integrations.wiring import get_connect_flow

    return get_connect_flow()


def _configured(flow: ConnectFlow) -> bool:
    """Providers without a `configured` flag (fakes, future adapters) count as configured."""
    return bool(getattr(flow.provider, "configured", True))


async def run_command(event: Event, flow: ConnectFlow | None = None) -> bool:
    parsed = parse_command(str(event.payload.get("text", "")))
    if parsed is None:
        return False
    name, args = parsed
    if name not in COMMANDS:
        return False
    f = _flow(flow)
    with f.reply_scope(event.id) as scope:
        await _run(f, event, name, args)
    if scope.texts:
        # log the answer like a normal turn does (deduped by event id), so history isn't missing it
        from mavis.store.repo import messages

        await messages.log(event.user_id, Role.ASSISTANT, "\n\n".join(scope.texts),
                           event_id=f"reply:{event.id}")
    return True


async def _run(f: ConnectFlow, event: Event, name: str, args: list[str]) -> None:
    if not _configured(f):
        await f.send(event.user_id, NOT_CONFIGURED_TEXT)
        return
    capability = capability_from_text(" ".join(args)) if args else None
    if name == "connect":
        if capability is not None:
            await f.start(event.user_id, capability, "")
        else:
            await f.offer_menu(event.user_id)
    elif name == "connections":
        await f.send(event.user_id, await f.status_text(event.user_id))
    elif capability is None or capability not in INTEGRATION_CAPABILITIES:
        options = ", ".join(DISPLAY_NAMES[c].split()[-1].lower() for c in INTEGRATION_CAPABILITIES)
        await f.send(event.user_id, f"Which one? e.g. /disconnect gmail ({options})")
    else:
        await f.disconnect(event.user_id, capability)


async def handle_connect(user_id: int, text: str, flow: ConnectFlow | None = None) -> str | None:
    """Natural-language connect request (Phase 4's CONNECT route; not routed in the slice).

    Sends the link or menu itself and returns no reply text.
    """
    f = _flow(flow)
    if not _configured(f):
        await f.send(user_id, NOT_CONFIGURED_TEXT)
        return None
    capability = capability_from_text(text)
    if capability is None:
        await f.offer_menu(user_id)
    else:
        await f.start(user_id, capability, "")
    return None

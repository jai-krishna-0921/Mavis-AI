"""Slash commands handled before routing, plus the CONNECT route's natural-language handler."""

from __future__ import annotations

import difflib
import re

from mavis.domain.events import Event
from mavis.domain.messages import Role
from mavis.domain.policy import Capability
from mavis.domain.terms import terms
from mavis.tools.integrations.actions import (
    DISPLAY_NAMES,
    GOOGLE_ANCHOR,
    active_capabilities,
    is_google,
    workspace_enabled,
)
from mavis.tools.integrations.composio_map import LEGACY_ALIASES
from mavis.tools.integrations.connect_flow import ConnectFlow

NOT_CONFIGURED_TEXT = "Connections aren't set up on this Mavis yet."
COMMANDS = ("connect", "connections", "disconnect", "mute", "unmute", "channel")
MUTE_COMMANDS = ("mute", "unmute")

_KEYWORDS: tuple[tuple[re.Pattern[str], Capability], ...] = (
    (re.compile(r"\b(g?cal(endar)?|meetings?|schedule)\b", re.I), Capability.CALENDAR),
    (re.compile(r"\b(gmail|e-?mails?|mail|inbox)\b", re.I), Capability.GMAIL),
    (re.compile(r"\bslack\b", re.I), Capability.SLACK),
    (re.compile(r"\bnotion\b", re.I), Capability.NOTION),
)


KNOWN_COMMANDS = ("start", *COMMANDS)  # what the bot answers to; /start is Telegram's own
# Words a model uses for "connect" that are not commands of ours.
_CONNECT_SYNONYMS = frozenset({"link", "relink", "reconnect", "auth", "authorize", "authorise", "login",
                               "signin", "integrate", "integrations"})
_SLASH = re.compile(r"(?<![\w/:.@\\])/([A-Za-z][A-Za-z_-]*)(?:([^\S\n]+)([A-Za-z][\w-]*))?")


def connect_word(capability: Capability) -> str:
    """The word /connect takes for a capability: what capability_from_text reads back as it."""
    if is_google(capability) and workspace_enabled():
        return "google"
    return "calendar" if capability is Capability.CALENDAR else capability.value


def _service(word: str) -> Capability | None:
    return capability_from_text(word) if word and word.lower() not in LEGACY_ALIASES else None


def _command_of(name: str) -> tuple[str, str] | None:
    """(real command, service word glued to it) for a slash word that is NOT one of ours but plainly
    means one, or None. A known command is never passed here: it is left exactly as written."""
    low = name.lower()
    head, sep, tail = re.split(r"([_-])", low, maxsplit=1) if re.search(r"[_-]", low) else (low, "", "")
    # Telegram-style /connect_google: the part after "_" is the argument, kept even when it is not a
    # service here (/connect google then opens the menu); after "-" only a service splits (/connect-four
    # is not ours)
    if head in KNOWN_COMMANDS and tail and (sep == "_" or _service(tail) is not None):
        return head, tail
    for cmd in ("disconnect", "connect"):  # glued service: /connectgmail
        if low.startswith(cmd) and _service(low[len(cmd):]) is not None:
            return cmd, low[len(cmd):]
    close = difflib.get_close_matches(low, KNOWN_COMMANDS, n=1, cutoff=0.8)  # /connection, /conect
    if close:
        return close[0], ""
    return ("connect", "") if low in _CONNECT_SYNONYMS else None


# Code spans, fenced blocks and verbatim spans are shown as written: commands inside them are never touched.
_KEEP_AS_IS = re.compile("```.*?(?:```|$)|`[^`\n]+`|\x0e.*?(?:\x0f|$)", re.DOTALL)


def canonical_commands(text: str) -> str:
    """Slash commands in a reply are real ones (track 1 T1.4, hotfix4 H6). Only a slash token that is not
    a command of ours but plainly means one is rewritten, and only that token: /connect_google ->
    /connect google, /connectgmail -> /connect gmail, /connection -> /connections, /link -> /connect.
    Known commands, their arguments, everything after the token, code and verbatim spans stay as
    written; slash words not near any command ("and/or", a path) are left alone."""

    def fix(m: re.Match[str]) -> str:
        if m.group(1).lower() in KNOWN_COMMANDS:
            return m.group(0)
        found = _command_of(m.group(1))
        if found is None:
            return m.group(0)
        cmd, glued = found
        if (cap := _service(glued)) is not None:
            glued = connect_word(cap)
        arg = f" {glued}" if glued else ""
        return f"/{cmd}{arg}{m.group(2) or ''}{m.group(3) or ''}"

    out, last = [], 0
    for held in _KEEP_AS_IS.finditer(text or ""):
        out.append(_SLASH.sub(fix, text[last:held.start()]))
        out.append(held.group(0))
        last = held.end()
    out.append(_SLASH.sub(fix, (text or "")[last:]))
    return "".join(out)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    return parts[0].split("@", 1)[0].lower(), parts[1:]


# With Workspace on, every Google word opens the one Google consent (spec 3.3).
_GOOGLE_WORDS = re.compile(
    r"\b(google|workspace|g?suite|drive|docs?|sheets?|spreadsheets?|tasks?|to-?dos?|contacts?|meet)\b", re.I
)


def capability_from_text(text: str) -> Capability | None:
    if workspace_enabled():
        for pattern, capability in _KEYWORDS:  # Slack and Notion first: "notion docs" is Notion
            if not is_google(capability) and pattern.search(text or ""):
                return capability
        if _GOOGLE_WORDS.search(text or "") or any(p.search(text or "") for p, _ in _KEYWORDS):
            return GOOGLE_ANCHOR  # only Google keywords are left (gmail, calendar...)
        return None
    for pattern, capability in _KEYWORDS:
        if pattern.search(text or ""):
            return capability
    return None


def capabilities_named(text: str) -> set[Capability]:
    """Every account the text names (an email, the calendar, Slack, Notion, a Google app). What a user
    asked for is what they named: a capability outside this set was never requested."""
    named = {capability for pattern, capability in _KEYWORDS if pattern.search(text or "")}
    if _GOOGLE_WORDS.search(text or ""):
        named.add(GOOGLE_ANCHOR)
    return named


def capability_requested(capability: Capability, text: str) -> bool:
    """Did the text ask for this account? Google capabilities count as one family when Workspace is on."""
    named = capabilities_named(text)
    if capability in named:
        return True
    return workspace_enabled() and is_google(capability) and any(is_google(c) for c in named)


# The connect tool's own vocabulary (registry-style term overlap, domain.terms): asking to link something.
_CONNECT_VERBS = terms("connect reconnect link relink unlink authorize authorise integrate")
_CONNECT_OBJECTS = terms("account connection integration")


def wants_connect(text: str) -> bool:
    """The message asks to link an account: a connect verb AND a service it names (or "account").

    connect_account sends the user a link by itself, so it is offered to a chat turn only then (track 1
    T1.4); a missing link found while acting goes through ConnectionRequired instead. "Amazon links"
    has the verb's word but names no service, so it is not a connect request."""
    words = terms(text or "")
    if not words & _CONNECT_VERBS:
        return False
    return capability_from_text(text) is not None or bool(words & _CONNECT_OBJECTS)


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


async def run_mute(f: ConnectFlow, user_id: int, name: str, args: list[str]) -> None:
    """/mute and /unmute: keep a sender, domain or Slack channel or user out of what Mavis learns from."""
    from mavis.tools.integrations.native import guard

    if not args:
        if name == "unmute":
            await f.send(user_id, "Which one? e.g. /unmute news@example.com, /unmute example.com "
                                  "or /unmute C0123ABCD")
        else:
            await f.send(user_id, guard.describe_mute(await guard.load_mute(user_id)))
        return
    target = " ".join(args)
    done = await (guard.add_mute if name == "mute" else guard.remove_mute)(user_id, target)
    if done is None:
        await f.send(user_id, "I can mute a sender address, a domain, or a Slack channel or user id "
                              "(like C0123ABCD). Which one did you mean?")
        return
    kind, value = done
    what = {"senders": "mail from", "domains": "mail from the domain", "slack": "Slack messages from"}[kind]
    if name == "mute":
        await f.send(user_id, f"Muted. I won't learn from {what} {value}. /unmute {value} undoes it.")
    else:
        await f.send(user_id, f"Unmuted {value}. I'll learn from {what} {value} again.")


CHANNEL_NAMES = {"telegram": "Telegram", "slack": "Slack", "both": "Telegram and Slack"}


async def run_channel(f: ConnectFlow, user_id: int, args: list[str]) -> None:
    """/channel [telegram|slack|both]: where proactive messages (briefs, reminders, heads-ups) go. Replies
    always go back to the channel you wrote from."""
    from mavis.channels import routing
    from mavis.store.repo import users

    current = routing.pref_of(await users.get_state(user_id))
    choice = (args[0].lower() if args else "")
    if choice not in routing.PREFS:
        await f.send(user_id, f"I send my own messages to {CHANNEL_NAMES[current]}. Replies always come back "
                              "where you wrote from. Change it with /channel telegram, /channel slack or "
                              "/channel both.")
        return
    if choice != "telegram" and await routing.slack_chat_for(user_id) is None:
        await f.send(user_id, "Slack isn't set up for chatting yet. Connect Slack with /connect slack "
                              "(approve the bot too), then try again.")
        return
    await routing.set_pref(user_id, choice)
    await f.send(user_id, f"Done. My own messages now go to {CHANNEL_NAMES[choice]}.")


async def _run(f: ConnectFlow, event: Event, name: str, args: list[str]) -> None:
    if name == "channel":
        await run_channel(f, event.user_id, args)
        return
    if name in MUTE_COMMANDS:
        await run_mute(f, event.user_id, name, args)
        return
    if not _configured(f):
        await f.send(event.user_id, NOT_CONFIGURED_TEXT)
        return
    words = " ".join(args).strip().lower()
    if name == "disconnect" and workspace_enabled() and words in LEGACY_ALIASES:
        await f.disconnect_legacy(event.user_id, words)
        return
    capability = capability_from_text(words) if args else None
    if name == "connect":
        if capability is not None:
            await f.start(event.user_id, capability, "")
        else:
            await f.offer_menu(event.user_id)
    elif name == "connections":
        await f.send(event.user_id, await f.status_text(event.user_id))
    elif capability is None or capability not in active_capabilities():
        if workspace_enabled():
            await f.send(event.user_id, "Which one? e.g. /disconnect google (google, slack, notion, "
                                        "gmail-legacy, calendar-legacy)")
        else:
            options = ", ".join(DISPLAY_NAMES[c].split()[-1].lower() for c in active_capabilities())
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

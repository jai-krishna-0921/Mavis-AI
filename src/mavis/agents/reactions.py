"""Mood reactions on the user's message (T1.3).

The chat model may end its reply with one marker line, `[react: <emoji>]`, in the same call that writes
the reply (no extra LLM call). Code then decides whether it lands:
- the emoji must be on Mavis's list (a subset of Telegram's bot-allowed reactions); anything else drops;
- frequency: never on two messages in a row, at most one in any three, and never the same emoji three
  times running (ReactionLog keeps the last few outcomes per user);
- the instant "seen" reaction (PRESENCE_REACTION, optional) is replaced by the mood reaction, or cleared
  when there is none or Telegram rejects it.
The marker is always stripped from the reply text, valid or not.
"""

from __future__ import annotations

import re
from collections import deque

import structlog

from mavis.bus import get_redis
from mavis.channels import presence
from mavis.config import get_settings

log = structlog.get_logger(__name__)

_VS16 = "️"

# What each reaction is for (prompt guidance; the model picks). Every emoji is written exactly as in
# Telegram's ReactionTypeEmoji list (a test checks the set stays inside it).
MOODS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("a win, a milestone, something done", ("\U0001f525", "\U0001f3c6", "\U0001f389")),
    ("progress or a good effort", ("\U0001f44f", "\U0001f4af", "\U0001f44d")),
    ("hype and excitement", ("⚡", "\U0001f929", "❤️‍\U0001f525")),
    ("something funny", ("\U0001f601", "\U0001f923")),
    ("thanks", ("\U0001f91d", "\U0001f64f", "\U0001fae1")),
    ("warmth, only when your reply is warm too", ("❤", "\U0001f917")),
    ("a tricky question or a nerdy one", ("\U0001f914", "\U0001f913")),
    ("something impressive or mind-blowing", ("\U0001f92f", "\U0001f60e")),
    ("noted, on it", ("✍", "\U0001f44c", "\U0001f440")),
    ("good night", ("\U0001f634",)),
)
ALLOWED = frozenset(e for _, emoji in MOODS for e in emoji)
_CANONICAL = {e.replace(_VS16, ""): e for e in ALLOWED}

REACTION_RULE = (
    "Reacting\n"
    "- Like a friend tapping a reaction, you can react to their message with one emoji. Most messages "
    "get none, but real good news, a laugh or a thank-you usually earns one. Never when they're upset, "
    "stressed, angry with you or in a crisis, never on health, money trouble or bad news, and not on "
    "plain formal requests.\n"
    "- To react, end your reply with one last line: [react: EMOJI]. Use only these: "
    + "; ".join(f"{label} {' '.join(emoji)}" for label, emoji in MOODS)
    + ". Otherwise add nothing."
)

REACTION_REMINDER = (
    "[System note, not from the user] If a reaction fits their last message, end your reply with "
    "[react: EMOJI]; most messages get none."
)


_FENCE = re.compile(r"^\s*```")
# Tolerant: any bracketed react-like marker ("[react 🔥]", "(reaction: 🔥)", "[React = 🔥]") anywhere on a
# line, or a bare "react: 🔥" line on its own whose value is not words ("Reaction: exothermic" is prose).
_MARKER = re.compile(
    r"[^\S\n]*[\[(]\s*react(?:ion|ing)?\s*[:=\-]?\s*([^\])\n]{0,24}?)\s*[\])][^\S\n]*", re.IGNORECASE
)
_BARE = re.compile(r"^\s*react(?:ion|ing)?\s*[:=]\s*([^\sA-Za-z0-9][^\s]{0,11}|none)\s*$", re.IGNORECASE)


def normalize(raw: str | None) -> str | None:
    """The allowed reaction `raw` names (in Telegram's spelling), or None."""
    if not raw:
        return None
    return _CANONICAL.get(raw.strip().replace(_VS16, ""))


def split_reaction(text: str) -> tuple[str, str | None]:
    """(reply without reaction markers, the last valid reaction or None). Markers inside ``` blocks are
    content and stay."""
    out: list[str] = []
    found: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (bare := _BARE.match(line)):
            found.append(bare.group(1))
            continue
        elif not in_fence and _MARKER.search(line):
            found += _MARKER.findall(line)
            line = _MARKER.sub(" ", line).strip()
            if not line:
                continue
        out.append(line)
    valid = [e for e in (normalize(f) for f in found) if e]
    return "\n".join(out).strip(), (valid[-1] if valid else None)


GAP = 2  # after a reaction, this many messages go without one
REPEAT = 2  # the same emoji at most this many times running
HISTORY = 20  # outcomes kept per user: the frequency rule and the replayed history (HISTORY_LIMIT)
_TTL_S = 3 * 24 * 3600


def allowed(candidate: str | None, recent: list[str]) -> bool:
    """Whether `candidate` may land given the user's recent outcomes (oldest first)."""
    if candidate is None or any(recent[-GAP:]):
        return False
    landed = [r for r in recent if r]
    return not (len(landed) >= REPEAT and all(r == candidate for r in landed[-REPEAT:]))


class ReactionLog:
    """The last HISTORY reaction outcomes per user, one per answered message (event id): Redis when
    configured, else this process. Entries are "event_id<TAB>emoji", newest last; "" = nothing landed."""

    def __init__(self) -> None:
        self._mem: dict[int, deque[str]] = {}

    @staticmethod
    def _key(user_id: int) -> str:
        return f"mavis:reactions:{user_id}"

    async def _entries(self, user_id: int) -> list[tuple[str, str]]:
        raw: list[str] | None = None
        client = get_redis()
        if client is not None:
            try:
                raw = list(reversed(await client.lrange(self._key(user_id), 0, HISTORY - 1)))
            except Exception as exc:  # noqa: BLE001 - fall back to memory if redis blips
                log.debug("reactions.redis_failed", error=type(exc).__name__)
        if raw is None:
            raw = list(self._mem.get(user_id, ()))
        return [(e.partition("\t")[0], e.partition("\t")[2]) for e in raw]

    async def recent(self, user_id: int) -> list[str]:
        """Outcomes, oldest first."""
        return [emoji for _, emoji in await self._entries(user_id)]

    async def landed(self, user_id: int) -> dict[str, str]:
        """event id -> the reaction that landed on that message."""
        return {key: emoji for key, emoji in await self._entries(user_id) if emoji}

    async def outcome(self, user_id: int, event_id: str) -> str | None:
        """What was recorded for `event_id` ("" for nothing), or None when it was never settled."""
        return dict(await self._entries(user_id)).get(event_id)

    async def record(self, user_id: int, event_id: str, outcome: str) -> None:
        """Record once per event: a retry of the same message keeps the first outcome."""
        if await self.outcome(user_id, event_id) is not None:
            return
        entry = f"{event_id}\t{outcome}"
        client = get_redis()
        if client is not None:
            try:
                key = self._key(user_id)
                await client.lpush(key, entry)
                await client.ltrim(key, 0, HISTORY - 1)
                await client.expire(key, _TTL_S)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("reactions.redis_failed", error=type(exc).__name__)
        self._mem.setdefault(user_id, deque(maxlen=HISTORY)).append(entry)


_log = ReactionLog()


async def landed(user_id: int) -> dict[str, str]:
    """event id -> reaction that landed, for replaying past reactions in the chat history."""
    try:
        return await _log.landed(user_id)
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.debug("reactions.landed_failed", error=type(exc).__name__)
        return {}


async def apply(user_id: int, chat_id: int | str, message_id: int, candidate: str | None,
                event_id: str) -> str:
    """Land the mood reaction on the message of `event_id` (or clear the "seen" one). Returns what landed
    ("" for nothing). Settles once per event: a retried turn keeps the first outcome. Never raises."""
    try:
        await presence.ack_settled(event_id)  # a late "seen" cue must not overwrite the mood
        settled = await _log.outcome(user_id, event_id)
        if settled is not None:
            return settled
        recent = await _log.recent(user_id)
        chosen = candidate if allowed(candidate, recent) else None
        if candidate and chosen is None:
            log.info("reactions.rate_limited", emoji=candidate)
        landed = ""
        if chosen and await presence.react(chat_id, message_id, chosen):
            landed = chosen
        if not landed and get_settings().presence_reaction:
            await presence.clear(chat_id, message_id)  # the "seen" cue has done its job
        await _log.record(user_id, event_id, landed)
        log.info("reactions.settled", chosen=bool(candidate), landed=bool(landed))
        return landed
    except Exception as exc:  # noqa: BLE001 - cosmetic, must not fail the turn
        log.warning("reactions.apply_failed", error=type(exc).__name__)
        return ""

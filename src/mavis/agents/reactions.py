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

_FENCE = re.compile(r"^\s*```")
_MARKER = re.compile(r"[^\S\n]*\[\s*react(?:ion)?\s*:\s*([^\]\n]{0,24}?)\s*\][^\S\n]*", re.IGNORECASE)


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
HISTORY = 6  # outcomes kept per user (oldest first), "" when nothing landed
_TTL_S = 3 * 24 * 3600


def allowed(candidate: str | None, recent: list[str]) -> bool:
    """Whether `candidate` may land given the user's recent outcomes (oldest first)."""
    if candidate is None or any(recent[-GAP:]):
        return False
    landed = [r for r in recent if r]
    return not (len(landed) >= REPEAT and all(r == candidate for r in landed[-REPEAT:]))


class ReactionLog:
    """The last HISTORY reaction outcomes per user: Redis when configured, else this process."""

    def __init__(self) -> None:
        self._mem: dict[int, deque[str]] = {}

    @staticmethod
    def _key(user_id: int) -> str:
        return f"mavis:reactions:{user_id}"

    async def recent(self, user_id: int) -> list[str]:
        client = get_redis()
        if client is not None:
            try:
                return list(reversed(await client.lrange(self._key(user_id), 0, HISTORY - 1)))
            except Exception as exc:  # noqa: BLE001 - fall back to memory if redis blips
                log.debug("reactions.redis_failed", error=type(exc).__name__)
        return list(self._mem.get(user_id, ()))

    async def record(self, user_id: int, outcome: str) -> None:
        client = get_redis()
        if client is not None:
            try:
                key = self._key(user_id)
                await client.lpush(key, outcome)
                await client.ltrim(key, 0, HISTORY - 1)
                await client.expire(key, _TTL_S)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("reactions.redis_failed", error=type(exc).__name__)
        self._mem.setdefault(user_id, deque(maxlen=HISTORY)).append(outcome)


_log = ReactionLog()


async def apply(user_id: int, chat_id: int, message_id: int, candidate: str | None,
                ack_key: str | None = None) -> str:
    """Land the mood reaction (or clear the "seen" one). Returns what landed ("" for nothing). Never
    raises: reactions are cosmetic. `ack_key`: the event whose "seen" cue must land first."""
    try:
        if ack_key is not None:
            await presence.ack_settled(ack_key)  # a late "seen" cue must not overwrite the mood
        recent = await _log.recent(user_id)
        chosen = candidate if allowed(candidate, recent) else None
        if candidate and chosen is None:
            log.info("reactions.rate_limited", emoji=candidate)
        landed = ""
        if chosen and await presence.react(chat_id, message_id, chosen):
            landed = chosen
        if not landed and get_settings().presence_reaction:
            await presence.clear(chat_id, message_id)  # the "seen" cue has done its job
        await _log.record(user_id, landed)
        return landed
    except Exception as exc:  # noqa: BLE001 - cosmetic, must not fail the turn
        log.warning("reactions.apply_failed", error=type(exc).__name__)
        return ""

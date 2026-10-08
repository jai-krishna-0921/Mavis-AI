"""The user's register (how they talk), measured from their own recent messages (T1.2).

The model is told how the user talks right now so it can mirror it: casual or formal, sweary or clean,
hyped or calm. The word lists here are measured signals that feed the prompt, never reply text: the
model writes its own words. Persona rules (agents/persona.py) set the limits: never insult the user,
never slurs, never sexual content, step down when they are upset.

Two guards on outgoing text: `mask_slurs` (a slur never reaches the user, whatever the model wrote) and
`tone_down` (one rewrite call, only when a reply swears although their register does not allow it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from mavis.llm import models as llm

log = structlog.get_logger(__name__)

WINDOW = timedelta(hours=12)  # the "recent" register; older talk does not set today's tone
SAMPLE = 8  # user messages measured
SWEAR_RECENT = 3  # swearing is mirrored only when they swore in one of their last few messages

# Profanity (English, Hinglish). A token matches when it is one of these words or forms, or the text
# has a masked spelling ("f*ck", "sh!t"). Substrings never count ("shitake", "Scunthorpe", "class").
_SWEAR_WORDS = frozenset({
    "ass", "arse", "asshole", "arsehole", "bastard", "bitch", "bitching", "bloody", "bollocks",
    "bullshit", "crap", "crappy", "hell", "damn", "damned", "dammit", "dick", "dickhead", "goddamn", "piss",
    "pissed", "prick", "screwed", "sod", "wtf", "ffs", "stfu", "omfg", "fml", "af", "jfc",
    "bc", "mc", "bkl", "bsdk", "chutiya", "chutiye", "saala", "saale", "kamina", "harami",
})
_SWEAR_FORMS = re.compile(
    r"(?:mother)?f+u+c*k+\w*|fuk\w*|fck\w*|(?:bull|horse|dip|dog)?sh+i+t+(?:s|ty|tier|tiest|ting|ted|head|"
    r"show|load|hole|e|ey)?|(?:dumb|jack|bad|smart|lame|kick)ass(?:es)?|ass(?:holes?|hats?)|(?:god)?damn\w*",
)
_MASKED = re.compile(r"\b(?:f[\W_]{1,3}(?:c?k|ck)\w*|sh[\W_]{1,2}t\w*|b[\W_]tch\w*)", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z]+", re.IGNORECASE)

# Formality and casualness markers (measured, never echoed).
_POLITE = re.compile(
    r"\b(?:please|kindly|could you|would you|would it be possible|may i|dear|regards|sincerely|"
    r"thank you|i would appreciate|appreciate it|good (?:morning|afternoon|evening))\b",
    re.IGNORECASE,
)
_SLANG = frozenset({
    "lol", "lmao", "lmfao", "rofl", "bro", "bruh", "dude", "yaar", "yo", "ya", "yeah", "yep", "nah",
    "gonna", "wanna", "gotta", "kinda", "sorta", "u", "ur", "pls", "plz", "thx", "ty", "idk", "imo",
    "tbh", "ngl", "omg", "haha", "hahaha", "hehe", "okie", "k", "kk", "sup", "hey", "cool", "dope",
    "fr", "btw", "rn", "nvm", "ikr", "smh", "ugh", "meh", "yup", "cmon", "lets",
})
_ELONGATED = re.compile(r"([a-z])\1{2,}", re.IGNORECASE)  # "gooo", "yesss"
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")

# Distress: a hard moment in the latest message (loss, illness, fear, a job or a relationship ending).
# A measured signal only: it turns swearing and jokes off for this reply; the model reads the rest.
_DISTRESS = re.compile(
    r"\b(?:died|dying|death|passed away|funeral|hospital|icu|stroke|cancer|tumou?r|biopsy|"
    r"heart attack|accident|surgery|emergency|diagnos\w*|miscarriage|suicid\w*|self[- ]harm|"
    r"scared|terrified|afraid|panic\w*|anxious|anxiety|depress\w*|crying|cried|in tears|grief|grieving|"
    r"heartbroken|broke up|break ?up|divorce|laid off|got fired|lost my (?:job|mom|mum|dad|father|mother)|"
    r"can'?t (?:cope|breathe|think straight)|falling apart)\b",
    re.IGNORECASE,
)

# Slurs: masked in outgoing text whatever the register (a guard, not a style signal).
_SLURS = re.compile(
    r"\b(?:nigg(?:er|a|as|ers|ah)|fag(?:got)?s?|faggots?|retard(?:s|ed)?|tranny|trannies|chinks?|"
    r"spics?|kikes?|pakis?|wetbacks?|coons?|gooks?|dykes?|raghead|towelhead|shemale)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def has_profanity(text: str) -> bool:
    if _MASKED.search(text):
        return True
    return any(t in _SWEAR_WORDS or _SWEAR_FORMS.fullmatch(t) for t in _tokens(text))


def _formal(text: str) -> bool:
    """Reads formal: polite markers or a long well-formed sentence, and nothing casual about it."""
    stripped = text.strip()
    words = _tokens(stripped)
    if not words or has_profanity(stripped) or _casual_markers(stripped):
        return False
    well_formed = stripped[0].isupper() and stripped[-1] in ".?!" and " i " not in f" {stripped} "
    return well_formed and (bool(_POLITE.search(stripped)) or len(words) >= 14)


def _casual_markers(text: str) -> bool:
    words = set(_tokens(text))
    return bool(words & _SLANG or _ELONGATED.search(text) or _EMOJI.search(text))


def _casual(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _casual_markers(stripped) or has_profanity(stripped):
        return True
    return stripped[0].islower() and stripped[-1] not in ".?!"


def _hype(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.6
    return shouting or "!!" in text or bool(_ELONGATED.search(text))


@dataclass(frozen=True)
class Register:
    sample: int = 0  # user messages measured
    swears: bool = False  # swore recently and the latest message is not formal: swearing may be mirrored
    swear_share: float = 0.0  # share of measured messages with profanity ("in proportion")
    formal: bool = False  # the latest message reads formal
    casual: bool = False  # most measured messages are casual
    hype: bool = False  # the latest message is high energy
    distressed: bool = False  # the latest message sounds like a hard moment: no swearing, no jokes


def measure(texts: list[str]) -> Register:
    """Measure the register of a user's messages, oldest first. The latest message weighs most:
    a formal latest message turns swearing off even if they swore a minute ago."""
    texts = [t for t in texts if t and t.strip()][-SAMPLE:]
    if not texts:
        return Register()
    latest = texts[-1]
    formal = _formal(latest)
    distressed = bool(_DISTRESS.search(latest))
    sweary = [has_profanity(t) for t in texts]
    return Register(
        sample=len(texts),
        swears=not formal and not distressed and any(sweary[-SWEAR_RECENT:]),
        swear_share=sum(sweary) / len(texts),
        formal=formal,
        casual=not formal and sum(_casual(t) for t in texts) * 2 >= len(texts),
        hype=_hype(latest) and not distressed,
        distressed=distressed,
    )


def user_texts(history: list, now: datetime, window: timedelta | None = WINDOW) -> list[str]:
    """The user's own messages from `history` (oldest first), within `window` of `now` when given."""
    out = []
    for m in history:
        if getattr(m, "role", None) != "user":
            continue
        if window is not None:
            created = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=UTC)
            if now - created > window:
                continue
        out.append(m.content)
    return out


_LIMITS = "never at them, never an insult, never a slur, nothing sexual"


def prompt_line(reg: Register, *, proactive: bool = False) -> str:
    """One prompt line describing their register right now, or "" when there is nothing to go on
    (chat turns; the persona default then applies: no swearing)."""
    if proactive:
        if reg.swears and not reg.formal:
            return ("Their register lately: casual and sweary. You may swear lightly, at most once and one "
                    f"notch milder than them ({_LIMITS}), and not at all if this message carries bad news, "
                    "health or money trouble.")
        return "Their register lately is not sweary: don't swear in this message."
    if reg.sample == 0:
        return ""
    energy = " High energy right now: match it, short and punchy." if reg.hype else ""
    if reg.distressed:
        return ("They sound upset or shaken right now: no swearing at all (not even echoing their own words) "
                "and no jokes, even if they swore. Slow down, be kind and plain, then be useful.")
    if reg.formal:
        return ("Their register right now: formal. Answer clear and polite, no slang and no swearing, "
                f"even if they swore earlier.{energy}")
    if reg.swears:
        amount = "they swear now and then, so at most one, only where it fits"
        if reg.swear_share >= 0.5:
            amount = "they swear a lot, so a swear here and there sounds natural"
        return ("Their register right now: casual and sweary. You may swear casually back, in proportion: "
                f"{amount}. Use it for emphasis or fun, not filler ({_LIMITS}). Drop it the moment they "
                f"are upset or the news is bad.{energy}")
    if reg.casual:
        return f"Their register right now: casual. Keep it relaxed, but don't swear: they haven't.{energy}"
    return f"Their register right now: plain and neutral. Don't swear.{energy}"


def _mask(m: re.Match[str]) -> str:
    word = m.group(0)
    return word[0] + "*" * (len(word) - 2) + word[-1] if len(word) > 2 else "*" * len(word)


def mask_slurs(text: str) -> str:
    return _SLURS.sub(_mask, text)


def unmirrored(reg: Register, text: str, *, proactive: bool = False) -> bool:
    """The reply swears although their register does not allow it (formal, upset, or they never swore).
    Chat turns with nothing measured are left to the prompt; proactive messages need a sweary chat."""
    if reg.swears or (reg.sample == 0 and not proactive):
        return False
    return has_profanity(text)


TONE_DOWN = (
    "Rewrite the chat message you are given so it has no swearing and no crude words. Keep everything "
    "else the same: meaning, facts, warmth, length, emoji, line breaks and any line that is just ---. "
    "Reply with the rewritten message only."
)


async def tone_down(text: str, *, priority: llm.Priority = "interactive") -> str:
    """One rewrite without the swearing; the original when the rewrite fails or still swears."""
    try:
        out = await llm.complete([SystemMessage(TONE_DOWN), HumanMessage(text)], tier=llm.Tier.FAST,
                                 temperature=0.2, name="tone_down", priority=priority)
    except Exception as exc:  # noqa: BLE001 - the reply still goes out
        log.warning("register.tone_down_failed", error=type(exc).__name__)
        return text
    out = mask_slurs(out).strip()
    if not out or has_profanity(out):
        log.info("register.tone_down_kept_original")
        return text
    log.info("register.toned_down")
    return out

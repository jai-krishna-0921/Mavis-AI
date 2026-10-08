"""The user's register (how they talk), measured from their own recent messages (T1.2).

The model is told how the user talks right now so it can mirror it: casual or formal, sweary or clean,
hyped or calm. The word lists here are measured signals that feed the prompt, never reply text: the
model writes its own words. Persona rules (agents/persona.py) set the limits: never insult the user,
never slurs, never sexual content, step down when they are upset.

Two guards on outgoing text: `mask_slurs` (a slur never reaches the user, whatever the model wrote) and
`tone_down` (one rewrite call, only when a reply swears although their register does not allow it).
"""

from __future__ import annotations

import asyncio
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

# Profanity (English, Hinglish), matched on whole word tokens only: never a substring or a prefix, so
# "shitake", "Scunthorpe", "classes" and "Fukushima" are words, not swears. STRONG words are unambiguous;
# WEAK ones (mild, or with an innocent meaning: "bc" = because, "MC", "hell", "dick") never count alone.
# A capitalised word mid-sentence is a proper noun ("Moby Dick", "Hell's Kitchen") and never counts;
# an all-caps word does ("FUCK"). A masked spelling counts when it hides a strong word ("f*ck", "sh!t").
STRONG = frozenset({
    "fuck", "fucks", "fucked", "fucker", "fuckers", "fucking", "fuckin", "fuckery", "fuckhead", "fuckface",
    "fuckup", "fuckwit", "motherfucker", "motherfuckers", "motherfucking", "fck", "fcking", "fcked", "fking",
    "fkn", "fuk", "fukk", "fukking", "fuking", "fukin", "fukked", "shit", "shits", "shitty", "shittier",
    "shitting", "shitted", "shite", "shithead", "shitshow", "shitload", "shithole", "shitstorm", "bullshit",
    "horseshit", "dipshit", "apeshit", "asshole", "assholes", "arsehole", "arseholes", "dumbass", "jackass",
    "bitch", "bitches", "bitching", "bitchy", "bastard", "bastards", "dickhead", "dickheads", "goddamn",
    "goddamned", "goddammit", "dammit", "damnit", "cunt", "cunts", "twat", "wanker", "wtf", "stfu", "ffs",
    "omfg", "fml", "jfc", "gtfo", "bsdk", "bhenchod", "behenchod", "madarchod", "chutiya", "chutiye",
    "bhosdike",
})
WEAK = frozenset({
    "damn", "damned", "hell", "bloody", "crap", "crappy", "piss", "pissed", "pissy", "ass", "arse", "dick",
    "dicks", "prick", "pricks", "sod", "screwed", "bollocks", "badass", "bc", "mc", "af", "saala", "saale",
    "kamina", "harami", "gaand",
})
_MASK_CHARS = "*!#@$"
_WORD = re.compile(r"[A-Za-z](?:[A-Za-z'*!#@$]*[A-Za-z*])?")
# "Mr. Bastard", "Dr. Dick": an honorific's full stop is not a sentence end
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Mx|Dr|Prof|St|Sr|Jr|Mt|Capt|Sgt|Rev|Hon|Gen|Col|Lt|Fr)\.\s*$")
_SENTENCE_END = re.compile(r"(?:^|[.!?\n:;\"(\u201c]\s*)$")
_TOKEN = re.compile(r"[a-z]+", re.IGNORECASE)


def _word_tokens(text: str) -> list[tuple[str, bool]]:
    """(word, proper_noun) per word. Proper noun: capitalised, not all caps, not at a sentence start."""
    out = []
    for m in _WORD.finditer(text):
        word = m.group(0).rstrip("'")
        if word.lower().endswith("'s"):
            word = word[:-2]
        before = text[: m.start()]
        sentence_start = bool(_SENTENCE_END.search(before[-3:] if before else "")) and not _HONORIFIC.search(
            before[-8:])
        proper = word[:1].isupper() and not word.isupper() and not sentence_start
        out.append((word, proper and bool(before.strip())))
    return out


def _masked_strong(word: str) -> bool:
    if not any(c in _MASK_CHARS for c in word) or sum(c.isalpha() for c in word) < 2:
        return False
    pattern = re.sub(rf"[{re.escape(_MASK_CHARS)}]+", ".{1,3}", re.escape(word.lower()).replace("\\", ""))
    return any(re.fullmatch(pattern, w) for w in ("fuck", "fucking", "shit", "bitch", "cunt", "fucked"))


def profanity_score(text: str) -> tuple[int, int]:
    """(strong, weak) swear counts in `text`, proper nouns excluded."""
    strong = weak = 0
    for word, proper in _word_tokens(text):
        if proper:
            continue
        low = word.lower()
        if low in STRONG or _masked_strong(word):
            strong += 1
        elif low in WEAK:
            weak += 1
    return strong, weak


def has_profanity(text: str) -> bool:
    """They swore: one unambiguous swear, or two mild ones together."""
    strong, weak = profanity_score(text)
    return strong >= 1 or weak >= 2


def strong_profanity(text: str) -> bool:
    """An unambiguous swear (the bar for acting on a reply)."""
    return profanity_score(text)[0] >= 1


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
    r"heart attack|accident|surgery|diagnosed with|miscarriage|suicidal|suicide|self[- ]harm|"
    r"scared|terrified|panic attack|panicking|anxiety|depressed|depression|crying|cried|in tears|grief|"
    r"grieving|"
    r"heartbroken|broke up|break ?up|divorce|laid off|got fired|lost my (?:job|mom|mum|dad|father|mother)|"
    r"can'?t (?:cope|breathe|think straight)|falling apart)\b",
    re.IGNORECASE,
)

_LAUGHING = re.compile(r"\b(?:lol|lmao|lmfao|haha\w*|crying laughing|dying laughing)\b|\U0001f602|\U0001f923",
                       re.IGNORECASE)

# Slurs: masked in outgoing text whatever the register (a guard, not a style signal). Only words with
# no innocent meaning as plain words; whole words only, never inside links, email addresses, code or
# verbatim spans (third-party text shown as written). Each entry says whether it is also an ordinary
# capitalised name or food ("Faggots" is a British dish): only those keep the proper-noun exception;
# every other entry is masked however it is capitalised.
SLURS: dict[str, bool] = {  # word -> ambiguous as a capitalised name or food
    "nigger": False, "niggers": False, "nigga": False, "niggas": False, "faggot": True, "faggots": True,
    "kike": False, "kikes": False, "tranny": False, "trannies": False, "wetback": False, "wetbacks": False,
    "raghead": False, "ragheads": False, "towelhead": False, "towelheads": False,
}
# Spans that are never rewritten or masked: verbatim (\x0e...\x0f), code, links, email addresses.
PROTECTED = re.compile(
    r"\x0e.*?(?:\x0f|$)|```.*?(?:```|$)|`[^`\n]+`|(?:https?://|www\.)\S+|\S+@\S+\.\S+|"
    r"\b[\w-]+(?:\.[\w-]+)+/\S*",
    re.DOTALL,
)

def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


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


async def standing_brief(user_id: int) -> bool:
    """They asked for short messages: the profile preference LEARN wrote (profile card `brevity`). It holds
    until they take it back, so it is not limited to the recent conversation. No text matching here."""
    from mavis.store.repo import profile as profile_repo  # lazy: keep this module free of the store

    try:
        return (await profile_repo.get(user_id)).brevity == "short"
    except Exception:  # noqa: BLE001 - a style hint must never break a turn
        return False


@dataclass(frozen=True)
class Register:
    sample: int = 0  # user messages measured
    brief: bool = False  # a standing wish for short messages (profile card brevity)
    latest_swore: bool = False  # the latest message itself swears: mirror it now, not just "may"
    swears: bool = False  # swore recently and the latest message is not formal: swearing may be mirrored
    swear_share: float = 0.0  # share of measured messages with profanity ("in proportion")
    formal: bool = False  # the latest message reads formal
    casual: bool = False  # most measured messages are casual
    hype: bool = False  # the latest message is high energy
    distressed: bool = False  # the latest message sounds like a hard moment: no swearing, no jokes


def measure(texts: list[str], *, brief: bool = False) -> Register:
    """Measure the register of a user's messages, oldest first. The latest message weighs most:
    a formal latest message turns swearing off even if they swore a minute ago. `brief`: the profile card's
    standing wish for short messages."""
    texts = [t for t in texts if t and t.strip()][-SAMPLE:]
    if not texts:
        return Register(brief=brief)
    latest = texts[-1]
    formal = _formal(latest)
    distressed = bool(_DISTRESS.search(latest)) and not _LAUGHING.search(latest)
    sweary = [has_profanity(t) for t in texts]
    swears = not formal and not distressed and any(sweary[-SWEAR_RECENT:])
    return Register(
        sample=len(texts),
        brief=brief,
        latest_swore=swears and sweary[-1],
        swears=swears,
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


BRIEF_LINE = ("They told you they want short messages: keep every reply to one to three short lines "
              "(under about 250 characters), no lists, no preamble, answer first. Go longer only when they "
              "explicitly ask for detail, and then say the short version first.")


def prompt_line(reg: Register, *, proactive: bool = False) -> str:
    """The prompt line(s) describing their register right now, or "" when there is nothing to go on
    (chat turns; the persona default then applies: no swearing). A standing wish for short messages is
    appended whatever else is known."""
    line = _register_line(reg, proactive=proactive)
    return f"{line} {BRIEF_LINE}".strip() if reg.brief else line


def _register_line(reg: Register, *, proactive: bool = False) -> str:
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
        if reg.latest_swore:
            # "may" was read as "better not": the model stayed clean in every live run. They just swore, so
            # a clean reply reads as stiff: ask for one, aimed at the situation.
            return ("Their register right now: casual and sweary, and they just swore, so a spotless reply "
                    "sounds stiff. You may swear casually back, so do: include exactly ONE casual swear word "
                    "(damn, shit, hell, bloody, fuck: whichever fits, no stronger than theirs), aimed at the "
                    "situation (the "
                    f"week, the deadline, the mess), {_LIMITS}. In proportion: {amount}. Skip it only if "
                    f"they are upset or the news is bad.{energy}")
        return ("Their register right now: casual and sweary. You may swear casually back, in proportion: "
                f"{amount}. Use it for emphasis or fun, not filler ({_LIMITS}). Drop it the moment they "
                f"are upset or the news is bad.{energy}")
    if reg.casual:
        return f"Their register right now: casual. Keep it relaxed, but don't swear: they haven't.{energy}"
    return f"Their register right now: plain and neutral. Don't swear.{energy}"


def _mask_word(word: str) -> str:
    return word[0] + "*" * (len(word) - 2) + word[-1] if len(word) > 2 else "*" * len(word)


def _mask_plain(text: str) -> str:
    out, last = [], 0
    for m, (word, proper) in zip(_WORD.finditer(text), _word_tokens(text), strict=True):
        ambiguous = SLURS.get(word.lower())
        if ambiguous is not None and not (proper and ambiguous):
            out.append(text[last : m.start()])
            out.append(_mask_word(word) + m.group(0)[len(word):])
            last = m.end()
    out.append(text[last:])
    return "".join(out)


def _outside_protected(text: str, fn) -> str:
    out, last = [], 0
    for m in PROTECTED.finditer(text):
        out.append(fn(text[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def mask_slurs(text: str) -> str:
    return _outside_protected(text, _mask_plain)


def unmirrored(reg: Register, text: str, *, proactive: bool = False) -> bool:
    """The reply clearly swears (an unambiguous word outside names, links and code) although their register
    does not allow it: formal, upset, or they never swore. Chat turns with nothing measured are left to
    the prompt; proactive messages need a recent sweary chat."""
    if reg.swears or (reg.sample == 0 and not proactive):
        return False
    return strong_profanity(PROTECTED.sub(" ", text))


TONE_DOWN = (
    "Rewrite the chat message you are given so it has no swearing and no crude words. Change only those "
    "words. Keep everything else the same: meaning, facts, names, warmth, length, emoji, line breaks, any "
    "line that is just ---, and every placeholder like [[1]] exactly as it is. Reply with the rewritten "
    "message only."
)
TONE_DOWN_TIMEOUT_S = 4.0  # the whole rewrite, slot wait included; after this the original goes out
_PLACEHOLDER = "[[{}]]"


def _names(text: str) -> set[str]:
    """Capitalised words that are not swears: names and places the rewrite must keep."""
    return {w for w, _ in _word_tokens(text)
            if w[:1].isupper() and w.lower() not in STRONG | WEAK and not _masked_strong(w)}


async def tone_down(text: str, *, priority: llm.Priority = "interactive") -> str:
    """One rewrite without the swearing, within TONE_DOWN_TIMEOUT_S. Links, email addresses, code and
    verbatim spans are held back as placeholders and put back unchanged. The original goes out when the
    rewrite times out, fails, drops a name or a placeholder, or still swears."""
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return _PLACEHOLDER.format(len(held))

    sent = PROTECTED.sub(hold, text)
    try:
        out = await asyncio.wait_for(
            llm.complete([SystemMessage(TONE_DOWN), HumanMessage(sent)], tier=llm.Tier.FAST,
                         temperature=0.2, name="tone_down", priority=priority, fallback=False),
            TONE_DOWN_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - timeout or model failure: the reply still goes out
        log.warning("register.tone_down", outcome="failed", error=type(exc).__name__)
        return text
    out = (out or "").strip()
    marks = [_PLACEHOLDER.format(i + 1) for i in range(len(held))]
    if not out or any(out.count(mark) != 1 for mark in marks):
        log.info("register.tone_down", outcome="kept_original", reason="placeholders")
        return text
    for mark, original in zip(marks, held, strict=True):
        out = out.replace(mark, original)
    if strong_profanity(PROTECTED.sub(" ", out)) or _names(text) - _names(out):
        log.info("register.tone_down", outcome="kept_original", reason="swears_or_names")
        return text
    log.info("register.tone_down", outcome="rewritten")
    return mask_slurs(out)

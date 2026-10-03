"""Untrusted free text (subjects, names, requested actions) on its way to storage, prompts or the user."""

from __future__ import annotations

import re
import unicodedata

from mavis.attention.schema import EmailKind
from mavis.initiative.composer import CHECK_DIRECTLY, scrub_untrusted_origin

REMOVED = "[removed]"
SUMMARY_LIMIT = 200
SUBJECT_LIMIT = 80
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_MARKUP = re.compile(r"[<>`]")
_TAG = re.compile(r"</?\w+[^>]*>")
# Bare domains are removed in any script (Telegram auto-links IDN hosts, so Cyrillic or Greek lookalikes
# are blocked too). Side effect: "Mr.Smith" and "invoice.pdf" are scrubbed as well, which is acceptable.
_BARE_DOMAIN = re.compile(r"(?<![\w@])(?:[^\W_]|-)+(?:\.(?:[^\W_]|-)+)*\.[^\W\d_]{2,}(?!\w)")
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
_OBF_DOT = re.compile(r"\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\}|[\[(]\s*dot\s*[\])])\s*", re.IGNORECASE)
_OBF_AT = re.compile(r"\s*[\[(]\s*at\s*[\])]\s*", re.IGNORECASE)
_OBF_SPACED_DOT = re.compile(r"(?<=\w)[ \t]+\.[ \t]+(?=[^\W\d_]{2,}\b)")
_HXXP = re.compile(r"\bhxxp(s?)(?=\W)", re.IGNORECASE)
_HANDLE = re.compile(r"[\w.+-]+@[\w-]+")  # UPI ids and dotless handles the composer email pattern misses
_CODE_WORDS = r"(?:otp|code|pin|cvv|passcode|password|token|verification)"
_CODE_WORD = re.compile(
    rf"\b{_CODE_WORDS}\b[\s:=#-]*(?:is\s+)?[a-z0-9-]*\d[a-z0-9-]*",
    re.IGNORECASE,
)
_CODE_BEFORE = re.compile(  # "4821 is your verification code"
    rf"(?<![\w.,])\d{{4,8}}(?![\w.,])(?=\s+(?:is\s+)?(?:your\s+|the\s+)?(?:one[- ]time\s+)?{_CODE_WORDS}\b)",
    re.IGNORECASE,
)
_DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\d)"
# Digit groups joined by single spaces or hyphens; a date is never swallowed into a neighbouring run.
_DIGIT_RUN = re.compile(rf"(?<![\w/.])(?!{_DATE})\d+(?:[ -](?!{_DATE})\d+)*(?!\w)")
_KEEP_DATE = re.compile(rf"(?<![\w/.-])(?:{_DATE}|\d{{4}} \d{{2}} \d{{2}}(?!\d))")
SECRET_DIGITS = 9  # account, card and phone numbers; plain amounts and dates stay readable
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "ac", "edu"})


def _digit_run(m: re.Match[str]) -> str:
    return REMOVED if sum(c.isdigit() for c in m[0]) >= SECRET_DIGITS else m[0]


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t).replace("\u3002", ".")
    t = _HXXP.sub(lambda m: f"http{m[1]}", t)
    t = _OBF_DOT.sub(".", t)
    t = _OBF_AT.sub("@", t)
    return _OBF_SPACED_DOT.sub(".", t)


def clean(text: object, limit: int) -> str:
    dates: list[str] = []

    def hold(m: re.Match[str]) -> str:  # the composer phone pattern would eat dates; park them meanwhile
        dates.append(m[0])
        return chr(0xE100 + len(dates) - 1)

    t = _KEEP_DATE.sub(hold, _normalize(str(text or "")))
    t = scrub_untrusted_origin(t).replace(CHECK_DIRECTLY, REMOVED)
    t = _HANDLE.sub(REMOVED, t)
    t = _CODE_WORD.sub(REMOVED, t)
    t = _CODE_BEFORE.sub(REMOVED, t)
    t = _DIGIT_RUN.sub(_digit_run, t)
    t = _BARE_DOMAIN.sub(REMOVED, t)
    t = _MARKUP.sub("", _TAG.sub("", _CTRL.sub(" ", t)))
    t = " ".join(t.split())  # third-party text keeps its punctuation (a subject's "Q3 – final")
    for i, d in enumerate(dates):
        t = t.replace(chr(0xE100 + i), d)
    return t[:limit].rstrip()


def sender_domain(address: str) -> str:
    """Registrable domain of an address: alerts@mail.examplebank.in -> examplebank.in."""
    host = str(address or "").rpartition("@")[2].strip().lower().rstrip(".")
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return host
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_label(domain: str) -> str:
    """The name without TLD (examplebank): readable, and never rendered as a link."""
    return domain.split(".", 1)[0] if domain else "an unknown sender"


def summary_text(kind: EmailKind, domain: str, subject: str) -> str:
    """The only text that is embedded and shown in digests: kind, domain label, short scrubbed subject."""
    label = kind.value.replace("_", " ")
    return clean(f"{label} from {domain_label(domain)}: {clean(subject, SUBJECT_LIMIT)}", SUMMARY_LIMIT)

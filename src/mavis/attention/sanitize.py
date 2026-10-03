"""Untrusted free text (subjects, names, requested actions) on its way to storage, prompts or the user."""

from __future__ import annotations

import re

from mavis.attention.schema import EmailKind
from mavis.channels.formatting import sanitize_typography
from mavis.initiative.composer import CHECK_DIRECTLY, scrub_untrusted_origin

REMOVED = "[removed]"
SUMMARY_LIMIT = 200
SUBJECT_LIMIT = 80
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_MARKUP = re.compile(r"[<>`]")
_TAG = re.compile(r"</?\w+[^>]*>")
_BARE_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)  # Telegram auto-links these
_HANDLE = re.compile(r"[\w.+-]+@[\w-]+")  # UPI ids and dotless handles the composer email pattern misses
_CODE_WORD = re.compile(
    r"\b(?:otp|code|pin|cvv|passcode|password|token|verification)\b[\s:=#-]*(?:is\s+)?[a-z0-9-]*\d[a-z0-9-]*",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"(?<![\w])\d(?:[\d\s-]{3,}\d)(?![\w])")  # codes, card and account numbers
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "ac", "edu"})


def clean(text: object, limit: int) -> str:
    t = scrub_untrusted_origin(str(text or "")).replace(CHECK_DIRECTLY, REMOVED)
    t = _HANDLE.sub(REMOVED, t)
    t = _CODE_WORD.sub(REMOVED, t)
    t = _LONG_DIGITS.sub(REMOVED, t)
    t = _BARE_DOMAIN.sub(REMOVED, t)
    t = _MARKUP.sub("", _TAG.sub("", _CTRL.sub(" ", t)))
    t = sanitize_typography(" ".join(t.split()))
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

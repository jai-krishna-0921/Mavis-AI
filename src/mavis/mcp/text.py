# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""Hygiene for text a remote MCP server sends: invisible characters, terminal escapes, credential-shaped
strings, and oversized output.

Adapted from Hermes Agent: `tools/ansi_strip.py` (ANSI and Unicode TAG stripping), `tools/mcp_tool_common.py`
(the credential pattern) and `tools/tool_output_truncate.py` (40% head, 60% tail truncation).

Everything here is pure. A server's text is data: it is cleaned before it is stored, shown or handed to the
model, and the model still receives it inside the registry's untrusted fence."""

from __future__ import annotations

import re
from collections.abc import Iterable

# ANSI/ECMA-48 escape sequences: CSI, OSC (BEL or ST), DCS/SOS/PM/APC, nF, Fp/Fe/Fs and 8-bit C1 controls.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"  # CSI sequence
    r"|\][\s\S]*?(?:\x07|\x1b\\)"  # OSC (BEL or ST terminator)
    r"|[PX^_][\s\S]*?(?:\x1b\\)"  # DCS/SOS/PM/APC strings
    r"|[\x20-\x2f]+[\x30-\x7e]"  # nF escape sequences
    r"|[\x30-\x7e]"  # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"  # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"  # 8-bit OSC
    r"|[\x80-\x9f]",  # other 8-bit C1 controls
    re.DOTALL,
)
_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")
_HAS_UNICODE_TAG = re.compile(r"[\U000E0000-\U000E007F]")
# C0 controls except tab, newline and carriage return, plus DEL.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Unicode TAG characters render as nothing but a tokenizer sees them: the "ASCII smuggling" injection channel.
# The only legitimate use is an emoji tag sequence (a U+1F3F4 base, tag letters, U+E007F), which is kept.
_UNICODE_TAG_SUB_RE = re.compile(
    r"(\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F)"  # a valid emoji tag sequence (kept)
    r"|[\U000E0000-\U000E007F]"  # any other tag character (stripped)
)
# Not in Hermes: bidirectional overrides and zero-width characters hide or reorder text the same way. The
# zero-width joiner (U+200D) stays: emoji sequences need it.
_INVISIBLE_RE = re.compile(r"[​‌‎‏‪-‮⁠-⁤⁦-⁩﻿]")
_WHITESPACE_RE = re.compile(r"\s+")

# Credential shapes to blank out of text before it reaches a model or a log: a GitHub token, an OpenAI-style
# key, a Bearer header value, and token= / key= / API_KEY= / password= / secret= assignments (Hermes).
# The extra alternatives (Slack, AWS, JWT, GitHub fine-grained) are ours.
_CREDENTIAL_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|github_pat_[A-Za-z0-9_]{20,255}|gh[ousr]_[A-Za-z0-9_]{20,255}"
    r"|sk-[A-Za-z0-9_-](?:\.?[A-Za-z0-9_-]){0,254}|xox[abprs]-[A-Za-z0-9-]{10,255}|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}|Bearer\s+\S+"
    r"|(?:token|key|API_KEY|password|secret)=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"
HEAD_RATIO = 0.4


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences; clean text passes through unchanged."""
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def strip_unicode_tags(text: str) -> str:
    """Remove invisible Unicode TAG characters (valid emoji tag sequences are kept)."""
    if not text or not _HAS_UNICODE_TAG.search(text):
        return text
    return _UNICODE_TAG_SUB_RE.sub(lambda m: m.group(1) or "", text)


def strip_invisible(text: str) -> str:
    """Remove bidirectional overrides and zero-width characters."""
    return _INVISIBLE_RE.sub("", text) if text else text


def clean_text(text: str) -> str:
    """Server text made safe to show or store: escapes, controls, tag characters and invisible characters
    gone; CRs become newlines (so `\\r` overwrite spoofing cannot hide content). Newlines and tabs stay."""
    if not text:
        return ""
    text = strip_unicode_tags(strip_invisible(strip_ansi(text)))
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", text)


def clean_description(text: object, limit: int = 600) -> str:
    """One line of server-written description: cleaned, whitespace collapsed, at most `limit` characters."""
    if not isinstance(text, str):
        return ""
    flat = _WHITESPACE_RE.sub(" ", clean_text(text)).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def sanitize_error(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace credential-shaped strings, and any secret we hold, with [REDACTED]."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, REDACTED)
    return _CREDENTIAL_PATTERN.sub(REDACTED, text)


def truncation_notice(omitted: int, total: int, *, label: str = "OUTPUT", unit: str = "chars") -> str:
    return f"\n\n... [{label} TRUNCATED - {omitted:,} {unit} omitted out of {total:,} total] ...\n\n"


def truncate_head_tail(text: str, max_chars: int, *, label: str = "OUTPUT") -> str:
    """`text` unchanged when it fits; otherwise 40% head, a marker, 60% tail. Unlike Hermes the marker is
    inside the budget: the result is never longer than `max_chars`."""
    if len(text) <= max_chars:
        return text
    notice_len = len(truncation_notice(len(text), len(text), label=label))
    keep = max(max_chars - notice_len, 0)
    head = int(keep * HEAD_RATIO)
    tail = keep - head
    omitted = len(text) - head - tail
    return text[:head] + truncation_notice(omitted, len(text), label=label) + (text[-tail:] if tail else "")

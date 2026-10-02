"""Name normalisation and vocabulary sanitising shared by every graph backend."""

from __future__ import annotations

import re

from zento.domain.memory import NODE_LABELS, REL_TYPES

USER_KEY = "User:user"
_PUNCT = re.compile(r"[^\w\s'-]")
_REL_SAFE = re.compile(r"[^A-Z0-9_]")
_LABELS = {label.casefold(): label for label in NODE_LABELS}
_USER_WORDS = {"user", "me", "i", "myself"}


def normalize_name(name: str) -> str:
    return " ".join(_PUNCT.sub(" ", name).casefold().split())


def node_key(label: str, name: str) -> str:
    return f"{label}:{normalize_name(name)}"


def is_user(name: str) -> bool:
    return normalize_name(name) in _USER_WORDS


def sanitize_label(label: str) -> str:
    return _LABELS.get(label.strip().casefold(), "Topic")


def sanitize_rel(rel: str) -> str:
    candidate = _REL_SAFE.sub("_", rel.strip().upper().replace(" ", "_").replace("-", "_"))
    return candidate if candidate in REL_TYPES else "RELATED_TO"

"""Stable ids for the things Mavis knows about a user.

One id scheme serves the Vault (list, correct, forget), the learning suppression list and the evidence the
personal layer cites, so "forget this" means the same thing everywhere. An id is `<kind>:<key>`; the key is
derived from the item's content, never from a database row, so the same fact has the same id across graph
backends and across rebuilds.
"""

from __future__ import annotations

import hashlib
import re

from mavis.memory.names import normalize_name

FACT, PERSON, ROUTINE, PROFILE, LAYER, STYLE, LOOP, ENTITY = (
    "fact", "person", "routine", "profile", "layer", "style", "loop", "entity")


def digest(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:14]


def make(kind: str, key: str) -> str:
    return f"{kind}:{key}"[:240]


def split(item_id: str) -> tuple[str, str]:
    kind, _, key = item_id.partition(":")
    return kind, key


def fact_id(subject: str, rel: str, obj: str) -> str:
    return make(FACT, digest(normalize_name(subject), rel.strip().upper(), normalize_name(obj)))


def entity_id(name: str) -> str:
    return make(ENTITY, normalize_name(name))


_SLUG = re.compile(r"[^a-z0-9@._:-]+")


def person_key(email: str = "", slack_id: str = "", name: str = "") -> str:
    """The identity of a person across records: their address first, then their Slack id, then their name."""
    if email.strip():
        return email.strip().lower()
    if slack_id.strip():
        return f"slack:{slack_id.strip()}"
    return "name:" + _SLUG.sub("-", normalize_name(name))[:80]


def person_id(key: str) -> str:
    return make(PERSON, key)


def routine_id(title: str, weekday: int, hm: str) -> str:
    return make(ROUTINE, digest(normalize_name(title), str(weekday), hm))


def profile_id(field: str, value: str = "") -> str:
    return make(PROFILE, field if not value else f"{field}:{digest(' '.join(value.split()).casefold())}")


def layer_id(section: str, evidence: list[str]) -> str:
    return make(LAYER, digest(section, *sorted(evidence)))

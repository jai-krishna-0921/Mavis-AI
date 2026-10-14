# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""A server's slug: the short lowercase name the model and the user use for it (`[a-z0-9_]{1,24}`, unique per
user). Made from the display name; a clash gets a numeric suffix."""

from __future__ import annotations

import re
from collections.abc import Iterable

from mavis.mcp.limits import SLUG_RE

_NON_SLUG = re.compile(r"[^a-z0-9]+")
MAX = 24


def slugify(name: str) -> str:
    base = _NON_SLUG.sub("_", (name or "").lower()).strip("_")[:MAX].strip("_")
    return base or "server"


def unique_slug(name: str, taken: Iterable[str]) -> str:
    used = set(taken)
    base = slugify(name)
    if base not in used:
        return base
    for n in range(2, 1000):
        suffix = f"_{n}"
        candidate = base[: MAX - len(suffix)].rstrip("_") + suffix
        if candidate not in used:
            return candidate
    raise ValueError("too many servers with this name")


def is_slug(value: str) -> bool:
    return bool(SLUG_RE.match(value or ""))

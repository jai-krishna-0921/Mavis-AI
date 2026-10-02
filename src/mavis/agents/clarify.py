"""Deterministic pre-reply checks that ask instead of guessing."""

from __future__ import annotations

from datetime import datetime

from mavis.domain import timeutil

DAY_QUESTION_PREFIX = timeutil.DAY_QUESTION_PREFIX


def day_clarification(text: str, tz: str, at: datetime | None = None) -> str | None:
    """`at` is when the user wrote the message (defaults to now)."""
    return timeutil.needs_day_clarification(text, timeutil.to_local(at or timeutil.now(), tz))


def is_day_question(reply: str) -> bool:
    return reply.startswith(DAY_QUESTION_PREFIX)

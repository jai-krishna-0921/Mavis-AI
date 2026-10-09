from __future__ import annotations

import hashlib
from datetime import timedelta

from mavis.config import get_settings


def user_offset(user_id: int, span_s: int | None = None) -> timedelta:
    """Deterministic per-user offset in [0, span) so daily jobs for many users do not fire in the same minute
    (spec 7.3). Never applied to a time promised to the user (Programs deliveries are exempt)."""
    span = span_s if span_s is not None else get_settings().fanout_jitter_s
    if span <= 0:
        return timedelta(0)
    digest = int(hashlib.sha256(f"jitter:{user_id}".encode()).hexdigest(), 16)
    return timedelta(seconds=digest % span)

"""Composio user identity per user (spec 6.2): stored `users.composio_user_id` (`mavis-<env>-<id>` for new
users); legacy rows keep `mavis-<id>`, which only a prod stack may claim, so a staging stack sharing the key
can never take prod user 1's webhooks."""

from __future__ import annotations

import re
import time

import structlog
from sqlalchemy import select

from mavis.config import get_settings
from mavis.domain.errors import StartupRefused
from mavis.store.db import Session
from mavis.store.models import User

log = structlog.get_logger(__name__)
_LEGACY = re.compile(r"mavis-(\d+)")
_TTL_S = 600.0
_by_user: dict[int, tuple[float, str]] = {}
_by_pid: dict[str, tuple[float, int | None]] = {}


def clear_cache() -> None:
    _by_user.clear()
    _by_pid.clear()


def new_provider_id(user_id: int) -> str:
    return f"mavis-{get_settings().env}-{user_id}"


async def provider_id_for(user_id: int) -> str:
    hit = _by_user.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with Session() as s:
        stored = await s.scalar(select(User.composio_user_id).where(User.id == user_id))
    pid = stored or f"mavis-{user_id}"
    _by_user[user_id] = (time.monotonic() + _TTL_S, pid)
    return pid


async def user_for_provider_id(value: object) -> int | None:
    pid = str(value or "")
    if not pid:
        return None
    hit = _by_pid.get(pid)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with Session() as s:
        uid = await s.scalar(select(User.id).where(User.composio_user_id == pid))
        if uid is None and get_settings().env == "prod" and (m := _LEGACY.fullmatch(pid)):
            legacy = int(m.group(1))
            row = await s.get(User, legacy)
            uid = legacy if row is not None and row.composio_user_id is None else None
    _by_pid[pid] = (time.monotonic() + _TTL_S, uid)
    return uid


async def check_shared_key(provider) -> None:
    s = get_settings()
    if s.env == "prod" or s.composio_shared_key_ok:
        return
    lister = getattr(provider, "list_user_ids", None)
    if lister is None:
        return
    try:
        listed = await lister()
    except Exception as exc:  # noqa: BLE001 - an unreachable provider is not a reason to refuse to start
        log.warning("identity.shared_key_check_skipped", error=type(exc).__name__)
        return
    if any(_LEGACY.fullmatch(str(x)) or str(x).startswith("mavis-prod-") for x in listed):
        raise StartupRefused(
            "this Composio key has prod accounts; use a separate key or COMPOSIO_SHARED_KEY_OK")

"""Shared plumbing for /api/v1: the error shape, the session and CSRF dependencies, cookies, rate limits.

Every handler gets its user from `authed` (the session's user id); no handler reads a user id from the
request. Errors are {"error": code, "message": plain text}."""

from __future__ import annotations

import secrets

from fastapi import Depends, Request, Response

from mavis.api import ratelimit
from mavis.config import get_settings
from mavis.web import sessions
from mavis.web.sessions import Active

CSRF_HEADER = "X-Mavis-CSRF"
PRE_COOKIE = "mavis_login"
LINK_COOKIE = "mavis_link"  # holds the pre-session secret a pending Google address is bound to
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

AUTH_PER_IP = 30  # sign-in starts and callbacks per minute per IP
POLL_PER_IP = 90
SESSION_PER_MIN = 240  # authenticated requests per session per minute


class DashError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def not_found(what: str = "That") -> DashError:
    return DashError(404, "not_found", f"{what} was not found.")


async def require_enabled() -> None:
    if not get_settings().dashboard_enabled:
        raise DashError(404, "not_found", "Not found.")


def limit_ip(bucket: str, per_min: int):
    async def dep(request: Request) -> None:
        if not await ratelimit.hit(bucket, ratelimit.client_ip(request), per_min):
            raise DashError(429, "rate_limited", "Too many attempts. Wait a minute and try again.")

    return dep


def _secure() -> bool:
    return not get_settings().public_base_url.startswith("http://localhost")


def _secure_for(name: str) -> bool:
    return name.startswith("__Host-") or _secure()  # a __Host- cookie is only valid when Secure


def set_cookie(response: Response, name: str, value: str, *, max_age: int, path: str = "/") -> None:
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, secure=_secure_for(name),
                        samesite="lax")


def clear_cookie(response: Response, name: str, *, path: str = "/") -> None:
    response.delete_cookie(name, path=path, httponly=True, secure=_secure_for(name), samesite="lax")


def session_max_age() -> int:
    return get_settings().dashboard_session_days * 86_400


def new_pre_token() -> str:
    return secrets.token_hex(16)


async def current(request: Request) -> Active:
    active = await sessions.lookup(request.cookies.get(sessions.COOKIE))
    if active is None:
        raise DashError(401, "unauthorized", "Please sign in to continue.")
    if not await ratelimit.hit("sess", active.session_id, SESSION_PER_MIN):
        raise DashError(429, "rate_limited", "Too many requests. Wait a minute and try again.")
    return active


async def authed(request: Request, active: Active = Depends(current)) -> Active:
    """The session, with the CSRF header checked on every request that changes something."""
    supplied = request.headers.get(CSRF_HEADER)
    if request.method not in SAFE_METHODS and not sessions.csrf_ok(active.token, supplied):
        raise DashError(403, "csrf", "Your session could not be verified. Reload the page and try again.")
    return active


async def me_id(active: Active = Depends(authed)) -> int:
    return active.user.id

"""Sign-in and sign-out: Telegram link, Google, logout."""

from __future__ import annotations

from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mavis.access import admission
from mavis.access.codes import normalize
from mavis.api import ratelimit
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.store import db as dbm
from mavis.store.models import User
from mavis.web import emails, google_signin, logins, sessions, signed

router = APIRouter(prefix="/auth")
log = structlog.get_logger(__name__)

LINK_PURPOSE = "link"
LINK_TTL_S = 900
LOGIN_PATH = "/api/v1/auth"


class StartBody(BaseModel):
    invite: str | None = None
    link_token: str | None = None


async def _start_session(request: Request, response: Response, user_id: int) -> None:
    token = await sessions.create(user_id, ip=ratelimit.client_ip(request),
                                  user_agent=request.headers.get("user-agent", ""))
    common.set_cookie(response, sessions.COOKIE, token, max_age=common.session_max_age())


@router.post("/telegram/start", dependencies=[Depends(common.limit_ip("auth", common.AUTH_PER_IP))])
async def telegram_start(body: StartBody, response: Response) -> dict:
    invite = None
    if body.invite:
        invite = normalize(body.invite)
        if invite is None:
            raise DashError(400, "invalid_invite",
                            "That invite code does not look right. Check it and try again.")
    link_email = None
    if body.link_token:
        claim = signed.verify(LINK_PURPOSE, body.link_token)
        if claim is None:
            raise DashError(400, "link_expired", "That link expired. Sign in with Google again.")
        link_email = str(claim["email"])
    pre = common.new_pre_token()
    nonce, expires = await logins.start(pre, link_email=link_email)
    common.set_cookie(response, common.PRE_COOKIE, pre, max_age=int(logins.NONCE_TTL.total_seconds()),
                      path=LOGIN_PATH)
    return {"nonce": nonce, "deep_link": logins.deep_link(nonce, invite), "expires_at": expires.isoformat()}


@router.get("/telegram/poll", dependencies=[Depends(common.limit_ip("poll", common.POLL_PER_IP))])
async def telegram_poll(request: Request, response: Response, nonce: str = Query(max_length=64)) -> dict:
    status, user_id = await logins.collect(nonce, request.cookies.get(common.PRE_COOKIE))
    if status == "ok" and user_id is not None:
        await _start_session(request, response, user_id)
        common.clear_cookie(response, common.PRE_COOKIE, path=LOGIN_PATH)
    return {"status": status}


@router.get("/google/start", dependencies=[Depends(common.limit_ip("auth", common.AUTH_PER_IP))])
async def google_start(request: Request, confirm: bool = False) -> Response:
    """Sign in with Google; with ?confirm=1 and a live session, confirm that Google address on the account."""
    user_id = None
    if confirm:
        active = await sessions.lookup(request.cookies.get(sessions.COOKIE))
        if active is None:
            return RedirectResponse("/login", status_code=302)
        user_id = active.user.id
    try:
        begun = google_signin.begin(mode="confirm" if confirm else "signin", user_id=user_id)
    except google_signin.SigninError:
        return RedirectResponse("/login?error=google_unavailable", status_code=302)
    response = RedirectResponse(begun.url, status_code=302)
    common.set_cookie(response, google_signin.STATE_COOKIE, begun.cookie, max_age=google_signin.STATE_TTL_S,
                      path=f"{LOGIN_PATH}/google")
    return response


def _done(location: str) -> RedirectResponse:
    response = RedirectResponse(location, status_code=302)
    common.clear_cookie(response, google_signin.STATE_COOKIE, path=f"{LOGIN_PATH}/google")
    return response


@router.get("/google/callback", dependencies=[Depends(common.limit_ip("auth", common.AUTH_PER_IP))])
async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None) -> Response:
    if error or not code or not state:
        return _done("/login?error=google_denied")
    try:
        who = await google_signin.finish(request.cookies.get(google_signin.STATE_COOKIE), state, code)
    except google_signin.SigninError as exc:
        log.info("google_signin.failed", kind=exc.kind)
        return _done(f"/login?error=google_{exc.kind}")
    if who.mode == "confirm":
        active = await sessions.lookup(request.cookies.get(sessions.COOKIE))
        if active is None or active.user.id != who.user_id:
            return _done("/login")
        ok = await emails.confirm(active.user.id, who.email, "google_signin")
        return _done("/preferences?email=confirmed" if ok else "/preferences?email=taken")
    uid = await emails.user_for(who.email)
    user = None
    if uid is not None:
        async with dbm.Session() as s:
            user = await s.get(User, uid)
    if user is None or not admission.admitted(user):
        # Never create a user from Google alone: send them to Telegram, which confirms the address afterwards.
        token = signed.sign(LINK_PURPOSE, {"email": who.email}, LINK_TTL_S)
        return _done(f"/link-telegram?t={quote(token, safe='')}")
    await emails.confirm(user.id, who.email, "google_signin")
    response = _done("/workspace")
    await _start_session(request, response, user.id)
    return response


class LogoutBody(BaseModel):
    all: bool = False


@router.post("/logout", status_code=204)
async def logout(response: Response, body: LogoutBody | None = None,
                 active: sessions.Active = Depends(common.authed)) -> Response:
    if body is not None and body.all:
        await sessions.delete_all(active.user.id)
    else:
        await sessions.delete_one(active.token)
    out = Response(status_code=204)
    common.clear_cookie(out, sessions.COOKIE)
    return out

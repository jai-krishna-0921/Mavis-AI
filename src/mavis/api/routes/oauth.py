"""Where Google and Slack send the browser back after consent (native connectors).

The state is signed, single use and bound to the user (and, when the dashboard started it, to that
session), so this public endpoint needs no other auth. After a
sign-in the same CONNECTION_CHECK job as /connect/callback runs, so the user gets the usual "connected"
message and the first sync starts the way it does for every other connection. Vendor error text is never
shown or sent to the user.
"""

from __future__ import annotations

import html

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.domain.messages import Outbound
from mavis.domain.policy import Capability
from mavis.store import db as dbm
from mavis.store.models import User
from mavis.store.repo import connections
from mavis.tools.integrations import get_connection_cache, get_provider
from mavis.tools.integrations.actions import GOOGLE_ANCHOR, workspace_enabled
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import OAuthError
from mavis.web import sessions

router = APIRouter()
log = structlog.get_logger()

THROTTLE_S = 10
_NAMES = {NativeProvider.GOOGLE: "Google", NativeProvider.SLACK: "Slack"}

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
background:#0f1115;color:#e8e8e8}}main{{text-align:center;padding:24px}}h1{{font-size:22px}}</style></head>
<body><main><h1>{title}</h1><p>{body}</p></main></body></html>"""

_USER_TEXT = {
    "denied": "{name} sign-in was cancelled, so nothing was connected. Tell me when you want to try again.",
    "expired_state": "That {name} sign-in link expired. Ask me to connect {name} again for a new one.",
    "replayed_state": "That {name} sign-in link was already used or is no longer valid. "
                      "Ask me to connect {name} again for a fresh one.",
    "exchange_failed": "{name} did not complete the sign-in. Ask me to connect {name} again in a moment.",
    "account_taken": "That {name} account is already connected to another Mavis user, so I skipped it.",
    "not_configured": "{name} sign-in is not set up on my side yet.",
}


WEB_ORIGIN = "web"
_NOTHING_ALLOWED = ("You signed in with {account} but did not allow any Google service, so nothing is connected. "
                    "Send /connect google to try again and leave every box ticked.")


def _capability(provider: NativeProvider) -> Capability:
    """The capability a finished consent for this vendor is checked as (the Google anchor when Workspace is
    on, since one consent covers every Google service)."""
    if provider is NativeProvider.SLACK:
        return Capability.SLACK
    return GOOGLE_ANCHOR if workspace_enabled() else Capability.GMAIL


async def _nothing_allowed(integrations: IntegrationProvider, user_id: int) -> bool:
    access = getattr(integrations, "google_access", None)
    found = await access(user_id) if access is not None else None
    return found is not None and not found[0]


def _back_to_dashboard(error: str | None, *, connected: str | None = None) -> RedirectResponse:
    """A connect started from the dashboard returns there: the workspace page shows the outcome."""
    query = f"?error={error}" if error else f"?connected={connected}"
    return RedirectResponse(f"/workspace{query}", status_code=303)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=html.escape(body)), status_code=status)


def _clean(value: object, limit: int = 60) -> str:
    """Third-party or user-chosen text for a page or a message: one line, no control characters, bounded."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def _external(provider: NativeProvider, account: dict) -> str:
    """Which outside account was just linked, in words the person consenting will recognise."""
    if provider is NativeProvider.GOOGLE:
        return _clean(account.get("email")) or "a Google account"
    team = _clean(account.get("team_name"))
    return f"the {team} workspace" if team else "a Slack workspace"


async def _mavis_name(user_id: int) -> str:
    """The Telegram display name of the Mavis account being linked (never an id, token or secret)."""
    async with dbm.Session() as s:
        user = await s.get(User, user_id)
    return _clean(user.name) if user is not None and user.name else "a Mavis account"


async def _tell(user_id: int, text: str) -> None:
    from mavis.tools.integrations.wiring import outbox_notify

    try:
        await outbox_notify(Outbound(user_id=user_id, text=text))
    except Exception as exc:  # noqa: BLE001 - the page still renders; the user can ask again
        log.warning("oauth.notify_failed", error=type(exc).__name__)


async def _remember_identity(user_id: int, provider: NativeProvider, account: dict) -> None:
    """The linked account is the user's own: its address / Slack id map to the user node in the graph.
    Best effort: a failure here must not undo the connection."""
    from mavis.attention.connector_ingest import remember_identity

    try:
        if provider is NativeProvider.GOOGLE:
            await remember_identity(user_id, emails=(str(account.get("email") or ""),))
        else:
            await remember_identity(user_id, slack_ids=(str(account.get("user_id") or ""),),
                                    team=str(account.get("team_id") or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth.identity_failed", provider=provider.value, error=type(exc).__name__)


@router.get("/oauth/{provider}/callback", response_class=HTMLResponse)
async def oauth_callback(
    provider: str, request: Request, code: str | None = None, state: str | None = None,
    error: str | None = None,
    bus: EventBus = Depends(get_bus), integrations: IntegrationProvider = Depends(get_provider),
) -> HTMLResponse:
    oauth = getattr(integrations, "oauth", None)
    try:
        native = NativeProvider(provider)
    except ValueError:
        native = None
    if native is None or oauth is None:
        return _page("Not found", "This sign-in link is not valid.", 404)
    name = _NAMES[native]
    # A consent started from the dashboard is bound to that browser session. The cookie is optional here: a
    # link started from Telegram has no session to match.
    active = await sessions.lookup(request.cookies.get(sessions.COOKIE))
    session_hash = active.session_id if active is not None else None

    if error or not code or not state:
        # Denied, or a malformed return. We learn whose it was only from a valid state; never echo `error`.
        origin = None
        if state:
            try:
                user_id, _, origin = await oauth.deny_with_origin(state, native, session_hash)
            except OAuthError:
                user_id = None
            if user_id is not None:
                await _tell(user_id, _USER_TEXT["denied"].format(name=name))
        if origin == WEB_ORIGIN:
            return _back_to_dashboard(f"{native.value}_denied")
        return _page("Not connected", f"{name} was not connected. You can close this tab.", 400)

    try:
        done = await oauth.complete(state, code, native, session_hash)
    except OAuthError as exc:
        if exc.kind == "session_mismatch":
            log.info("oauth.session_mismatch", provider=provider)  # nothing saved, and nobody is messaged
            return _page("Continue from the dashboard", (
                f"This {name} sign-in was started in a different browser session, so nothing was connected. "
                "Open the Mavis dashboard in the browser where you are signed in, and connect again "
                "from there."), 400)
        if exc.user_id is not None and exc.kind in _USER_TEXT:
            await _tell(exc.user_id, _USER_TEXT[exc.kind].format(name=name))
        log.info("oauth.callback_failed", provider=provider, kind=exc.kind)
        if exc.origin == WEB_ORIGIN:
            return _back_to_dashboard(f"{native.value}_{exc.kind}")
        return _page("Not connected", f"{name} could not be connected. You can close this tab "
                                      "and try again from Telegram.", 400)

    get_connection_cache().invalidate(done.user_id)
    if native is NativeProvider.GOOGLE and await _nothing_allowed(integrations, done.user_id):
        # Every service box was unticked: there is nothing to connect, so do not call it connected.
        await oauth.revoke(done.user_id, native)
        await _tell(done.user_id, _NOTHING_ALLOWED.format(account=_external(native, done.account)))
        if done.origin == WEB_ORIGIN:
            return _back_to_dashboard(f"{native.value}_nothing_allowed")
        return _page("Nothing was allowed", (
            "You did not allow any Google service, so nothing was connected. Go back to Telegram, ask me "
            "to connect Google again, and leave the boxes ticked."), 400)
    await _remember_identity(done.user_id, native, done.account)
    # A granted Google address is not a sign-in identity: anyone can be handed a consent link. An address
    # becomes one only through a Google sign-in by the signed-in user, or the approved Telegram link.
    # Every finished consent is checked and activated (announcement, first sync, polling). A consent that
    # did not start from a /connect link (the dashboard's button) has no pending request yet: make one.
    pending_id = done.pending_id
    if pending_id is None:
        pending_id = await connections.create_pending(done.user_id, _capability(native), "", None)
    now = timeutil.now()
    await bus.enqueue(Job(
        id=f"conncheck:{pending_id}:{int(now.timestamp()) // THROTTLE_S}",
        user_id=done.user_id, kind=JobKind.CONNECTION_CHECK, payload={"pending_id": pending_id},
    ))
    # Login CSRF: a consent link forwarded to someone else links THEIR account to the sender's Mavis user.
    # Both ends are told exactly what was linked to whom, so a surprised person can undo it.
    outside = _external(native, done.account)
    await _tell(done.user_id, f"{name} is connected: {outside}. If that is not yours, tell me to "
                              f"disconnect {name} and I will remove it.")
    if done.origin == WEB_ORIGIN:
        return _back_to_dashboard(None, connected=native.value)
    who = await _mavis_name(done.user_id)
    return _page("Connected", (
        f"{outside} is now linked to the Mavis account of {who}. You can close this tab and head back to "
        f"Telegram. If this is not you, close this tab and remove Mavis from your {name} account's "
        "connected apps."))

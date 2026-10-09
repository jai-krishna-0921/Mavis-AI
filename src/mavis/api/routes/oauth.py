"""Where Google and Slack send the browser back after consent (native connectors).

The state is signed, single use and bound to the user, so this public endpoint needs no other auth. After a
sign-in the same CONNECTION_CHECK job as /connect/callback runs, so the user gets the usual "connected"
message and the first sync starts the way it does for every other connection. Vendor error text is never
shown or sent to the user.
"""

from __future__ import annotations

import html

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.domain.messages import Outbound
from mavis.tools.integrations import get_connection_cache, get_provider
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import OAuthError

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


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=html.escape(body)), status_code=status)


async def _tell(user_id: int, text: str) -> None:
    from mavis.tools.integrations.wiring import outbox_notify

    try:
        await outbox_notify(Outbound(user_id=user_id, text=text))
    except Exception as exc:  # noqa: BLE001 - the page still renders; the user can ask again
        log.warning("oauth.notify_failed", error=type(exc).__name__)


@router.get("/oauth/{provider}/callback", response_class=HTMLResponse)
async def oauth_callback(
    provider: str, code: str | None = None, state: str | None = None, error: str | None = None,
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

    if error or not code or not state:
        # Denied, or a malformed return. We learn whose it was only from a valid state; never echo `error`.
        if state:
            try:
                user_id, _ = await oauth.deny(state, native)
            except OAuthError:
                user_id = None
            if user_id is not None:
                await _tell(user_id, _USER_TEXT["denied"].format(name=name))
        return _page("Not connected", f"{name} was not connected. You can close this tab.", 400)

    try:
        done = await oauth.complete(state, code, native)
    except OAuthError as exc:
        if exc.user_id is not None and exc.kind in _USER_TEXT:
            await _tell(exc.user_id, _USER_TEXT[exc.kind].format(name=name))
        log.info("oauth.callback_failed", provider=provider, kind=exc.kind)
        return _page("Not connected", f"{name} could not be connected. You can close this tab "
                                      "and try again from Telegram.", 400)

    get_connection_cache().invalidate(done.user_id)
    if done.pending_id is not None:
        now = timeutil.now()
        await bus.enqueue(Job(
            id=f"conncheck:{done.pending_id}:{int(now.timestamp()) // THROTTLE_S}",
            user_id=done.user_id, kind=JobKind.CONNECTION_CHECK, payload={"pending_id": done.pending_id},
        ))
    else:
        await _tell(done.user_id, f"{name} is connected.")
    return _page("Connected", f"{name} is connected. You can close this tab and head back to Telegram.")

# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The Google and Slack entries of the provider table, and the vendor hooks they use."""

from __future__ import annotations

from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import GOOGLE_ANCHOR, GOOGLE_CAPABILITIES, workspace_enabled
from mavis.tools.integrations.native import google_scopes as gs
from mavis.tools.integrations.native.providers import (
    AfterSave,
    CatalogEntry,
    Exchange,
    ExchangeFailed,
    ExchangeRequest,
    ProviderSpec,
    RevokeRequest,
    ScopeParam,
    clean_text,
    register,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_REVOKE_URL = "https://slack.com/api/auth.revoke"
SLACK_OPEN_DM_URL = "https://slack.com/api/conversations.open"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# Full Workspace read and write. A broader scope covers the narrower ones (gmail.modify reads, calendar
# covers events, drive covers every Drive read, contacts covers contacts.readonly), so none of the
# narrower variants is asked for. router.ACTION_SCOPES still honours older grants made with them.
GOOGLE_SCOPES = ("openid", "email", "profile") + tuple(
    gs._AUTH + name for name in (
        "gmail.modify", "gmail.send", "gmail.compose",
        "calendar", "drive", "documents", "spreadsheets", "presentations",
        "forms.body.readonly", "forms.responses.readonly",
        "tasks", "contacts", "contacts.other.readonly", "directory.readonly",
        "meetings.space.created", "meetings.space.readonly",
    )
)
SLACK_USER_SCOPES = (
    "channels:history", "groups:history", "im:history", "mpim:history",
    "channels:read", "groups:read", "im:read", "mpim:read",
    "users:read", "users:read.email", "chat:write", "search:read",
)

# Bot scopes: Mavis as a bot user in the workspace (DM and @mention chat, cards, reactions, files).
SLACK_BOT_SCOPES = (
    "chat:write", "im:history", "im:read", "im:write", "app_mentions:read", "reactions:write",
    "users:read", "files:write", "commands",
)

# Vendor error codes that mean the grant itself is gone (the user must reconnect).
GOOGLE_GONE = frozenset({"invalid_grant"})
SLACK_GONE = frozenset({"invalid_refresh_token", "token_revoked", "account_inactive", "invalid_auth",
                        "token_expired", "not_authed", "invalid_grant"})


def _expiry(body: dict):
    try:
        ttl = int(body["expires_in"]) if body.get("expires_in") else None
    except (TypeError, ValueError):
        ttl = None
    return timeutil.now() + timedelta(seconds=ttl) if ttl else None


async def google_exchange(req: ExchangeRequest) -> Exchange:
    body = await req.post(GOOGLE_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": req.code, "redirect_uri": req.redirect_uri,
        "client_id": req.client_id, "client_secret": req.client_secret, "code_verifier": req.verifier})
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise ExchangeFailed
    info = await req.post(GOOGLE_USERINFO_URL, method="GET", headers={"Authorization": f"Bearer {access}"})
    email = info.get("email")
    if not isinstance(email, str) or not email:
        raise ExchangeFailed
    scope = body.get("scope")
    account = {"email": email.lower(), "sub": str(info.get("sub") or ""),
               "scopes": sorted(scope.split()) if isinstance(scope, str) else []}
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    return Exchange(account, access, refresh, _expiry(body))


async def google_revoke(req: RevokeRequest) -> None:
    await req.post(GOOGLE_REVOKE_URL, data={"token": req.refresh_token or req.access_token})


async def slack_exchange(req: ExchangeRequest) -> Exchange:
    body = await req.post(SLACK_TOKEN_URL, data={
        "code": req.code, "redirect_uri": req.redirect_uri,
        "client_id": req.client_id, "client_secret": req.client_secret})
    authed = body.get("authed_user")
    team = body.get("team")
    if body.get("ok") is not True or not isinstance(authed, dict):
        raise ExchangeFailed
    access = authed.get("access_token")
    if not isinstance(access, str) or not access or not authed.get("id"):
        raise ExchangeFailed
    scope = authed.get("scope")
    team = team if isinstance(team, dict) else {}
    account = {"team_id": str(team.get("id") or ""), "team_name": str(team.get("name") or ""),
               "user_id": str(authed["id"]),
               "scopes": sorted(scope.replace(",", " ").split()) if isinstance(scope, str) else []}
    refresh = authed.get("refresh_token") if isinstance(authed.get("refresh_token"), str) else None
    extras: dict = {}
    bot_token = body.get("access_token")
    if isinstance(bot_token, str) and bot_token and body.get("bot_user_id"):
        bot_scope = body.get("scope")
        extras["bot"] = {
            "token": bot_token,
            "account": {"team_id": account["team_id"], "team_name": account["team_name"],
                        "user_id": account["user_id"], "bot_user_id": str(body["bot_user_id"]),
                        "scopes": sorted(bot_scope.replace(",", " ").split())
                        if isinstance(bot_scope, str) else []},
        }
    return Exchange(account, access, refresh, _expiry(authed), extras)


async def slack_revoke(req: RevokeRequest) -> None:
    await req.post(SLACK_REVOKE_URL, headers={"Authorization": f"Bearer {req.access_token}"})


async def slack_after_save(a: AfterSave) -> None:
    """Keep the bot token sealed beside the user grant (provider slack_bot, keyed by team and the authorizing
    user) and note the user's DM with the bot. Best effort: a failure leaves Slack reading working."""
    from mavis.tools.integrations.native.base import NativeProvider
    from mavis.tools.integrations.native.tokens import AccountTaken

    bot = a.exchange.extras.get("bot")
    if not bot:
        return
    account = dict(bot["account"])
    try:
        data = await a.post(SLACK_OPEN_DM_URL, headers={"Authorization": f"Bearer {bot['token']}"},
                            json={"users": account["user_id"]})
        channel = (data.get("channel") or {}).get("id") if isinstance(data, dict) else None
        if isinstance(channel, str) and channel:
            account["dm"] = channel
    except Exception:  # noqa: BLE001 - OAuthError or ExchangeFailed from the capped call: no DM id, still save
        pass
    try:
        await a.tokens.save(a.user_id, NativeProvider.SLACK_BOT, account=account,
                            access_token=bot["token"], refresh_token=None, expires_at=None)
    except AccountTaken:
        pass


def _google_key(account: dict) -> str | None:
    return str(account.get("email") or "").lower()[:255] or None


def _slack_key(account: dict) -> str | None:
    user, team = str(account.get("user_id") or ""), str(account.get("team_id") or "")
    return (f"{team}/{user}" if user and team else user)[:255] or None


def _slack_external(account: dict) -> str:
    team = clean_text(account.get("team_name"))
    return f"the {team} workspace" if team else "a Slack workspace"


async def _google_identity(user_id: int, account: dict) -> None:
    from mavis.attention.connector_ingest import remember_identity

    await remember_identity(user_id, emails=(str(account.get("email") or ""),))


async def _slack_identity(user_id: int, account: dict) -> None:
    from mavis.attention.connector_ingest import remember_identity

    await remember_identity(user_id, slack_ids=(str(account.get("user_id") or ""),),
                            team=str(account.get("team_id") or ""))


GOOGLE_SERVICES = {"mail": gs._MAIL_READ, "calendar": gs._CAL_READ, "drive": gs._DRIVE_META,
                   "docs": gs._DOCS_READ, "sheets": gs._SHEETS_READ, "tasks": gs._TASKS_READ,
                   "meet": gs._MEET_CREATE, "contacts": gs._CONTACTS_READ}
SLACK_SERVICES = {"messages": frozenset({"channels:history", "groups:history", "im:history", "mpim:history"}),
                  "channels": frozenset({"channels:read", "groups:read", "im:read", "mpim:read"}),
                  "people": frozenset({"users:read"}), "search": frozenset({"search:read"}),
                  "post": frozenset({"chat:write"})}

register(ProviderSpec(
    id="google", display_name="Google", brand="Google",
    auth_url=GOOGLE_AUTH_URL, token_url=GOOGLE_TOKEN_URL, revoke_url=GOOGLE_REVOKE_URL,
    scope_params=(ScopeParam("scope", GOOGLE_SCOPES, " "),), pkce=True,
    extra_auth_params={"response_type": "code", "access_type": "offline", "prompt": "consent",
                       "include_granted_scopes": "true"},
    client_id_setting="google_oauth_client_id", client_secret_setting="google_oauth_client_secret",
    gone_codes=GOOGLE_GONE, exchange=google_exchange, revoke=google_revoke,
    account_key=_google_key, account_label=lambda a: a.get("email") or None,
    external_label=lambda a: clean_text(a.get("email")) or "a Google account",
    remember_identity=_google_identity,
    capabilities=GOOGLE_CAPABILITIES, anchor=GOOGLE_ANCHOR, family_enabled=workspace_enabled,
    legacy_capabilities=(Capability.GMAIL, Capability.CALENDAR), family_label="Google Workspace",
    action_scopes=gs.ACTION_SCOPES,
    toolkits=frozenset({"google", "googlesuper", *(c.value for c in GOOGLE_CAPABILITIES)}),
    executor=("google", "GoogleExecutor"), features=gs.GOOGLE_FEATURES,
    abilities="Gmail, Calendar, Drive, Docs, Sheets, Tasks, Contacts and Meet",
    reconnect_hint="send /connect google and leave every box ticked",
    nothing_allowed=("You signed in with {account} but did not allow any Google service, so nothing is "
                     "connected. Send /connect google to try again and leave every box ticked."),
    pollers={Capability.GMAIL: "mavis.tools.integrations.poller:poll_gmail",
             Capability.CALENDAR: "mavis.tools.integrations.poller:poll_calendar"},
    forget_prefixes=("gmail:",),
    memory_sources={"gmail": ("gmail:",), "calendar": ("calendar:",)},
    catalog=CatalogEntry(description="Mail, calendar, drive, docs and more, read and act on your behalf.",
                         services=GOOGLE_SERVICES, icon="google", always_listed=True),
))
register(ProviderSpec(
    id="slack", display_name="Slack", brand="Slack",
    auth_url=SLACK_AUTH_URL, token_url=SLACK_TOKEN_URL, revoke_url=SLACK_REVOKE_URL,
    scope_params=(ScopeParam("scope", SLACK_BOT_SCOPES, ","),
                  ScopeParam("user_scope", SLACK_USER_SCOPES, ",")),
    pkce=False, client_id_setting="slack_client_id", client_secret_setting="slack_client_secret",
    gone_codes=SLACK_GONE, soft_errors=True, exchange=slack_exchange, revoke=slack_revoke,
    after_save=slack_after_save, companions=("slack_bot",),
    account_key=_slack_key, account_label=lambda a: a.get("team_name") or a.get("team_id") or None,
    external_label=_slack_external, remember_identity=_slack_identity,
    capabilities=(Capability.SLACK,), anchor=Capability.SLACK,
    toolkits=frozenset({"slack"}), executor=("slack", "SlackExecutor"),
    pollers={Capability.SLACK: "mavis.tools.integrations.poller:poll_slack"},
    forget_prefixes=("slack:",), memory_sources={"slack": ("slack:",)},
    catalog=CatalogEntry(description="Read channels you invite Mavis to and message you there.",
                         services=SLACK_SERVICES, icon="slack", always_listed=True),
))
register(ProviderSpec(id="slack_bot", display_name="Slack", brand="Slack", internal=True,
                      account_key=_slack_key))

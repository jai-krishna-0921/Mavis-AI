# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Microsoft 365 as one ProviderSpec: Entra v2 endpoints on the common authority, delegated Graph scopes."""

from __future__ import annotations

import base64
import json
import re
from datetime import timedelta
from urllib.parse import urlencode

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.providers import (
    CatalogEntry,
    Exchange,
    ExchangeFailed,
    ExchangeRequest,
    ProviderSpec,
    ScopeParam,
)

AUTHORITY = "https://login.microsoftonline.com"
MS_AUTH_URL = f"{AUTHORITY}/common/oauth2/v2.0/authorize"
MS_TOKEN_URL = f"{AUTHORITY}/common/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # the consumer (outlook.com, hotmail) tenant
MS_SCOPES = (
    "openid", "profile", "email", "offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send",
    "Calendars.ReadWrite", "Chat.ReadWrite", "ChatMessage.Send", "Files.Read.All",
)
_POLL = "mavis.tools.integrations.microsoft_poll"
MS_CAPABILITIES = (C.OUTLOOK, C.OUTLOOK_CALENDAR, C.TEAMS, C.ONEDRIVE)
NEEDS = {  # capability -> any-of scopes (lowercase) the grant must hold to serve it
    C.OUTLOOK: {"mail.readwrite"}, C.OUTLOOK_CALENDAR: {"calendars.readwrite"},
    C.TEAMS: {"chat.readwrite"}, C.ONEDRIVE: {"files.read.all"},
}
_GUID = re.compile(r"^[0-9a-fA-F-]{8,40}$")
_ADMIN_CODES = ("AADSTS65001", "AADSTS90094", "AADSTS90095", "AADSTS50105")


def _scope_names(raw: object) -> list[str]:
    names = raw.split() if isinstance(raw, str) else []
    return sorted({n.rsplit("/", 1)[-1] for n in names})   # Graph may answer https://graph.microsoft.com/Mail.ReadWrite


def _claims(id_token: object) -> dict:
    """Payload of the ID token. Not signature-checked: it came straight from the token endpoint over TLS in
    answer to our own code exchange (OIDC Core 3.1.3.7 allows that); `aud` is still checked by the caller."""
    try:
        payload = str(id_token).split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise ExchangeFailed("id_token") from None
    if not isinstance(data, dict):
        raise ExchangeFailed("id_token")
    return data


async def exchange(req: ExchangeRequest) -> Exchange:
    body = await req.post(MS_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": req.code, "redirect_uri": req.redirect_uri,
        "client_id": req.client_id, "client_secret": req.client_secret, "code_verifier": req.verifier,
        "scope": " ".join(MS_SCOPES)})
    access = body.get("access_token")
    claims = _claims(body.get("id_token"))
    if not isinstance(access, str) or not access or claims.get("aud") != req.client_id:
        raise ExchangeFailed("token")
    tid = str(claims.get("tid") or "")
    # consumer tokens may omit oid; sub is per app but stable
    oid = str(claims.get("oid") or claims.get("sub") or "")
    if not _GUID.match(tid) or not oid:
        raise ExchangeFailed("identity")
    mail = str(claims.get("email") or claims.get("preferred_username") or "")
    try:   # the mailbox address is more reliable than the optional email claim; best effort
        me = await req.post(f"{GRAPH}/me?$select=mail,userPrincipalName", method="GET",
                            headers={"Authorization": f"Bearer {access}"})
        mail = str(me.get("mail") or me.get("userPrincipalName") or mail)
    except Exception:  # noqa: BLE001 - identity from the token is enough
        pass
    ttl = body.get("expires_in")
    has_ttl = isinstance(ttl, (int, str)) and str(ttl).isdigit()
    expires = timeutil.now() + timedelta(seconds=int(ttl)) if has_ttl else None
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    return Exchange(
        account={"tid": tid, "oid": oid, "mail": mail.lower(), "scopes": _scope_names(body.get("scope"))},
        access_token=access, refresh_token=refresh, expires_at=expires)


def denial_kind(error: str | None, description: str | None) -> str:
    text = f"{error or ''} {description or ''}"
    if any(code in text for code in _ADMIN_CODES) or error in ("consent_required", "admin_consent_required"):
        return "admin_consent"
    return "denied"


def admin_link() -> str | None:
    s = get_settings()
    if not s.microsoft_client_id:
        return None
    return f"{AUTHORITY}/organizations/v2.0/adminconsent?" + urlencode({
        "client_id": s.microsoft_client_id, "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": f"{s.public_base_url.rstrip('/')}/oauth/microsoft/callback", "state": "admin"})


async def remember_microsoft_identity(user_id: int, account: dict) -> None:
    """The linked mailbox is the user's own: its address maps to the user node (so Outlook and Teams records
    from it are the user's own words, exactly like Gmail's)."""
    from mavis.attention.connector_ingest import (
        remember_identity,  # lazy: attention imports the tools package
    )

    if account.get("mail"):
        await remember_identity(user_id, emails=(str(account["mail"]),))


def grant_note(account: dict) -> str | None:
    """A short line about what this account cannot do, for the card and the announcement."""
    if account.get("tid") == PERSONAL_TENANT:
        return "Teams is not available for personal Microsoft accounts."
    scopes = account.get("scopes")
    if isinstance(scopes, list) and "chat.readwrite" not in {str(s).lower() for s in scopes}:
        return "Teams was not allowed on this account."
    return None


def caps_for_grant(grant) -> tuple[C, ...]:
    have = {s.lower() for s in grant.scopes}
    personal = grant.account.get("tid") == PERSONAL_TENANT
    return tuple(c for c in MS_CAPABILITIES
                 if NEEDS[c] & have and not (personal and c is C.TEAMS))


MS_SPEC = providers.register(ProviderSpec(
    id="microsoft", display_name="Microsoft 365", brand="Microsoft",
    # Entra has no revocation endpoint for delegated tokens
    auth_url=MS_AUTH_URL, token_url=MS_TOKEN_URL, revoke_url=None,
    scope_params=(ScopeParam("scope", MS_SCOPES, " "),), pkce=True,
    # response_type=code is REQUIRED by Entra and the generic builder does not add it: it only emits
    # response_type when the spec supplies it here, and then emits it first in the query.
    extra_auth_params={"response_type": "code", "response_mode": "query", "prompt": "select_account"},
    client_id_setting="microsoft_client_id", client_secret_setting="microsoft_client_secret",
    refresh_params={"scope": " ".join(MS_SCOPES)},
    gone_codes=frozenset({"invalid_grant", "interaction_required"}),
    exchange=exchange, denial_kind=denial_kind, admin_link=admin_link,
    remember_identity=remember_microsoft_identity,
    account_key=lambda a: f"{a['tid']}/{a['oid']}" if a.get("tid") and a.get("oid") else None,
    account_label=lambda a: a.get("mail") or None,
    external_label=lambda a: a.get("mail") or "a Microsoft account",
    capabilities=MS_CAPABILITIES, anchor=C.OUTLOOK, capabilities_for=caps_for_grant,
    family_label="Microsoft 365",
    toolkits=frozenset({"microsoft", "microsoft365", "m365", "outlook", "teams", "onedrive",
                        *(c.value for c in MS_CAPABILITIES)}),
    executor=("microsoft", "MicrosoftExecutor"),
    pollers={C.OUTLOOK: f"{_POLL}:poll_mail", C.OUTLOOK_CALENDAR: f"{_POLL}:poll_calendar",
             C.TEAMS: f"{_POLL}:poll_teams", C.ONEDRIVE: f"{_POLL}:poll_onedrive"},
    abilities="Outlook mail, Outlook Calendar, Teams chats and OneDrive", grant_note=grant_note,
    ability_labels={C.OUTLOOK: "Outlook mail", C.OUTLOOK_CALENDAR: "Outlook Calendar",
                    C.TEAMS: "Teams chats", C.ONEDRIVE: "OneDrive"},
    reconnect_hint="send /connect microsoft and accept every permission",
    forget_prefixes=("outlook:", "ocal:", "teams:", "onedrive:"),
    memory_sources={"outlook": ("outlook:",), "outlook_calendar": ("ocal:",),
                    "teams": ("teams:",), "onedrive": ("onedrive:",)},
    catalog=CatalogEntry(  # keyword-only: CatalogEntry is kw_only=True
        description="Outlook mail, Outlook Calendar, Teams chats and OneDrive files.",
        services={"outlook": frozenset({"Mail.ReadWrite"}), "calendar": frozenset({"Calendars.ReadWrite"}),
                  "teams": frozenset({"Chat.ReadWrite"}), "onedrive": frozenset({"Files.Read.All"})},
        icon="microsoft", name="Microsoft 365", chips=True),
    user_text={
        "admin_consent": (
            "Your organisation needs an admin to approve Mavis AI before {name} can be connected. Forward "
            "this link to your IT admin: {admin_link} Once they approve, tell me to connect Microsoft 365 "
            "again."),
        "admin_approved": "Thank you. The approval is recorded. The person who asked can now connect "
                          "Microsoft 365 again from Mavis AI. You can close this tab.",
    },
))

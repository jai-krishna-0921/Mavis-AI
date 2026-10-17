# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Fathom as one ProviderSpec, plus its per-user webhook: the webhook id, the route token and the signing
secret live in a sealed companion grant (provider fathom_hook), removed with the Fathom grant."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

import httpx

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import (
    AfterSave,
    CatalogEntry,
    Exchange,
    ExchangeFailed,
    ExchangeRequest,
    ProviderSpec,
    RevokeRequest,
    ScopeParam,
)

log = logging.getLogger(__name__)

FATHOM_API = "https://api.fathom.ai/external/v1"
FATHOM_AUTH_URL = "https://fathom.video/external/v1/oauth2/authorize"
FATHOM_TOKEN_URL = f"{FATHOM_API}/oauth2/token"
FATHOM_WEBHOOKS_URL = f"{FATHOM_API}/webhooks"
HOOK = NativeProvider.FATHOM_HOOK


async def exchange(req: ExchangeRequest) -> Exchange:
    body = await req.post(FATHOM_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": req.code, "client_id": req.client_id,
        "client_secret": req.client_secret, "redirect_uri": req.redirect_uri})
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise ExchangeFailed
    ttl = body.get("expires_in")
    expires = (timeutil.now() + timedelta(seconds=int(ttl))
               if isinstance(ttl, (int, str)) and str(ttl).isdigit() else None)
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    return Exchange(account={"scopes": ["public_api"]}, access_token=access, refresh_token=refresh,
                    expires_at=expires)


async def hook_owner(tokens, route_token: str) -> int | None:
    """The user whose ACTIVE webhook companion holds this route token."""
    return await tokens.owner_of_key(HOOK, route_token)


async def hook_secret(tokens, user_id: int) -> str | None:
    """The sealed `whsec_...` signing secret of this user's webhook, or None."""
    sealed = await tokens.reveal(user_id, HOOK)
    return (sealed[0] or None) if sealed is not None else None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_webhook(post, token: str, webhook_id: str) -> None:
    """Best effort DELETE of one webhook; only the status code is ever logged."""
    try:
        resp = await post(f"{FATHOM_WEBHOOKS_URL}/{webhook_id}", method="DELETE", headers=_auth(token))
    except Exception:  # noqa: BLE001 - OAuthError or a transport failure: the webhook is not worth a failed call
        log.info("fathom.webhook_delete_failed")
        return
    status = getattr(resp, "status_code", None)
    if status is not None and status not in (204, 404):
        log.info("fathom.webhook_delete_status", extra={"status": status})


async def fathom_after_save(a: AfterSave) -> None:
    """Create the user's webhook and seal its secret in the companion grant. Best effort: without it the
    connection still works and polling covers the gap."""
    from mavis.tools.integrations.native.tokens import AccountTaken

    token = a.exchange.access_token
    old = await a.tokens.account(a.user_id, HOOK)
    if old and old.get("webhook_id"):  # a reconnect replaces the old webhook first
        await _delete_webhook(a.post, token, str(old["webhook_id"]))
    route_token = secrets.token_urlsafe(24)
    base = get_settings().public_base_url.rstrip("/")
    try:
        body = await a.post(FATHOM_WEBHOOKS_URL, headers=_auth(token), json={
            "destination_url": f"{base}/webhooks/fathom/{route_token}",
            "triggered_for": ["my_recordings"], "include_action_items": True, "include_summary": True,
            "include_transcript": False, "include_crm_matches": False})
    except Exception:  # noqa: BLE001 - the capped call raises OAuthError on any non-2xx or transport failure
        log.info("fathom.webhook_create_failed")
        return
    webhook_id, signing = body.get("id"), body.get("secret")
    if not isinstance(webhook_id, str) or not webhook_id or not isinstance(signing, str) or not signing:
        log.info("fathom.webhook_create_bad_shape")
        return
    try:
        await a.tokens.save(a.user_id, HOOK, account={"webhook_id": webhook_id, "route_token": route_token,
                                                      "scopes": []},
                            access_token=signing, refresh_token=None, expires_at=None)
    except AccountTaken:
        pass


async def fathom_revoke(r: RevokeRequest) -> None:
    """Delete the webhook at Fathom (the vendor has no token revoke). The companion row is removed by the
    generic revoke path right after."""
    if r.tokens is None:
        return
    account = await r.tokens.account(r.user_id, HOOK)
    webhook_id = str((account or {}).get("webhook_id") or "")
    if not webhook_id:
        return
    url = f"{FATHOM_WEBHOOKS_URL}/{webhook_id}"
    token = r.access_token
    try:
        for force in (False, True):  # the stored token may be expired: refresh first, once more after a 401
            try:
                token = await r.tokens.access_token(r.user_id, NativeProvider.FATHOM, force=force)
            except Exception:  # noqa: BLE001 - ReauthRequired or a refresh failure: try the stored token
                pass
            resp = await r.post(url, method="DELETE", headers=_auth(token))
            if resp.status_code != 401:
                break
    except httpx.HTTPError:
        log.info("fathom.webhook_delete_failed")
        return
    if resp.status_code not in (204, 404):
        log.info("fathom.webhook_delete_status", extra={"status": resp.status_code})


FATHOM_SPEC = providers.register(ProviderSpec(
    id="fathom", display_name="Fathom", brand="Fathom",
    auth_url=FATHOM_AUTH_URL, token_url=FATHOM_TOKEN_URL, revoke_url=None,  # Fathom has no revoke endpoint
    scope_params=(ScopeParam("scope", ("public_api",)),), pkce=False,
    extra_auth_params={"response_type": "code"},
    client_id_setting="fathom_client_id", client_secret_setting="fathom_client_secret",
    token_auth="body", gone_codes=frozenset({"invalid_grant"}),
    exchange=exchange, revoke=fathom_revoke, after_save=fathom_after_save, companions=("fathom_hook",),
    external_label=lambda a: "a Fathom account",
    capabilities=(C.FATHOM,), anchor=C.FATHOM, toolkits=frozenset({"fathom"}),
    executor=("fathom", "FathomExecutor"),
    abilities="your Fathom meeting notes and action items",
    forget_prefixes=("fathom:",), state_cursors=("fathom_",), memory_sources={"fathom": ("fathom:",)},
    catalog=CatalogEntry(
        description="Meeting notes and action items from your Fathom recordings.",
        services={"fathom": frozenset({"public_api"})}, icon="fathom", name="Fathom"),
))
FATHOM_HOOK_SPEC = providers.register(ProviderSpec(
    id="fathom_hook", display_name="Fathom", brand="Fathom", internal=True,
    account_key=lambda a: str(a.get("route_token") or "") or None))

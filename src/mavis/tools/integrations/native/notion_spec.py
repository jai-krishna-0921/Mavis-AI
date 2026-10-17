# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Notion as one ProviderSpec: a public integration authorised per user, Basic-auth JSON token calls."""

from __future__ import annotations

import base64
from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.providers import (
    CatalogEntry,
    Exchange,
    ExchangeFailed,
    ExchangeRequest,
    ProviderSpec,
    clean_text,
)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
NOTION_AUTH_URL = f"{NOTION_API}/oauth/authorize"
NOTION_TOKEN_URL = f"{NOTION_API}/oauth/token"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


async def exchange(req: ExchangeRequest) -> Exchange:
    basic = base64.b64encode(f"{req.client_id}:{req.client_secret}".encode()).decode()
    body = await req.post(NOTION_TOKEN_URL, headers={"Authorization": f"Basic {basic}"}, json={
        "grant_type": "authorization_code", "code": req.code, "redirect_uri": req.redirect_uri})
    access = body.get("access_token")
    owner = body.get("owner")
    user = owner.get("user") if isinstance(owner, dict) and owner.get("type") == "user" else None
    owner_id = _text(user.get("id")) if isinstance(user, dict) else ""
    workspace_id = _text(body.get("workspace_id"))
    if not isinstance(access, str) or not access or not owner_id or not workspace_id:
        raise ExchangeFailed("token")
    person = user.get("person") if isinstance(user.get("person"), dict) else {}
    me = await req.post(f"{NOTION_API}/users/me", method="GET", headers={
        "Authorization": f"Bearer {access}", "Notion-Version": NOTION_VERSION})
    bot_user_id = _text(me.get("id"))
    if not bot_user_id:
        raise ExchangeFailed("identity")
    ttl = body.get("expires_in")
    expires = (timeutil.now() + timedelta(seconds=int(ttl))
               if isinstance(ttl, (int, str)) and str(ttl).isdigit() else None)
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    return Exchange(
        account={"workspace_id": workspace_id, "workspace_name": clean_text(body.get("workspace_name")),
                 "owner_id": owner_id, "owner_email": clean_text(person.get("email"), 254),
                 "bot_user_id": bot_user_id, "template_page_id": _text(body.get("duplicated_template_id")),
                 "scopes": ["notion"]},
        access_token=access, refresh_token=refresh, expires_at=expires)


def _key(account: dict) -> str | None:
    w, o = account.get("workspace_id"), account.get("owner_id")
    return f"{w}/{o}" if w and o else None


def _external(account: dict) -> str:
    name = clean_text(account.get("workspace_name"))
    return f"the {name} workspace" if name else "a Notion workspace"


NOTION_SPEC = providers.register(ProviderSpec(
    id="notion", display_name="Notion", brand="Notion",
    auth_url=NOTION_AUTH_URL, token_url=NOTION_TOKEN_URL, revoke_url=None,  # Notion has no revoke endpoint
    scope_params=(), pkce=False, extra_auth_params={"response_type": "code", "owner": "user"},
    client_id_setting="notion_client_id", client_secret_setting="notion_client_secret",
    token_auth="basic_json", gone_codes=frozenset({"invalid_grant", "unauthorized_client"}),
    exchange=exchange,
    account_key=_key,
    account_label=lambda a: a.get("workspace_name") or None,
    external_label=_external,
    capabilities=(C.NOTION,), anchor=C.NOTION, toolkits=frozenset({"notion"}),
    pollers={C.NOTION: "mavis.tools.integrations.notion_sync.runner:poll_notion"},
    executor=("notion", "NotionExecutor"),
    abilities="your Todos, Meetings, Briefings, People and Projects databases",
    forget_prefixes=("notion:",), state_cursors=("notion_",), memory_sources={"notion": ("notion:",)},
    catalog=CatalogEntry(
        description="Your todos, meetings, people and projects, laid out in your Notion.",
        services={"notion": frozenset({"notion"})}, icon="notion", name="Notion"),
))

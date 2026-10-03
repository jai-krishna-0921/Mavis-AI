"""Composio REST v3 adapter.

TEMPORARY: swap for direct Google/Slack/Notion adapters behind the same port.

Patterns carried over from ~/Desktop/comarketer (revenue_intel/integrations.py,
scripts/composio_mcp_server.py):
- managed auth config found-or-created per toolkit; connect via POST /connected_accounts/link
- explicit identity (mavis-<id>), never the first ACTIVE account on the key
- curated slugs only; provider errors surface as status codes, never bodies (they can echo the key)
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from mavis.domain.errors import IntegrationError
from mavis.domain.events import Event
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS, COMPOSIO_TRIGGERS, toolkit_of_slug

CATALOG: tuple[Toolkit, ...] = (
    Toolkit(slug="gmail", name="Gmail", description="Read, triage and draft email."),
    Toolkit(
        slug="googlecalendar", name="Google Calendar", description="Events, availability and scheduling."
    ),
    Toolkit(slug="slack", name="Slack", description="Messages and channels."),
    Toolkit(slug="notion", name="Notion", description="Pages and notes."),
)
_SLUGS = {t.slug for t in CATALOG}
_STATE_MAP = {
    "ACTIVE": ConnectionState.ACTIVE,
    "INITIATED": ConnectionState.INITIATED,
    "INITIALIZING": ConnectionState.INITIATED,
    "FAILED": ConnectionState.FAILED,
    "EXPIRED": ConnectionState.FAILED,
    "INACTIVE": ConnectionState.FAILED,
}
# Composio expires a connect link nobody finished after 10 minutes as EXPIRED, with one of these
# reasons. That account was never authorized: it is "not connected", not "expired" or "revoked".
_ABANDONED_REASONS = ("before authorization was started", "started but not completed")


def _abandoned(item: dict[str, Any]) -> bool:
    if str(item.get("status")) not in ("EXPIRED", "FAILED"):
        return False
    reason = str(item.get("status_reason") or "").lower()
    return any(r in reason for r in _ABANDONED_REASONS)


class ComposioProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://backend.composio.dev/api/v3",
        webhook_secret: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._webhook_secret = webhook_secret
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None
        self._auth_configs: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"x-api-key": self._api_key, "Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        if not self._api_key:
            raise IntegrationError("COMPOSIO_API_KEY is not set, so no account can be connected.")
        try:
            resp = await self._http().request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            # `from None`: httpx exceptions carry the request (and its headers) - never chain them.
            raise IntegrationError(f"could not reach Composio: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise IntegrationError(f"Composio answered {resp.status_code} for {method} {path}") from None
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise IntegrationError(f"Composio answered {resp.status_code} with a non-JSON body") from None
        if not isinstance(data, dict):
            raise IntegrationError(f"Composio answered {resp.status_code} with an unexpected JSON shape")
        return data

    # --- connections -------------------------------------------------------------------------------

    async def catalog(self) -> list[Toolkit]:
        return list(CATALOG)

    async def _auth_config(self, toolkit: str) -> str:
        if toolkit in self._auth_configs:
            return self._auth_configs[toolkit]
        found = await self._request("GET", "/auth_configs", params={"toolkit_slug": toolkit, "limit": 10})
        config_id = next(
            (str(i.get("id")) for i in found.get("items") or [] if str(i.get("status")) == "ENABLED"), ""
        )
        if not config_id:
            created = await self._request("POST", "/auth_configs", body={
                "toolkit": {"slug": toolkit}, "auth_config": {"type": "use_composio_managed_auth"},
            })
            config_id = str((created.get("auth_config") or {}).get("id") or "")
        if not config_id:
            raise IntegrationError(f"{toolkit} has no usable auth config and one could not be created.")
        self._auth_configs[toolkit] = config_id
        return config_id

    async def _accounts(self, user: UserRef) -> dict[str, dict[str, Any]]:
        """Newest account per toolkit for exactly this identity; a newer non-ACTIVE never hides an ACTIVE.
        Abandoned connect attempts (links never finished) are skipped: they were never accounts."""
        answer = await self._request(
            "GET", "/connected_accounts", params={"user_ids": user.provider_id, "limit": 100}
        )
        items = sorted(
            answer.get("items") or [],
            key=lambda i: str(i.get("created_at") or i.get("createdAt") or ""),
        )
        best: dict[str, dict[str, Any]] = {}
        for item in items:
            if str(item.get("user_id") or user.provider_id) != user.provider_id or _abandoned(item):
                continue
            slug = str((item.get("toolkit") or {}).get("slug") or "").lower()
            if not slug:
                continue
            current = best.get(slug)
            if current is None or item.get("status") == "ACTIVE" or current.get("status") != "ACTIVE":
                best[slug] = item
        return best

    async def status(self, user: UserRef) -> dict[str, ConnectionState]:
        states = {t.slug: ConnectionState.NONE for t in CATALOG}
        if not self.configured:
            return states
        for slug, item in (await self._accounts(user)).items():
            if slug in states:
                states[slug] = _STATE_MAP.get(str(item.get("status")), ConnectionState.NONE)
        return states

    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str:
        toolkit = (toolkit or "").strip().lower()
        if toolkit not in _SLUGS:
            raise IntegrationError(
                f"{toolkit!r} is not one of the services Mavis connects: {sorted(_SLUGS)}."
            )
        answer = await self._request("POST", "/connected_accounts/link", body={
            "auth_config_id": await self._auth_config(toolkit),
            "user_id": user.provider_id,
            "callback_url": callback_url,
        })
        url = str(answer.get("redirect_url") or "")
        if not url:
            raise IntegrationError(f"Composio started a {toolkit} connection but returned no consent URL.")
        return url

    async def disconnect(self, user: UserRef, toolkit: str) -> None:
        row = (await self._accounts(user)).get((toolkit or "").strip().lower())
        if not row:
            raise IntegrationError(f"there is no {toolkit} connection to remove.")
        await self._request("DELETE", f"/connected_accounts/{row.get('id')}")

    # --- tools --------------------------------------------------------------------------------------

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        spec = ACTIONS.get(action)
        mapping = COMPOSIO_ACTIONS.get(action)
        if spec is None or mapping is None:
            return ToolResult(ok=False, error=f"unknown action {action!r}")
        try:
            parsed = spec.args_model.model_validate(args)
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
            return ToolResult(ok=False, error=f"invalid arguments for {action}: {fields}")
        try:
            answer = await self._request(
                "POST", f"/tools/execute/{mapping.slug}",
                body={"user_id": user.provider_id, "arguments": mapping.translate(parsed)},
            )
        except IntegrationError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not answer.get("successful", False):
            reason = str(answer.get("error") or "the provider reported a failure")
            return ToolResult(ok=False, error=reason[:300])
        return ToolResult(ok=True, data=answer.get("data"))

    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str:
        slug = COMPOSIO_TRIGGERS.get(trigger)
        if slug is None:
            raise IntegrationError(f"unknown trigger {trigger!r}")
        account = (await self._accounts(user)).get(toolkit_of_slug(slug))
        if not account or account.get("status") != "ACTIVE":
            raise IntegrationError(f"no ACTIVE {toolkit_of_slug(slug)} connection to attach {trigger} to.")
        answer = await self._request(
            "POST", f"/trigger_instances/{slug}/upsert",
            body={"connected_account_id": account.get("id"), "trigger_config": config},
        )
        trigger_id = str(answer.get("trigger_id") or answer.get("id") or "")
        if not trigger_id:
            raise IntegrationError(f"Composio accepted {trigger} but returned no trigger id.")
        return trigger_id

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]:
        from mavis.tools.integrations.composio_webhooks import parse_composio_webhook

        return parse_composio_webhook(headers, body, self._webhook_secret)

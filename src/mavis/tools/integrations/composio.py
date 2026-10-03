"""Composio REST v3 adapter.

TEMPORARY: swap for direct Google/Slack/Notion adapters behind the same port.

Patterns carried over from ~/Desktop/comarketer (revenue_intel/integrations.py,
scripts/composio_mcp_server.py):
- managed auth config found-or-created per toolkit; connect via POST /connected_accounts/link
- explicit identity (mavis-<id>), never the first ACTIVE account on the key
- curated slugs only; provider errors surface as status codes, never bodies (they can echo the key)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from mavis.config import get_settings
from mavis.domain.errors import IntegrationError, NoSuchConnection
from mavis.domain.events import Event
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import ACTIONS, GOOGLE_CAPABILITIES, WORKSPACE_CAPABILITIES
from mavis.tools.integrations.composio_map import (
    COMPOSIO_ACTIONS,
    COMPOSIO_TRIGGERS,
    GOOGLESUPER,
    GOOGLESUPER_TRIGGERS,
    LEGACY_ALIASES,
    LEGACY_TOOLKITS,
    slug_for,
    toolkit_of_slug,
)

# Hosts Composio's presigned upload URLs may point at (its OpenAPI: storage_backend s3 or azure_blob_storage).
STORAGE_HOST_SUFFIXES = (".amazonaws.com", ".composio.dev", ".blob.core.windows.net")
UPLOAD_LIMIT = 5 * 1024 * 1024  # googlesuper UPLOAD_FILE takes at most 5 MB


def _storage_host_ok(url: str) -> bool:
    """https only, a DNS name (no IP literal, no localhost) under a known storage domain."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return host.endswith(STORAGE_HOST_SUFFIXES)


CATALOG: tuple[Toolkit, ...] = (
    Toolkit(slug="gmail", name="Gmail", description="Read, triage and draft email."),
    Toolkit(
        slug="googlecalendar", name="Google Calendar", description="Events, availability and scheduling."
    ),
    Toolkit(slug="slack", name="Slack", description="Messages and channels."),
    Toolkit(slug="notion", name="Notion", description="Pages and notes."),
)
_SLUGS = {t.slug for t in CATALOG}
GOOGLE_TOOLKIT = Toolkit(
    slug=GOOGLESUPER, name="Google Workspace",
    description="Gmail, Calendar, Drive, Docs, Sheets, Tasks, Contacts and Meet with one consent.",
)
# Names that mean "the one Google account" when Workspace is on (connect and disconnect).
_GOOGLE_NAMES = frozenset({"google", GOOGLESUPER, *(c.value for c in GOOGLE_CAPABILITIES)})
_LEGACY_TRIGGER_PREFIXES = ("GMAIL_", "GOOGLECALENDAR_")
ROUTE_TTL_S = 30.0  # execute() re-reads account states at most this often per user
_STATE_MAP = {
    "ACTIVE": ConnectionState.ACTIVE,
    "INITIATED": ConnectionState.INITIATED,
    "INITIALIZING": ConnectionState.INITIATED,
    "FAILED": ConnectionState.FAILED,
    "EXPIRED": ConnectionState.FAILED,
    "INACTIVE": ConnectionState.FAILED,
}


def _pick_toolkit(statuses: dict[str, str], legacy: str) -> str:
    """googlesuper when ACTIVE, else the legacy toolkit when ACTIVE, else googlesuper (to report it)."""
    if statuses.get(GOOGLESUPER) == "ACTIVE":
        return GOOGLESUPER
    return legacy if statuses.get(legacy) == "ACTIVE" else GOOGLESUPER


class ComposioProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://backend.composio.dev/api/v3",
        webhook_secret: str = "",
        timeout_s: float = 30.0,
        workspace: bool = False,
    ) -> None:
        self._api_key = api_key.strip()
        self._workspace = workspace
        self._routes: dict[str, tuple[float, dict[str, str]]] = {}  # provider id -> (expiry, toolkit->status)
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
        if self._workspace:
            return [GOOGLE_TOOLKIT, *(t for t in CATALOG if t.slug not in LEGACY_TOOLKITS.values())]
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
        """Newest account per toolkit for exactly this identity; a newer non-ACTIVE never hides an ACTIVE."""
        answer = await self._request(
            "GET", "/connected_accounts", params={"user_ids": user.provider_id, "limit": 100}
        )
        items = sorted(
            answer.get("items") or [],
            key=lambda i: str(i.get("created_at") or i.get("createdAt") or ""),
        )
        best: dict[str, dict[str, Any]] = {}
        for item in items:
            if str(item.get("user_id") or user.provider_id) != user.provider_id:
                continue
            slug = str((item.get("toolkit") or {}).get("slug") or "").lower()
            if not slug:
                continue
            current = best.get(slug)
            if current is None or item.get("status") == "ACTIVE" or current.get("status") != "ACTIVE":
                best[slug] = item
        return best

    def _remember(self, user: UserRef, accounts: dict[str, dict[str, Any]]) -> None:
        statuses = {slug: str(item.get("status")) for slug, item in accounts.items()}
        self._routes[user.provider_id] = (time.monotonic() + ROUTE_TTL_S, statuses)

    async def _route_statuses(self, user: UserRef) -> dict[str, str]:
        hit = self._routes.get(user.provider_id)
        if hit is not None and time.monotonic() < hit[0]:
            return hit[1]
        accounts = await self._accounts(user)
        self._remember(user, accounts)
        return self._routes[user.provider_id][1]

    async def status(self, user: UserRef) -> dict[str, ConnectionState]:
        states = {t.slug: ConnectionState.NONE for t in CATALOG}
        if self._workspace:
            states.update({c.value: ConnectionState.NONE for c in WORKSPACE_CAPABILITIES})
        if not self.configured:
            return states
        accounts = await self._accounts(user)
        self._remember(user, accounts)
        for slug, item in accounts.items():
            if slug in states:
                states[slug] = _STATE_MAP.get(str(item.get("status")), ConnectionState.NONE)
        if not self._workspace:
            return states
        google = ConnectionState.NONE
        if GOOGLESUPER in accounts:
            google = _STATE_MAP.get(str(accounts[GOOGLESUPER].get("status")), ConnectionState.NONE)
        for capability in GOOGLE_CAPABILITIES:
            legacy = states.get(capability.value) if capability in LEGACY_TOOLKITS else None
            if google is ConnectionState.ACTIVE or legacy in (None, ConnectionState.NONE):
                states[capability.value] = google  # one Google account: all eight share its state
            else:
                states[capability.value] = legacy  # Gmail/Calendar keep working on the old connection
        return states

    @staticmethod
    def _read_artifact(path: str) -> bytes:
        """Defence in depth: only a regular file inside the artifacts directory, at most 5 MiB."""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(get_settings().artifacts_dir.resolve()) or not resolved.is_file():
            raise IntegrationError("the file is not an artifact of this app")
        if resolved.stat().st_size > UPLOAD_LIMIT:
            raise IntegrationError("the file is larger than 5 MB")
        return resolved.read_bytes()

    async def _stage_file(self, slug: str, path: str, name: str, mime: str) -> dict[str, str]:
        """Upload a local file to Composio's storage for `slug` and return the file reference the tool takes.
        The presigned PUT goes to the storage host without the API key."""
        data = await asyncio.to_thread(self._read_artifact, path)
        answer = await self._request("POST", "/files/upload/request", body={
            "toolkit_slug": GOOGLESUPER, "tool_slug": slug, "filename": name, "mimetype": mime,
            "md5": hashlib.md5(data).hexdigest(),  # Composio's dedupe key, not a security check
        })
        key = str(answer.get("key") or "")
        if not key:
            raise IntegrationError("Composio returned no storage key for the upload.")
        url = str(answer.get("new_presigned_url") or answer.get("newPresignedUrl") or "")
        if url:  # absent when Composio already holds a file with this md5
            if not _storage_host_ok(url):
                raise IntegrationError("unexpected upload host")
            headers = {"Content-Type": mime}
            if (urlsplit(url).hostname or "").lower().endswith(".blob.core.windows.net"):
                headers["x-ms-blob-type"] = "BlockBlob"  # Azure storage backend requires it
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as storage:
                    resp = await storage.put(url, content=data, headers=headers)
            except httpx.HTTPError as exc:
                raise IntegrationError(f"could not upload the file: {type(exc).__name__}") from None
            if resp.status_code >= 400:
                raise IntegrationError(f"file storage answered {resp.status_code} for the upload") from None
        return {"name": name, "mimetype": mime, "s3key": key}

    async def _toolkit_for(self, user: UserRef, capability: Capability, legacy_slug: str) -> str:
        if not self._workspace or capability not in GOOGLE_CAPABILITIES:
            return toolkit_of_slug(legacy_slug)
        if capability not in LEGACY_TOOLKITS:
            return GOOGLESUPER
        return _pick_toolkit(await self._route_statuses(user), LEGACY_TOOLKITS[capability])

    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str:
        toolkit = (toolkit or "").strip().lower()
        if self._workspace and toolkit in _GOOGLE_NAMES:
            toolkit = GOOGLESUPER  # every Google capability (and "google") opens the one consent
        elif toolkit not in _SLUGS:
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
        name = original = (toolkit or "").strip().lower()
        if self._workspace and name in _GOOGLE_NAMES:
            name = GOOGLESUPER  # every Google capability drops at once; legacy accounts stay
        elif self._workspace and name in LEGACY_ALIASES:
            name = LEGACY_ALIASES[name]
        accounts = await self._accounts(user)
        row = accounts.get(name)
        if not row and name == GOOGLESUPER and original in LEGACY_TOOLKITS.values():
            row = accounts.get(original)  # legacy-only user disconnecting "gmail" or "googlecalendar"
        if not row:
            raise NoSuchConnection(f"there is no {toolkit} connection to remove.")
        await self._request("DELETE", f"/connected_accounts/{row.get('id')}")
        self._routes.pop(user.provider_id, None)  # after the DELETE, so no concurrent execute re-caches it

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
            slug = slug_for(action, await self._toolkit_for(user, spec.capability, mapping.slug))
            arguments = mapping.translate(parsed)
            if mapping.file_arg:
                arguments[mapping.file_arg] = await self._stage_file(
                    slug, parsed.path, parsed.name, parsed.mime  # type: ignore[attr-defined]
                )
            answer = await self._request(
                "POST", f"/tools/execute/{slug}",
                body={"user_id": user.provider_id, "arguments": arguments},
            )
        except IntegrationError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not answer.get("successful", False):
            reason = str(answer.get("error") or "the provider reported a failure")
            return ToolResult(ok=False, error=reason[:300])
        return ToolResult(ok=True, data=answer.get("data"))

    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str:
        google_slug = GOOGLESUPER_TRIGGERS.get(trigger) if self._workspace else None
        legacy = COMPOSIO_TRIGGERS.get(trigger)
        if google_slug is None and legacy is None:
            raise IntegrationError(f"unknown trigger {trigger!r}")
        accounts = await self._accounts(user)
        self._remember(user, accounts)
        if legacy is None:
            if google_slug is None:
                raise IntegrationError(f"unknown trigger {trigger!r}")
            slug, toolkit = google_slug, GOOGLESUPER  # workspace-only triggers exist on googlesuper alone
        elif google_slug is not None:
            statuses = {k: str(v.get("status")) for k, v in accounts.items()}
            toolkit = _pick_toolkit(statuses, toolkit_of_slug(legacy))
            slug = google_slug if toolkit == GOOGLESUPER else legacy
        else:
            slug, toolkit = legacy, toolkit_of_slug(legacy)
        account = accounts.get(toolkit)
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

    async def retire_legacy_triggers(self, user: UserRef) -> int:
        """After the Google upgrade: delete this user's Gmail/Calendar trigger instances on the legacy
        accounts, so every email arrives once (from googlesuper). Returns how many were deleted."""
        answer = await self._request(
            "GET", "/trigger_instances/active", params={"user_ids": user.provider_id, "limit": 100}
        )
        deleted = 0
        for item in answer.get("items") or []:
            name = str(item.get("trigger_name") or item.get("triggerName") or "").upper()
            if str(item.get("user_id") or user.provider_id) != user.provider_id:
                continue
            if name.startswith(_LEGACY_TRIGGER_PREFIXES) and item.get("id"):
                await self._request("DELETE", f"/trigger_instances/manage/{item['id']}")
                deleted += 1
        return deleted

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]:
        from mavis.tools.integrations.composio_webhooks import parse_composio_webhook

        return parse_composio_webhook(headers, body, self._webhook_secret)

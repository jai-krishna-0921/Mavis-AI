# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""NativeRouter: the IntegrationProvider that prefers our own Google and Slack executors.

Per action: the native executor runs it when (a) native is configured for that provider, (b) the user has
an ACTIVE grant covering the action's capability, (c) an executor is registered and handles(action). Anything
else goes to Composio unchanged, so a missing executor, an unconnected user or an action native does not
implement degrades to the old path instead of failing.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

import httpx

from mavis.config import get_settings
from mavis.domain.errors import FailureKind, IntegrationError, NoSuchConnection
from mavis.domain.events import Event
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import ACTIONS
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.native import oauth as oauth_mod
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeExecutor, NativeProvider
from mavis.tools.integrations.native.google_scopes import ACTION_SCOPES  # noqa: F401  (re-exported)
from mavis.tools.integrations.native.http import make_client
from mavis.tools.integrations.native.merge import MERGED, NEW_ITEM, MailCalendarMerge
from mavis.tools.integrations.native.oauth import NativeOAuth
from mavis.tools.integrations.native.tokens import ACTIVE, REVOKED, Grant, NativeTokenStore


def _spec(provider) -> providers.ProviderSpec | None:
    return providers.spec_of(provider)


def feature_access(grant: Grant) -> tuple[list[str], list[str]]:
    """(what this grant lets Mavis do, what it does not), by the grant's own scopes."""
    spec = _spec(grant.provider)
    feats = spec.features if spec is not None else ()
    can = [label for label, action in feats if allows(grant, action)]
    return can, [label for label, _ in feats if label not in can]


def provider_of(capability: Capability) -> NativeProvider | None:
    spec = providers.provider_of_capability(capability)
    try:
        return NativeProvider(spec.id) if spec is not None else None
    except ValueError:
        return None


def allows(grant: Grant, action: str) -> bool:
    """Do this grant's scopes cover everything the action needs? No scope table: any ACTIVE grant covering
    the action's capability."""
    spec = _spec(grant.provider)
    if spec is None:
        return False
    if spec.action_scopes:
        groups = spec.action_scopes.get(action)
        return bool(groups) and all(grant.scopes & group for group in groups)
    found = ACTIONS.get(action)
    return found is not None and covers(grant, found.capability)


def covers(grant: Grant, capability: Capability) -> bool:
    """Can this grant do at least one thing in the capability?"""
    spec = _spec(grant.provider)
    if spec is None or capability not in spec.caps_for(grant):
        return False
    if not spec.action_scopes:
        return True
    return any(ACTIONS[a].capability is capability and allows(grant, a) for a in spec.action_scopes)


class NativeRouter:
    def __init__(self, fallback: IntegrationProvider, tokens: NativeTokenStore, oauth: NativeOAuth,
                 executors: Iterable[NativeExecutor] = (), client: httpx.AsyncClient | None = None) -> None:
        self.fallback, self.tokens, self.oauth = fallback, tokens, oauth
        self.executors: dict[NativeProvider, NativeExecutor] = {e.provider: e for e in executors}
        self._client = client
        self.merge = MailCalendarMerge(self)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        aclose = getattr(self.fallback, "aclose", None)
        if aclose is not None:
            await aclose()

    @property
    def configured(self) -> bool:
        """Can this Mavis connect anything? Native sign-in set up for either vendor counts on its own: a
        native-only deployment (no Composio key) is fully configured. Otherwise ask the fallback."""
        if any(self._ready(p) for p in self.executors):
            return True
        return bool(getattr(self.fallback, "configured", True))

    def can_connect(self, capability: Capability) -> bool:
        """Is there anything that can connect this service? Google and Slack are ours when their native
        sign-in is set up; everything else (Notion) needs the fallback provider."""
        native = provider_of(capability)
        if native is not None and self._ready(native):
            return True
        return bool(getattr(self.fallback, "configured", True))

    # --- routing -------------------------------------------------------------------------------------

    def _ready(self, provider: NativeProvider | None) -> bool:
        return provider is not None and provider in self.executors and oauth_mod.configured(provider)

    async def _active_grant(self, user_id: int, provider: NativeProvider) -> Grant | None:
        g = await self.tokens.grant(user_id, provider)
        return g if g is not None and g.status == ACTIVE else None

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        merged = await self.merge.execute(user, action, args)
        if merged is not None:
            return merged
        if action in NEW_ITEM:  # only the merge layer reads these; no single provider does
            args = {k: v for k, v in args.items() if k not in ("account", "sender_pin")}
        return await self._execute_single(user, action, args)

    async def execute_native(self, provider: NativeProvider, user: UserRef, action: str, args: dict
                             ) -> ToolResult:
        """Run an action on ONE provider's own executor: no merge, no Composio fallback. Sync and poll use
        this so an Outlook read can never be answered from Gmail (or the reverse)."""
        spec = ACTIONS.get(action)
        executor = self.executors.get(provider)
        grant = await self._active_grant(user.user_id, provider) if self._ready(provider) else None
        if spec is None or executor is None or not executor.handles(action) or grant is None:
            return ToolResult(ok=False, error_kind=FailureKind.AUTH,
                              error=f"the {provider.value} account is not connected; "
                                    "the user must reconnect it")
        cap = spec.capability  # mail.* and calendar.* carry Google's capability; Outlook's serves them
        if provider is NativeProvider.MICROSOFT:
            cap = next((ms for google, ms in MERGED.values() if google is cap), cap)
        if not self._permitted(grant, action, cap):
            return ToolResult(ok=False, error_kind=FailureKind.PERMISSION_MISSING,
                              error=f"the connected {provider.value} account was not granted the permission "
                                    f"{action} needs; the user must reconnect and allow it")
        return await executor.execute(user, action, args)

    async def execute_single(self, user: UserRef, action: str, args: dict) -> ToolResult:
        """Like `execute` but never merged: Google's own mail and calendar (Composio fallback included). The
        Gmail and Google Calendar polls and syncs use it so they never ingest Outlook items as Gmail's."""
        return await self._execute_single(user, action, args)

    async def _execute_single(self, user: UserRef, action: str, args: dict) -> ToolResult:
        spec = ACTIONS.get(action)
        provider = provider_of(spec.capability) if spec is not None else None
        if spec is not None and self._ready(provider):
            executor = self.executors[provider]
            if executor.handles(action):
                grant = await self._active_grant(user.user_id, provider)
                if grant is not None:
                    if self._permitted(grant, action, spec.capability):
                        return await executor.execute(user, action, args)
                    # The grant exists but lacks what this action needs: Google is not called. A legacy
                    # Composio account for the service may still do it; otherwise say what is missing.
                    if await self._composio_active(user, spec.capability):
                        return await self.fallback.execute(user, action, args)
                    return ToolResult(
                        ok=False, error_kind=FailureKind.PERMISSION_MISSING,
                        error=f"the connected {provider.value} account was not granted the permission "
                              f"{action} needs; the user must reconnect and allow it")
        return await self.fallback.execute(user, action, args)

    async def provider_access(self, user_id: int, provider: NativeProvider
                              ) -> tuple[list[str], list[str]] | None:
        """What the user's native grant allows and does not, or None when they have no active one (or the
        provider words no features)."""
        if not self._ready(provider):
            return None
        spec = _spec(provider)
        grant = await self._active_grant(user_id, provider)
        if grant is None or spec is None or not spec.features:
            return None
        return feature_access(grant)

    async def slack_chat_ready(self, user_id: int) -> bool:
        """Can this user talk to Mavis in a Slack DM? True when the workspace bot is installed for them."""
        return await self.tokens.account(user_id, NativeProvider.SLACK_BOT) is not None

    async def uses_native(self, user_id: int, capability: Capability) -> bool:
        """Does an ACTIVE native grant serve this capability for the user? Triggers never exist for it
        (they stay with Composio), so the poller is the inbound path."""
        provider = provider_of(capability)
        if not self._ready(provider):
            return False
        grant = await self._active_grant(user_id, provider)
        return grant is not None and covers(grant, capability)

    @staticmethod
    def _permitted(grant: Grant, action: str, capability: Capability) -> bool:
        spec = _spec(grant.provider)
        return allows(grant, action) if spec is not None and spec.action_scopes else covers(grant, capability)

    async def _composio_active(self, user: UserRef, capability: Capability) -> bool:
        try:
            states = await self.fallback.status(user)
        except IntegrationError:
            return False
        return states.get(capability.value) is ConnectionState.ACTIVE

    # --- connection state ----------------------------------------------------------------------------

    async def catalog(self) -> list[Toolkit]:
        return await self.fallback.catalog()

    async def status(self, user: UserRef) -> dict[str, ConnectionState]:
        try:
            states = dict(await self.fallback.status(user))
        except IntegrationError:
            states = {}  # Composio down or unkeyed must not hide native grants
        for grant in await self.tokens.grants(user.user_id):
            if not self._ready(grant.provider):
                continue
            spec = _spec(grant.provider)
            for cap in (spec.capabilities if spec is not None else ()):
                if grant.status == ACTIVE and covers(grant, cap):
                    states[cap.value] = ConnectionState.ACTIVE
                elif grant.status == REVOKED and states.get(cap.value) is not ConnectionState.ACTIVE:
                    states[cap.value] = ConnectionState.FAILED
        return states

    def _native_target(self, toolkit: str) -> NativeProvider | None:
        name = (toolkit or "").strip().lower()
        for spec in providers.user_providers():
            if name in spec.toolkits:
                try:
                    provider = NativeProvider(spec.id)
                except ValueError:
                    return None
                return provider if oauth_mod.configured(provider) else None
        return None

    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str:
        provider = self._native_target(toolkit)
        if provider is None:
            return await self.fallback.connect_link(user, toolkit, callback_url)
        pending = parse_qs(urlsplit(callback_url).query).get("p", [""])[0]
        pending_id = int(pending) if pending.isascii() and pending.isdigit() else None
        try:
            url = await self.oauth.authorize_url(user.user_id, provider, pending_id)
            return await oauth_mod.handoff_link(url, user.user_id, provider)  # a confirmation page first
        except oauth_mod.OAuthError:
            raise IntegrationError(f"{provider.value} sign-in is not available right now") from None

    async def disconnect(self, user: UserRef, toolkit: str) -> None:
        provider = self._native_target(toolkit)
        grant = await self.tokens.grant(user.user_id, provider) if provider is not None else None
        if provider is None or grant is None:
            return await self.fallback.disconnect(user, toolkit)
        await self.oauth.revoke(user.user_id, provider)
        try:  # a legacy Composio account for the same service goes too
            await self.fallback.disconnect(user, toolkit)
        except (NoSuchConnection, IntegrationError):
            pass

    # --- triggers and webhooks stay with Composio ----------------------------------------------------

    async def subscribe(self, user: UserRef, trigger: str, config: dict) -> str:
        return await self.fallback.subscribe(user, trigger, config)

    async def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]:
        return await self.fallback.parse_webhook(headers, body)

    def __getattr__(self, name: str):
        # Composio-only extras (retire_legacy_triggers, configured, ...) keep working through the router.
        if name.startswith("__") or name == "fallback":
            raise AttributeError(name)
        return getattr(self.fallback, name)


def _load_executors(tokens: NativeTokenStore, client: httpx.AsyncClient) -> list[NativeExecutor]:
    """Executors are optional at construction: a module that is not on this build is skipped (its actions
    route to Composio). An import error from inside an existing module is a real bug and propagates."""
    found: list[NativeExecutor] = []
    for spec in providers.user_providers():
        if spec.executor is None:
            continue
        module, cls = spec.executor
        name = f"mavis.tools.integrations.native.{module}"
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        found.append(getattr(mod, cls)(tokens, client))
    return found


def build_native_router(fallback: IntegrationProvider) -> NativeRouter:
    client = make_client()
    tokens = NativeTokenStore(client)
    return NativeRouter(fallback, tokens, NativeOAuth(tokens, client),
                        _load_executors(tokens, client), client)


def native_enabled() -> bool:
    return get_settings().integration_provider == "native"

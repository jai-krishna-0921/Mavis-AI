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
from mavis.domain.errors import IntegrationError, NoSuchConnection
from mavis.domain.events import Event
from mavis.domain.integrations import ConnectionState, Toolkit, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import ACTIONS, GOOGLE_CAPABILITIES
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.native import oauth as oauth_mod
from mavis.tools.integrations.native.base import NativeExecutor, NativeProvider
from mavis.tools.integrations.native.http import make_client
from mavis.tools.integrations.native.oauth import NativeOAuth
from mavis.tools.integrations.native.tokens import ACTIVE, REVOKED, Grant, NativeTokenStore

# Google OAuth scope prefixes that cover each capability. A capability without an entry (Tasks, Meet) is
# never native; it stays on Composio.
_GOOGLE_SCOPE_FOR: dict[Capability, tuple[str, ...]] = {
    Capability.GMAIL: ("https://www.googleapis.com/auth/gmail.",),
    Capability.CALENDAR: ("https://www.googleapis.com/auth/calendar",),
    Capability.DRIVE: ("https://www.googleapis.com/auth/drive",),
    Capability.DOCS: ("https://www.googleapis.com/auth/drive",),
    Capability.SHEETS: ("https://www.googleapis.com/auth/drive",),
    Capability.CONTACTS: ("https://www.googleapis.com/auth/contacts",),
}
_GOOGLE_TOOLKITS = frozenset({"google", "googlesuper", *(c.value for c in GOOGLE_CAPABILITIES)})
_SLACK_TOOLKITS = frozenset({"slack"})


def provider_of(capability: Capability) -> NativeProvider | None:
    if capability in GOOGLE_CAPABILITIES:
        return NativeProvider.GOOGLE
    if capability is Capability.SLACK:
        return NativeProvider.SLACK
    return None


def covers(grant: Grant, capability: Capability) -> bool:
    """Does this grant authorise the capability? Slack: any ACTIVE grant. Google: a granted scope must
    match (Google lets users untick scopes on the consent screen)."""
    if grant.provider is NativeProvider.SLACK:
        return capability is Capability.SLACK
    prefixes = _GOOGLE_SCOPE_FOR.get(capability)
    return bool(prefixes) and any(s.startswith(p) for s in grant.scopes for p in prefixes)


class NativeRouter:
    def __init__(self, fallback: IntegrationProvider, tokens: NativeTokenStore, oauth: NativeOAuth,
                 executors: Iterable[NativeExecutor] = (), client: httpx.AsyncClient | None = None) -> None:
        self.fallback, self.tokens, self.oauth = fallback, tokens, oauth
        self.executors: dict[NativeProvider, NativeExecutor] = {e.provider: e for e in executors}
        self._client = client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        aclose = getattr(self.fallback, "aclose", None)
        if aclose is not None:
            await aclose()

    # --- routing -------------------------------------------------------------------------------------

    def _ready(self, provider: NativeProvider | None) -> bool:
        return provider is not None and provider in self.executors and oauth_mod.configured(provider)

    async def _active_grant(self, user_id: int, provider: NativeProvider) -> Grant | None:
        g = await self.tokens.grant(user_id, provider)
        return g if g is not None and g.status == ACTIVE else None

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult:
        spec = ACTIONS.get(action)
        provider = provider_of(spec.capability) if spec is not None else None
        if spec is not None and self._ready(provider):
            executor = self.executors[provider]
            if executor.handles(action):
                grant = await self._active_grant(user.user_id, provider)
                if grant is not None and covers(grant, spec.capability):
                    return await executor.execute(user, action, args)
        return await self.fallback.execute(user, action, args)

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
            google = grant.provider is NativeProvider.GOOGLE
            for cap in (_GOOGLE_SCOPE_FOR if google else (Capability.SLACK,)):
                if grant.status == ACTIVE and covers(grant, cap):
                    states[cap.value] = ConnectionState.ACTIVE
                elif grant.status == REVOKED and states.get(cap.value) is not ConnectionState.ACTIVE:
                    states[cap.value] = ConnectionState.FAILED
        return states

    def _native_target(self, toolkit: str) -> NativeProvider | None:
        name = (toolkit or "").strip().lower()
        if name in _GOOGLE_TOOLKITS and name not in (Capability.TASKS.value, Capability.MEET.value):
            provider = NativeProvider.GOOGLE
        elif name in _SLACK_TOOLKITS:
            provider = NativeProvider.SLACK
        else:
            return None
        return provider if oauth_mod.configured(provider) else None

    async def connect_link(self, user: UserRef, toolkit: str, callback_url: str) -> str:
        provider = self._native_target(toolkit)
        if provider is None:
            return await self.fallback.connect_link(user, toolkit, callback_url)
        pending = parse_qs(urlsplit(callback_url).query).get("p", [""])[0]
        pending_id = int(pending) if pending.isascii() and pending.isdigit() else None
        try:
            return await self.oauth.authorize_url(user.user_id, provider, pending_id)
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

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[Event]:
        return self.fallback.parse_webhook(headers, body)

    def __getattr__(self, name: str):
        # Composio-only extras (retire_legacy_triggers, configured, ...) keep working through the router.
        if name.startswith("__") or name == "fallback":
            raise AttributeError(name)
        return getattr(self.fallback, name)


_EXECUTORS = (("google", "GoogleExecutor"), ("slack", "SlackExecutor"))


def _load_executors(tokens: NativeTokenStore, client: httpx.AsyncClient) -> list[NativeExecutor]:
    """Executors are optional at construction: a module that is not on this build is skipped (its actions
    route to Composio). An import error from inside an existing module is a real bug and propagates."""
    found: list[NativeExecutor] = []
    for module, cls in _EXECUTORS:
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

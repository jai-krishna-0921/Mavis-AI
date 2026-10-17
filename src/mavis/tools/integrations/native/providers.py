# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The provider table: one ProviderSpec per native vendor. Nothing here imports oauth, tokens, router,
poller or connect_flow (they import this module)."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mavis.config import get_settings
from mavis.domain.policy import Capability

if TYPE_CHECKING:
    from mavis.tools.integrations.native.tokens import Grant


class ExchangeFailed(Exception):
    """A vendor exchange hook could not finish; NativeOAuth turns it into OAuthError("exchange_failed")."""


class PollAuthError(Exception):
    """A poll hook was refused as unauthorised; the Poller decides whether the chain ends."""


@dataclass(frozen=True)
class ScopeParam:
    """One scope-carrying query parameter of the authorize URL."""

    name: str  # "scope" or "user_scope"
    scopes: tuple[str, ...]
    sep: str = " "  # " " (Google, Microsoft) or "," (Slack)


@dataclass(frozen=True)
class ExchangeRequest:
    post: Callable[..., Awaitable[dict]]  # NativeOAuth._post: capped, returns JSON
    code: str
    verifier: str  # PKCE verifier ("" when the spec has pkce=False)
    redirect_uri: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class Exchange:
    account: dict  # non-secret facts stored in NativeGrant.account (must hold "scopes": list[str])
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    extras: dict = field(default_factory=dict)  # secret side data for after_save (Slack's bot token)


@dataclass(frozen=True)
class RevokeRequest:
    post: Callable[..., Awaitable[object]]
    access_token: str
    refresh_token: str | None


@dataclass(frozen=True)
class AfterSave:
    post: Callable[..., Awaitable[Any]]
    tokens: Any  # NativeTokenStore
    user_id: int
    exchange: Exchange


@dataclass(frozen=True, kw_only=True)
class CatalogEntry:
    description: str
    services: Mapping[str, frozenset[str]]  # chip name -> any-of scopes that count as having it
    icon: str  # web icon id, e.g. "google", "slack"
    always_listed: bool = False  # True: shown on the dashboard even when not configured (Google, Slack)
    name: str = ""  # dashboard title; empty falls back to the spec's display_name
    chips: bool = False  # True: the dashboard shows one chip per service


def clean_text(value: object, limit: int = 60) -> str:
    """Third-party or user-chosen text for a page or a message: one line, no control characters, bounded."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def _no_key(account: dict) -> str | None:
    return None


def _no_label(account: dict) -> str | None:
    return None


def _no_external(account: dict) -> str:
    return "an account"


def _always() -> bool:
    return True


@dataclass(frozen=True, kw_only=True)
class ProviderSpec:
    """Everything the platform needs to know about one native vendor. Registered once in PROVIDERS."""

    # identity
    id: str  # NativeProvider value, <= 16 chars
    display_name: str  # "Google", "Slack", "Microsoft 365": callback pages, Telegram, dashboard
    brand: str  # brand key used by actions.BRANDS ("Google")
    internal: bool = False  # True for companion grants (slack_bot): no OAuth, no catalog, no /oauth routes
    # OAuth
    auth_url: str = ""
    token_url: str = ""
    revoke_url: str | None = None
    scope_params: tuple[ScopeParam, ...] = ()
    pkce: bool = False  # S256 challenge on authorize, verifier on exchange
    extra_auth_params: Mapping[str, str] = field(default_factory=dict)  # static query params
    client_id_setting: str = ""  # attribute name on Settings, e.g. "google_oauth_client_id"
    client_secret_setting: str = ""
    token_auth: str = "body"  # "body" (credentials in the form) or "basic_json" (HTTP Basic + JSON)
    refresh_params: Mapping[str, str] = field(default_factory=dict)  # extra form fields on a refresh
    gone_codes: frozenset[str] = frozenset()  # refresh error codes that mean the grant is dead
    soft_errors: bool = False  # True: HTTP 200 with {"ok": false, "error": code} is an error (Slack)
    exchange: Callable[[ExchangeRequest], Awaitable[Exchange]] | None = None  # exchange + identity lookup
    revoke: Callable[[RevokeRequest], Awaitable[None]] | None = None  # vendor revoke, best effort
    after_save: Callable[[AfterSave], Awaitable[None]] | None = None  # companion grants (Slack bot)
    companions: tuple[str, ...] = ()  # provider ids deleted together with this grant on revoke
    denial_kind: Callable[[str | None, str | None], str] | None = None
    #   (callback `error`, `error_description`) -> a key of user_text ("denied" default)
    admin_link: Callable[[], str | None] | None = None  # URL a user can forward to an admin
    # account
    account_key: Callable[[dict], str | None] = _no_key  # unique per (provider, key); <= 255 chars
    account_label: Callable[[dict], str | None] = _no_label  # dashboard/Telegram label (email, team name)
    external_label: Callable[[dict], str] = _no_external  # callback copy: "you@x.com" / "the Acme workspace"
    remember_identity: Callable[[int, dict], Awaitable[None]] | None = None  # the user's own graph identity
    # capabilities
    capabilities: tuple[Capability, ...] = ()  # fan-out of one consent; see anchor
    anchor: Capability | None = None  # capability used for connect prompts, status and pending requests
    family_enabled: Callable[[], bool] = _always  # False: capabilities are not a family (Google workspace)
    legacy_capabilities: tuple[Capability, ...] = ()  # served by an older Composio connection (Google)
    family_label: str = ""  # menu row label for the family; empty: display_name
    capabilities_for: Callable[[Grant], tuple[Capability, ...]] | None = None  # call via spec.caps_for(grant)
    action_scopes: Mapping[str, tuple[frozenset[str], ...]] = field(default_factory=dict)
    #   action -> tuple of any-of scope groups, all groups must be met. Empty: any ACTIVE grant covers all.
    toolkits: frozenset[str] = frozenset()  # /connect and Composio names for this provider
    executor: tuple[str, str] | None = None  # ("google", "GoogleExecutor") under native
    features: tuple[tuple[str, str], ...] = ()  # (human label, action) for "what is allowed" wording
    abilities: str = ""  # "Gmail, Calendar, ..." for the announcement
    ability_labels: Mapping[Capability, str] = field(default_factory=dict)  # announcement words
    unavailable_services: Callable[[dict], frozenset[str]] | None = None  # chips this account can never use
    grant_note: Callable[[dict], str | None] | None = None  # account facts -> a short line (or None)
    reconnect_hint: str = ""  # "send /connect google and leave every box ticked"
    nothing_allowed: str | None = None  # callback copy when features is non-empty and none is allowed
    # polling
    pollers: Mapping[Capability, str] = field(default_factory=dict)
    #   capability -> "module:function"; async fn(poller, user_id, state) -> int; raises PollAuthError
    # memory
    forget_prefixes: tuple[str, ...] = ()  # origin prefixes erased by disconnect-and-forget ("gmail:")
    state_cursors: tuple[str, ...] = ()  # users.state["cursors"] key prefixes cleared on disconnect
    owed_sources: tuple[str, ...] = ()  # slack_owed entries with this source (none counts as slack)
    memory_sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #   vault connector name -> origin prefixes
    # dashboard
    catalog: CatalogEntry | None = None
    # copy
    user_text: Mapping[str, str] = field(default_factory=dict)  # callback outcome overrides

    def caps_for(self, grant: Grant) -> tuple[Capability, ...]:
        """What this particular grant can serve (default: every capability)."""
        return self.capabilities if self.capabilities_for is None else self.capabilities_for(grant)

    def anchor_capability(self) -> Capability:
        """The capability connect prompts and pending requests use: the anchor while the family is on,
        else the first capability (Google with Workspace off is just Gmail)."""
        return self.anchor if self.anchor is not None and self.family_enabled() else self.capabilities[0]


PROVIDERS: dict[str, ProviderSpec] = {}
_HOOK_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
_MAX_ID = 16
_TOKEN_AUTH = frozenset({"body", "basic_json"})


def register(spec: ProviderSpec) -> ProviderSpec:
    if spec.id in PROVIDERS:
        raise ValueError(f"provider {spec.id} is already registered")
    if not spec.id or len(spec.id) > _MAX_ID:
        raise ValueError(f"provider id {spec.id!r} must be 1 to {_MAX_ID} characters")
    if not spec.capabilities and not spec.internal:
        raise ValueError(f"provider {spec.id} must declare at least one capability")
    if spec.token_auth not in _TOKEN_AUTH:
        raise ValueError(f"provider {spec.id} token_auth must be one of {sorted(_TOKEN_AUTH)}")
    if spec.anchor is not None and spec.anchor not in spec.capabilities:
        raise ValueError(f"provider {spec.id} anchor {spec.anchor.value} is not one of its capabilities")
    for other in PROVIDERS.values():
        clash = set(other.capabilities) & set(spec.capabilities)
        if clash:
            raise ValueError(f"{sorted(c.value for c in clash)} already belong to {other.id}")
    if spec.toolkits and not {c.value for c in spec.capabilities} <= spec.toolkits:
        raise ValueError("toolkits must include every capability value (ConnectFlow passes capability.value)")
    stray = set(spec.pollers) - set(spec.capabilities)
    if stray:
        raise ValueError(f"provider {spec.id} has pollers for capabilities it does not own: "
                         f"{sorted(c.value for c in stray)}")
    for capability, path in spec.pollers.items():
        if not isinstance(path, str) or not _HOOK_PATH.fullmatch(path):
            raise ValueError(f"provider {spec.id} poller for {capability.value} must be a 'module:function' "
                             "string")
    PROVIDERS[spec.id] = spec
    return spec


def unregister(provider_id: str) -> None:
    PROVIDERS.pop(str(provider_id), None)


@contextlib.contextmanager
def registered(spec: ProviderSpec) -> Iterator[ProviderSpec]:
    register(spec)
    try:
        yield spec
    finally:
        unregister(spec.id)


def spec_of(provider: str) -> ProviderSpec | None:
    return PROVIDERS.get(str(provider))


def user_providers() -> list[ProviderSpec]:
    return [s for s in PROVIDERS.values() if not s.internal]


def provider_of_capability(capability: Capability) -> ProviderSpec | None:
    return next((s for s in user_providers() if capability in s.capabilities), None)


def family_of(capability: Capability) -> ProviderSpec | None:
    """The provider whose single consent covers `capability` together with others, or None."""
    spec = provider_of_capability(capability)
    return spec if spec is not None and len(spec.capabilities) > 1 and spec.family_enabled() else None


def client_credentials(spec: ProviderSpec) -> tuple[str, str]:
    s = get_settings()
    return (str(getattr(s, spec.client_id_setting, "") or ""),
            str(getattr(s, spec.client_secret_setting, "") or ""))


def configured(spec: ProviderSpec) -> bool:
    if not get_settings().native_token_kek or not spec.client_id_setting:
        return False
    cid, secret = client_credentials(spec)
    return bool(cid and secret)


from mavis.tools.integrations.native import builtin, microsoft_spec  # noqa: E402,F401  (register the vendors)

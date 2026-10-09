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
from mavis.tools.integrations.actions import ACTIONS, GOOGLE_CAPABILITIES
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.native import oauth as oauth_mod
from mavis.tools.integrations.native.base import NativeExecutor, NativeProvider
from mavis.tools.integrations.native.http import make_client
from mavis.tools.integrations.native.oauth import NativeOAuth
from mavis.tools.integrations.native.tokens import ACTIVE, REVOKED, Grant, NativeTokenStore

_AUTH = "https://www.googleapis.com/auth/"
_MAIL_ALL = "https://mail.google.com/"


def _any(*names: str) -> frozenset[str]:
    return frozenset(n if n.startswith("https://") else _AUTH + n for n in names)


_MAIL_READ = _any("gmail.readonly", "gmail.modify", _MAIL_ALL)
_MAIL_SEND = _any("gmail.send", "gmail.compose", "gmail.modify", _MAIL_ALL)
_MAIL_DRAFT = _any("gmail.compose", "gmail.modify", _MAIL_ALL)
_CAL_READ = _any("calendar.readonly", "calendar.events.readonly", "calendar.events", "calendar")
_CAL_FREEBUSY = _any("calendar.freebusy", "calendar.readonly", "calendar.events.readonly",
                     "calendar.events", "calendar")
_CAL_WRITE = _any("calendar.events", "calendar")
_DRIVE_READ = _any("drive.readonly", "drive")
_DRIVE_META = _any("drive.readonly", "drive.metadata.readonly", "drive.metadata", "drive")
_DOCS_READ = _any("documents.readonly", "documents", "drive.readonly", "drive")
_SHEETS_READ = _any("spreadsheets.readonly", "spreadsheets", "drive.readonly", "drive")
_CONTACTS_READ = _any("contacts.readonly", "contacts")
# Writes. drive.file is deliberately absent: it only reaches files this app created, and these actions work
# on the user's existing files (comment, share, move, append, update cells), so the full drive scope counts.
_DRIVE_WRITE = _any("drive")
_DOCS_WRITE = _any("documents", "drive")
_SHEETS_WRITE = _any("spreadsheets", "drive")
_TASKS_READ = _any("tasks", "tasks.readonly")
_TASKS_WRITE = _any("tasks")
_MEET_CREATE = _any("meetings.space.created")
_MEET_READ = _any("meetings.space.readonly", "meetings.space.created")

# What each natively handled Google action needs: a tuple of any-of groups, and every group must be met
# (a reply reads the thread and then sends). An action with no entry is never run natively. Google lets a
# user untick scopes on the consent screen, so the grant's scopes, not what we asked for, decide.
ACTION_SCOPES: dict[str, tuple[frozenset[str], ...]] = {
    "mail.search": (_MAIL_READ,), "mail.read": (_MAIL_READ,), "mail.thread": (_MAIL_READ,),
    "mail.profile": (_MAIL_READ,),
    "mail.send": (_MAIL_SEND,), "mail.draft": (_MAIL_DRAFT,), "mail.reply": (_MAIL_READ, _MAIL_SEND),
    "calendar.list": (_CAL_READ,), "calendar.find": (_CAL_READ,), "calendar.free_slots": (_CAL_FREEBUSY,),
    "calendar.create_event": (_CAL_WRITE,), "calendar.update_event": (_CAL_WRITE,),
    "drive.search": (_DRIVE_META,), "drive.list_recent": (_DRIVE_META,), "drive.meta": (_DRIVE_META,),
    "drive.permissions": (_DRIVE_META,), "drive.read": (_DRIVE_READ,), "drive.download": (_DRIVE_READ,),
    "docs.read": (_DOCS_READ,), "sheets.find": (_DRIVE_META,), "sheets.read": (_SHEETS_READ,),
    "contacts.search": (_CONTACTS_READ,), "contacts.list": (_CONTACTS_READ,),
    "drive.create_folder": (_DRIVE_WRITE,), "drive.move": (_DRIVE_WRITE,), "drive.share": (_DRIVE_WRITE,),
    "drive.upload_file": (_DRIVE_WRITE,), "docs.comment": (_DRIVE_WRITE,),
    "docs.create": (_DOCS_WRITE,), "docs.insert_text": (_DOCS_WRITE,),
    "sheets.create": (_SHEETS_WRITE,), "sheets.append_row": (_SHEETS_WRITE,),
    "sheets.update_range": (_SHEETS_WRITE,),
    "tasks.list": (_TASKS_READ,), "tasks.get": (_TASKS_READ,), "tasks.add": (_TASKS_WRITE,),
    "tasks.patch": (_TASKS_WRITE,), "tasks.delete": (_TASKS_WRITE,),
    "meet.create": (_MEET_CREATE,), "meet.transcript": (_MEET_READ,),
}
_GOOGLE_CAPABILITIES = frozenset(ACTIONS[a].capability for a in ACTION_SCOPES)
_GOOGLE_TOOLKITS = frozenset({"google", "googlesuper", *(c.value for c in GOOGLE_CAPABILITIES)})
_SLACK_TOOLKITS = frozenset({"slack"})


def provider_of(capability: Capability) -> NativeProvider | None:
    if capability in GOOGLE_CAPABILITIES:
        return NativeProvider.GOOGLE
    if capability is Capability.SLACK:
        return NativeProvider.SLACK
    return None


def allows(grant: Grant, action: str) -> bool:
    """Do this Google grant's scopes cover everything the action needs?"""
    groups = ACTION_SCOPES.get(action)
    return bool(groups) and all(grant.scopes & group for group in groups)


def covers(grant: Grant, capability: Capability) -> bool:
    """Can this grant do at least one thing in the capability? Slack: any ACTIVE grant."""
    if grant.provider is NativeProvider.SLACK:
        return capability is Capability.SLACK
    return any(ACTIONS[a].capability is capability and allows(grant, a) for a in ACTION_SCOPES)


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
        if grant.provider is NativeProvider.SLACK:
            return covers(grant, capability)
        return allows(grant, action)

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
            google = grant.provider is NativeProvider.GOOGLE
            for cap in (_GOOGLE_CAPABILITIES if google else (Capability.SLACK,)):
                if grant.status == ACTIVE and covers(grant, cap):
                    states[cap.value] = ConnectionState.ACTIVE
                elif grant.status == REVOKED and states.get(cap.value) is not ConnectionState.ACTIVE:
                    states[cap.value] = ConnectionState.FAILED
        return states

    def _native_target(self, toolkit: str) -> NativeProvider | None:
        name = (toolkit or "").strip().lower()
        if name in _GOOGLE_TOOLKITS:
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

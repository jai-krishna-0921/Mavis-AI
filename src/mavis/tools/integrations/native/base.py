# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Contracts shared by the native token store, the Google and Slack executors and the router.

Executors never see refresh tokens or keys: they ask a TokenSource for a short-lived access token and
call the vendor API with it. Every vendor response is third-party data and is returned as ToolResult.data
(rendered through base.render_result, which the model sees as untrusted).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from mavis.domain.integrations import ToolResult, UserRef


class NativeProvider(StrEnum):
    GOOGLE = "google"
    SLACK = "slack"
    MICROSOFT = "microsoft"
    NOTION = "notion"
    FATHOM = "fathom"
    FATHOM_HOOK = "fathom_hook"  # the per-grant webhook secret companion; never a user-data capability
    SLACK_BOT = "slack_bot"  # the workspace bot token (chat as Mavis); never a user-data capability
    TESTING = "testing"  # reserved for the registry test fake; never present in PROVIDERS in production


class ReauthRequired(Exception):
    """The stored grant is gone (revoked, invalid_grant, token_revoked): the user must reconnect.

    Executors turn this into ToolResult(ok=False, error_kind=FailureKind.AUTH ...) so the poller's
    is_auth_error path and the reconnect prompt fire.
    """


class TokenSource(Protocol):
    async def access_token(self, user_id: int, provider: NativeProvider, *, force: bool = False) -> str:
        """A valid access token, refreshed single-flight when close to expiry. force=True after a 401.
        Raises ReauthRequired when no usable grant exists."""
        ...

    async def account(self, user_id: int, provider: NativeProvider) -> dict | None:
        """Non-secret account facts saved at connect time (email, team id, user id, scopes), or None."""
        ...


class NativeExecutor(Protocol):
    provider: NativeProvider

    def handles(self, action: str) -> bool:
        """True for the Mavis action names (actions.ACTIONS keys) this executor implements."""
        ...

    async def execute(self, user: UserRef, action: str, args: dict) -> ToolResult: ...

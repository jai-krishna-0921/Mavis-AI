# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Seal and open the secrets of an MCP server row with the envelope crypto the native connectors use
(`tools/integrations/native/crypto.py`, keyed by NATIVE_TOKEN_KEK).

Every sealed value is bound to the user, the server row and the column (AES-GCM associated data), so a
ciphertext copied into another user's row, another server's row or another column fails to open. Plaintext in
a sealed column is an error, never returned."""

from __future__ import annotations

from dataclasses import dataclass

from mavis.tools.integrations.native import crypto
from mavis.tools.integrations.native.crypto import SealContext

PURPOSE = "mcp-token"
PROVIDER = "mcp"
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
CLIENT_SECRET = "client_secret"
CODE_VERIFIER = "code_verifier"


@dataclass(frozen=True)
class Tokens:
    access: str | None
    refresh: str | None


def context(user_id: int, server_id: int, field: str) -> SealContext:
    return SealContext(PURPOSE, user_id, PROVIDER, str(server_id), field)


def seal(value: str | None, user_id: int, server_id: int, field: str) -> str | None:
    return crypto.seal_text(value, context(user_id, server_id, field))


def unseal(value: str | None, user_id: int, server_id: int, field: str) -> str | None:
    return crypto.open_text(value, context(user_id, server_id, field))


def tokens_of(row) -> Tokens:
    """The plain access and refresh token of an `McpServer` row."""
    return Tokens(unseal(row.access_token, row.user_id, row.id, ACCESS_TOKEN),
                  unseal(row.refresh_token, row.user_id, row.id, REFRESH_TOKEN))


def client_secret_of(row) -> str | None:
    """The OAuth client secret kept in the row's `oauth` JSON, if there is one."""
    return unseal((row.oauth or {}).get(CLIENT_SECRET), row.user_id, row.id, CLIENT_SECRET)


def secrets_of(row) -> tuple[str, ...]:
    """Every secret this row holds, in plain form, for redacting error text. Never raises: a value that
    cannot be opened is simply not redacted by value (credential shapes are still blanked)."""
    found: list[str] = []
    for getter in (lambda: tokens_of(row).access, lambda: tokens_of(row).refresh,
                   lambda: client_secret_of(row)):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - redaction helper
            continue
        if value:
            found.append(value)
    return tuple(found)

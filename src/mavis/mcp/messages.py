# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The words for MCP failures: what the user is told, and what the model is told.

Codes are the fixed short words of `mavis.mcp.failures` (and the OAuth ones). The sentences are ours, never
a server's, and carry no response body, URL or token. No em or en dashes in any of them."""

from __future__ import annotations

from mavis.mcp.failures import Kind

# What a person reads (dashboard, Telegram).
USER_TEXT = {
    "auth": "The server did not accept the sign-in. Reconnect it.",
    "needs_token": ("This server needs a sign-in token and does not offer another way. "
                    "Paste a token and add it again."),
    "oauth_token": "The sign-in was refused. Reconnect it.",
    "issuer_mismatch": ("The sign-in reply did not come from the expected provider, so it was refused. "
                        "Reconnect it."),
    "issuer_changed": "The server's sign-in provider changed. Reconnect it to confirm.",
    "not_found": "That address did not answer as an MCP server (not found).",
    "not_mcp": "That address is not an MCP server.",
    "rejected": "The server rejected the request.",
    "blocked_url": "That address is not allowed. Use a public https address.",
    "redirect": "The server redirected somewhere else, which is not allowed. Use its final address.",
    "too_large": "The server sent a response that was too large.",
    "protocol": "The server answered in a way Mavis does not understand.",
    "timeout": "The server took too long to answer.",
    "unreachable": "Could not reach the server.",
    "server_error": "The server reported an error.",
    "rate_limited": "The server is limiting requests. Try again in a minute.",
    "session": "The connection to the server was interrupted.",
    "connection_lost": "The connection to the server dropped.",
    "rpc_error": "The server reported a problem with the request.",
    "oauth_unsupported": "The server asks for sign-in but does not offer a way Mavis can use.",
    "oauth_metadata": "The server's sign-in details look wrong, so Mavis did not use them.",
    "oauth_not_same_site": "The server's sign-in pages are on an unrelated site, so Mavis did not use them.",
    "oauth_discovery": "Could not read the server's sign-in details.",
    "oauth_registration": "The server would not register Mavis as a client.",
    "no_registration": "The server does not allow Mavis to register itself as a client.",
    "pkce_unsupported": "The server's sign-in does not support the secure flow Mavis needs.",
    "unknown": "Something went wrong talking to the server.",
}


def user_text(code: str | None) -> str:
    return USER_TEXT.get(code or "unknown", USER_TEXT["unknown"])


def model_text(name: str, code: str, kind: Kind, *, unknown_outcome: bool = False, tool: str = "") -> str:
    """The sentence a tool result gives the model when a call did not complete. `name` is the server's
    display name (already cleaned)."""
    if unknown_outcome:
        return (f"The connection to {name} dropped while running {tool or 'the tool'}, so the outcome is "
                "unknown: it may or may not have happened. Check before trying again; do not retry it "
                "automatically.")
    if kind is Kind.AUTH:
        return (f"The {name} connection needs to be reconnected in the dashboard; do not retry. "
                "Tell the user.")
    if kind is Kind.PERMANENT:
        return f"{name} cannot be used right now: {user_text(code)} Tell the user; do not retry."
    return f"Could not complete that on {name} just now: {user_text(code)} It may work later."

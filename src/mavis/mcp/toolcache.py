# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The per-server tool cache: what we keep of a server's `tools/list` (spec D5, D6).

An entry is `{name, description, input_schema, kind}`. The description is cleaned (invisible characters and
terminal escapes stripped, whitespace collapsed, 600 characters at most) and the input schema is normalized
(`mavis.mcp.schema`). `kind` is "read" only when ALL of these hold: the server's `readOnlyHint` is true, its
`destructiveHint` is not true, and the tool NAME passes the read-stem check (it starts with a read stem such
as get or list and contains no mutating verb such as create or send). Otherwise it is "act". A vendor hint
alone never makes a tool run without approval: the name check is ours, the hint is the vendor's, and both
must agree.

The read and mutating word lists come from our own earlier remote-MCP adapter (branch `connectors`,
`remote_mcp.py`).

Limits: 128 tools kept (the first 128 by name, after filtering); a tool whose normalized input schema is over
8 KB of JSON is skipped, as is a tool with an unusable name."""

from __future__ import annotations

import re
from datetime import datetime

import structlog

from mavis.domain import timeutil
from mavis.mcp.client import RawTool
from mavis.mcp.limits import DESCRIPTION_CHARS, MAX_TOOLS_PER_SERVER, SCHEMA_MAX_BYTES, TOOLS_TTL
from mavis.mcp.schema import json_size, normalize_input_schema
from mavis.mcp.text import clean_description

log = structlog.get_logger(__name__)

READ = "read"
ACT = "act"

# A tool is a read only if its name STARTS with a read stem (an allowlist: anything else is not a read) and
# no word in it is a mutating verb (defence in depth against "get_and_send_email").
READ_STEMS = frozenset({
    "get", "list", "search", "find", "fetch", "read", "show", "view", "lookup", "query", "count", "describe",
    "history", "check", "browse", "preview", "track", "summarize"})
MUTATING = frozenset({
    "send", "post", "publish", "share", "invite", "email", "reply", "forward", "broadcast", "notify", "tweet",
    "submit", "pay", "buy", "purchase", "checkout", "transfer", "book", "place", "delete", "remove", "cancel",
    "destroy", "revoke", "create", "update", "add", "write", "set", "put", "move", "upload", "assign",
    "schedule", "approve", "text", "sms", "dm", "call", "archive", "unsubscribe", "edit", "modify", "rename",
    "save", "apply", "enable", "disable", "start", "stop", "run", "execute", "trigger", "join", "leave",
    "follow", "like", "react", "mark", "reorder", "reject", "merge", "close", "clear", "reset",
    "drop", "push", "replace", "insert", "deploy", "commit", "truncate", "wipe", "erase", "import", "install",
    "uninstall", "exec", "grant", "refund", "charge", "order", "sell", "sync", "restore", "patch", "kill",
    "flag", "star", "subscribe", "block", "ban", "mute", "accept", "decline", "comment", "tag", "label",
    "pin", "lock", "unlock", "upsert", "store", "purge", "generate", "convert", "translate"})
# A name that chains two actions ("get_and_send") is never a read, whatever the verbs are.
CONJUNCTIONS = frozenset({"and", "then", "or", "plus", "also"})
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


def _stems(name: str) -> list[str]:
    words = [w.lower() for w in _WORD.findall(name or "")]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def _chains_actions(name: str) -> bool:
    words = [w.lower() for w in _WORD.findall(name or "")]
    return any(w in CONJUNCTIONS for w in words) or any(
        a == "with" and b in {"side", "sides"} for a, b in zip(words, words[1:], strict=False))


def mutating_words(name: str) -> set[str]:
    return {w for w in _stems(name) if w in MUTATING}


def is_read_name(name: str) -> bool:
    """The name starts with a read stem and contains no mutating verb."""
    stems = _stems(name)
    if not stems or stems[0] not in READ_STEMS:
        return False
    return not mutating_words(name) and not _chains_actions(name)


def kind_of(name: str, read_only_hint: bool | None, destructive_hint: bool | None) -> str:
    """"read" only when the vendor says read-only, does not say destructive, AND our name check agrees."""
    if read_only_hint is True and destructive_hint is not True and is_read_name(name):
        return READ
    return ACT


def build_entries(raw_tools: list[RawTool]) -> list[dict]:
    """The cache entries for a listed set of tools: cleaned, normalized, classified, capped, sorted."""
    entries: dict[str, dict] = {}
    skipped = {"name": 0, "schema": 0, "duplicate": 0}
    for raw in raw_tools:
        if not isinstance(raw.name, str) or not _SAFE_NAME.fullmatch(raw.name):
            skipped["name"] += 1
            continue
        if raw.name in entries:
            skipped["duplicate"] += 1
            continue
        try:
            schema = normalize_input_schema(raw.input_schema)
            if json_size(schema) > SCHEMA_MAX_BYTES:
                skipped["schema"] += 1
                continue
        except RecursionError:
            skipped["schema"] += 1
            continue
        entries[raw.name] = {
            "name": raw.name,
            "description": clean_description(raw.description, DESCRIPTION_CHARS),
            "input_schema": schema,
            "kind": kind_of(raw.name, raw.read_only_hint, raw.destructive_hint),
        }
    kept = [entries[name] for name in sorted(entries)][:MAX_TOOLS_PER_SERVER]
    if any(skipped.values()) or len(entries) > len(kept):
        log.info("mcp.tools_filtered", kept=len(kept), over_cap=len(entries) - len(kept), **skipped)
    return kept


def is_stale(fetched_at: datetime | None, now: datetime | None = None) -> bool:
    """The cached list is older than 15 minutes (or was never fetched)."""
    if fetched_at is None:
        return True
    return timeutil.ensure_utc(fetched_at) + TOOLS_TTL <= (now or timeutil.now())


def find(tools: list[dict], name: str) -> dict | None:
    return next((t for t in tools if t.get("name") == name), None)

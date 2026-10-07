"""Classify a provider failure into the one FailureKind vocabulary (hotfix4 H3).

Classification reads STRUCTURE, never English prose: an HTTP status (ours, or one the provider put in a
JSON error body: `code`, `status`, `status_code`), else a provider's machine error code (Google's
`reason`/`status` such as `notFound` or `RESOURCE_EXHAUSTED`, Slack's `channel_not_found`, Notion's
`object_not_found`), else UNKNOWN. A field is named only when the body says which argument it was about
in a structured key (`location`, `param`, `field`, `argument`). The free-text `message` is kept for the
model (wrapped as untrusted elsewhere) and never decides anything here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from mavis.domain.errors import FailureKind

_STATUS_KEYS = ("code", "status", "status_code", "statusCode", "http_status")
_CODE_KEYS = ("reason", "status", "error", "code", "type", "error_code")
_FIELD_KEYS = ("location", "param", "field", "argument", "parameter")
# Machine error codes, as tokens: compared after lowercasing and removing "_", "-" and spaces.
_CODE_TOKENS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (FailureKind.RATE_LIMITED, ("ratelimit", "resourceexhausted", "quotaexceeded", "toomanyrequests",
                                "userratelimitexceeded")),
    (FailureKind.AUTH, ("unauthenticated", "unauthorized", "permissiondenied", "forbidden", "invalidauth",
                        "notauthed", "autherror", "tokenexpired", "invalidgrant", "insufficientpermissions",
                        "accountinactive", "tokenrevoked")),
    (FailureKind.NOT_FOUND, ("notfound", "objectnotfound", "channelnotfound", "deleted", "gone")),
    (FailureKind.UNAVAILABLE, ("unavailable", "deadlineexceeded", "backenderror", "internalerror",
                               "timeout", "serviceunavailable")),
    (FailureKind.INVALID_ARGUMENT, ("invalidargument", "invalid", "badrequest", "validationerror",
                                    "invalidparameter", "required", "failedprecondition", "outofrange")),
)


def kind_for_status(status: int | None) -> FailureKind | None:
    if status is None:
        return None
    if status in (400, 409, 411, 412, 413, 414, 415, 416, 422):
        return FailureKind.INVALID_ARGUMENT
    if status in (401, 403, 407):
        return FailureKind.AUTH
    if status in (404, 410):
        return FailureKind.NOT_FOUND
    if status == 429:
        return FailureKind.RATE_LIMITED
    if status == 408 or 500 <= status <= 599:
        return FailureKind.UNAVAILABLE
    return None


def _parse(body: Any) -> Any:
    if isinstance(body, (dict, list)):
        return body
    if not isinstance(body, str):
        return None
    start = min((i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        return json.loads(body[start:])
    except ValueError:
        return None


def _walk(node: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 6:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(_parse(value) if isinstance(value, str) else value, depth + 1)
    elif isinstance(node, list):
        for item in node[:20]:
            yield from _walk(item, depth + 1)


def _token(value: str) -> str:
    return re.sub(r"[\s_\-]", "", value.lower())


def _kind_for_code(value: str) -> FailureKind | None:
    if len(value) > 60 or " " in value.strip():
        return None  # prose, not a machine code
    token = _token(value)
    for kind, codes in _CODE_TOKENS:
        if any(code in token for code in codes):
            return kind
    return None


def classify(body: Any = None, *, status: int | None = None) -> tuple[FailureKind, str | None]:
    """(kind, field) for a provider failure. `body` is the provider's error (dict, JSON text, or a
    string that may embed JSON); `status` an HTTP status the adapter saw itself."""
    tree = _parse(body)
    nodes = list(_walk(tree)) if tree is not None else []
    field = next((str(n[k]) for n in nodes for k in _FIELD_KEYS if isinstance(n.get(k), str) and n[k]), None)
    kind = kind_for_status(status)
    if kind is None:
        statuses = (n[k] for n in nodes for k in _STATUS_KEYS if isinstance(n.get(k), int))
        kind = next((k for k in map(kind_for_status, statuses) if k is not None), None)
    if kind is None:
        codes = [n[k] for n in nodes for k in _CODE_KEYS if isinstance(n.get(k), str)]
        if isinstance(body, str) and tree is None:
            codes.append(body)  # a bare machine code such as "channel_not_found"
        kind = next((k for k in map(_kind_for_code, codes) if k is not None), None)
    return kind or FailureKind.UNKNOWN, field

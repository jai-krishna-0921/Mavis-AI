from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from mavis.domain.errors import FailureKind


class UserRef(BaseModel):
    user_id: int

    @property
    def provider_id(self) -> str:
        return f"mavis-{self.user_id}"


class ConnectionState(StrEnum):
    ACTIVE = "ACTIVE"
    INITIATED = "INITIATED"
    FAILED = "FAILED"
    NONE = "NONE"


class Toolkit(BaseModel):
    slug: str
    name: str
    description: str


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None  # provider detail: for the model (wrapped as untrusted) and logs, never the user
    error_kind: FailureKind | None = None  # set by the adapter; None means classify `error` as given
    error_field: str | None = None  # our argument name the failure was about, when the provider said so


_PROVIDER_ID = re.compile(r"mavis-(\d+)")


class PendingStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DECLINED = "declined"
    FAILED = "failed"
    EXPIRED = "expired"


def user_from_provider_id(value: object) -> int | None:
    """Inverse of the legacy UserRef.provider_id. Anything not shaped 'mavis-<int>' is not ours. Kept for the
    default resolver of parse_composio_webhook; live callers use identity.user_for_provider_id."""
    m = _PROVIDER_ID.fullmatch(str(value or ""))
    return int(m.group(1)) if m else None

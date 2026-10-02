from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


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
    error: str | None = None

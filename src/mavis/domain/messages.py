from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Button(BaseModel):
    label: str
    data: str = Field(default="", max_length=64, description="Telegram callback_data limit is 64 bytes")
    url: str | None = Field(default=None, description="If set, rendered as a URL button (no callback)")


class Outbound(BaseModel):
    user_id: int
    text: str = ""
    buttons: list[list[Button]] = Field(default_factory=list)  # rows of buttons
    document_path: str | None = None  # local path to an artefact to send as a file
    proactive: bool = False
    dedupe_key: str | None = None


class InboundFile(BaseModel):
    file_id: str
    file_name: str
    mime_type: str | None = None
    size: int | None = None

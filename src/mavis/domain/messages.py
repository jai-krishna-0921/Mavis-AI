from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# An assistant message written from third-party content (an email, a web page, a tainted task's approval
# preview) is logged with this event_id suffix. Learn text that includes it is learned as untrusted, and a
# turn that sees it in its recent history runs tainted.
TAINT_SUFFIX = ":tainted"


def tainted_event_id(event_id: str | None) -> bool:
    """A history row logged with the taint marker: written after reading third-party content."""
    return bool(event_id and event_id.endswith(TAINT_SUFFIX))


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

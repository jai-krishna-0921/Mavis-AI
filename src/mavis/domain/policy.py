from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class RiskClass(StrEnum):
    READ = "read"
    WRITE_SELF = "write_self"
    OUTWARD = "outward"
    SPEND = "spend"
    DESTRUCTIVE = "destructive"

    @property
    def needs_approval(self) -> bool:
        return self in (RiskClass.OUTWARD, RiskClass.SPEND, RiskClass.DESTRUCTIVE)


class Capability(StrEnum):
    GMAIL = "gmail"
    CALENDAR = "googlecalendar"
    SLACK = "slack"
    NOTION = "notion"
    SANDBOX = "sandbox"
    WEB = "web"


class PolicyVerdict(BaseModel):
    allow: bool
    defer_until: datetime | None = None
    reason: str = ""

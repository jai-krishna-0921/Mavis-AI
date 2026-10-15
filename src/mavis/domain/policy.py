# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
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
    # Google Workspace (googlesuper). Values are Mavis names, not Composio toolkit slugs.
    DRIVE = "drive"
    DOCS = "docs"
    SHEETS = "sheets"
    TASKS = "tasks"
    CONTACTS = "contacts"
    MEET = "meet"
    OUTLOOK = "outlook"
    OUTLOOK_CALENDAR = "outlookcalendar"
    TEAMS = "teams"
    ONEDRIVE = "onedrive"
    SLACK = "slack"
    NOTION = "notion"
    SANDBOX = "sandbox"
    WEB = "web"
    MCP = "mcp"  # the user has at least one active MCP server (see mavis.mcp.tools)


class PolicyVerdict(BaseModel):
    allow: bool
    defer_until: datetime | None = None
    reason: str = ""
    budget_bypass: bool = False  # allowed only because it is a security notice over the daily budget

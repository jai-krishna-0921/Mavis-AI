"""Multi-user front door (Phase 11): invite codes, the access gate, commands, budgets, deletion."""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BANNED = "banned"
    DELETING = "deleting"
    DELETED = "deleted"


class UserTier(StrEnum):
    OWNER = "owner"
    STANDARD = "standard"
    TRUSTED = "trusted"

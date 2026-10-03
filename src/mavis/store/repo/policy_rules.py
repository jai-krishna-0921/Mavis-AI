"""Standing approval rules, e.g. 'always OK invites to Jawahar'."""

from __future__ import annotations

import json

from sqlalchemy import select

from mavis.store.db import Session
from mavis.store.models import PolicyRule


async def add(user_id: int, tool: str, field: str, contains: str, description: str) -> int:
    if not contains.strip():
        raise ValueError("a standing rule needs non-empty match text")
    async with Session() as s:
        r = PolicyRule(user_id=user_id, tool=tool, field=field, contains=contains, description=description)
        s.add(r)
        await s.commit()
        return r.id


async def list_for(user_id: int) -> list[PolicyRule]:
    async with Session() as s:
        return list(await s.scalars(select(PolicyRule).where(PolicyRule.user_id == user_id)))


async def matches(user_id: int, tool: str, args: dict) -> bool:
    """A rule matches when args[field], serialised, contains the rule text (case-insensitive)."""
    async with Session() as s:
        rules = list(await s.scalars(
            select(PolicyRule).where(PolicyRule.user_id == user_id, PolicyRule.tool == tool)
        ))
    for rule in rules:
        if rule.field not in args:
            continue
        haystack = json.dumps(args[rule.field], default=str, ensure_ascii=False).lower()
        if rule.contains.lower() in haystack:
            return True
    return False

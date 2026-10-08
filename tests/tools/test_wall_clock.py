"""A7: the model never does timezone arithmetic in tool arguments. Every datetime a tool takes is the
local wall-clock time as the user said it; code attaches the zone (the user's, or one the user named),
in one place every tool path uses."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from mavis.domain.localtime import LOCAL_ONLY_FIELDS, WALL_CLOCK_MARK, LocalTimes, localize_args
from mavis.domain.policy import RiskClass
from mavis.tools.registry import MavisTool

ZONES = ["Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"]


class MeetArgs(LocalTimes):
    title: str
    start: datetime
    end: datetime | None = None


def _expected(wall: str, zone: str) -> datetime:
    return datetime.fromisoformat(wall).replace(tzinfo=ZoneInfo(zone))


INPUTS = [
    # (what the model sent, timezone field, wall clock the user meant, zone it is in: None = user's)
    ("2026-10-04T11:00", None, "2026-10-04T11:00", None),                    # naive: the rule
    ("2026-10-04T11:00:00+05:30", None, "2026-10-04T11:00", None),           # the incident: a made-up offset
    ("2026-10-04T11:00:00-04:00", None, "2026-10-04T11:00", None),
    ("2026-10-04T11:00:00Z", None, "2026-10-04T11:00", None),
    ("2026-10-04T15:00", "America/New_York", "2026-10-04T15:00", "America/New_York"),  # "3pm New York time"
    ("2026-10-04T15:00:00Z", "Asia/Tokyo", "2026-10-04T15:00", "Asia/Tokyo"),
    ("2026-10-04T09:30", "Not/AZone", "2026-10-04T09:30", None),           # unknown zone: the user's
]


@pytest.mark.parametrize("user_tz", ZONES)
@pytest.mark.parametrize("sent,tzfield,wall,zone", INPUTS)
def test_localize_keeps_the_wall_clock_and_attaches_the_right_zone(user_tz, sent, tzfield, wall, zone):
    args = MeetArgs(title="x", start=sent, end=sent, timezone=tzfield)
    out = localize_args(args, user_tz)
    want = _expected(wall, zone or user_tz)
    assert out.start == want and out.start.utcoffset() == want.utcoffset()
    assert out.end == want
    assert localize_args(out, user_tz) == out  # idempotent (an approved, stored call re-localizes)


@pytest.mark.parametrize("user_tz", ZONES)
@pytest.mark.parametrize("sent,tzfield,wall,zone", INPUTS[:5])
async def test_builtin_tool_path_localizes_before_the_tool_runs(user, fresh_registry, user_tz, sent, tzfield,
                                                                wall, zone):
    from mavis.store.repo import users

    await users.update(user.id, timezone=user_tz)
    seen = []

    async def _fn(user_id, args):
        seen.append(args)
        return "ok"

    tool = MavisTool("book_room", "Book a room.", MeetArgs, RiskClass.WRITE_SELF, _fn,
                     agents=frozenset({"conversation"}))
    fresh_registry.register(tool)
    await fresh_registry.invoke(tool, user.id, MeetArgs(title="t", start=sent, timezone=tzfield))
    assert seen[0].start == _expected(wall, zone or user_tz)


@pytest.mark.parametrize("user_tz", ZONES)
async def test_calendar_invite_card_and_provider_get_the_time_the_user_said(user, fresh_registry, provider,
                                                                           cache, monkeypatch, user_tz):
    """The prod incident: 'tomorrow at 11 am' sent as 05:00+05:30. The card and the event say 11:00."""
    from mavis.domain.errors import ApprovalRequired
    from mavis.domain.integrations import ConnectionState
    from mavis.domain.policy import Capability
    from mavis.store.repo import users
    from mavis.tools import integrations
    from mavis.tools.integrations.tools import register_integration_tools
    from tests.agents.test_simple_turn_tools import _getter

    monkeypatch.setattr(integrations, "get_provider", _getter(provider))
    monkeypatch.setattr(integrations, "get_connection_cache", _getter(cache))
    await users.update(user.id, timezone=user_tz)
    provider.set_state(user.id, Capability.CALENDAR, ConnectionState.ACTIVE)
    register_integration_tools(fresh_registry)
    tool = fresh_registry.get("calendar_create_event")
    args = tool.args_model.model_validate({"summary": "Interview", "start": "2026-10-04T11:00:00+05:30",
                                           "attendees": ["jk@example.com"]})
    with pytest.raises(ApprovalRequired) as req:
        await fresh_registry.invoke(tool, user.id, args)
    assert "11:00 to 12:00" in req.value.preview  # only a start: the default length (60 min)
    approved = tool.args_model.model_validate(req.value.arguments)
    await tool.fn(user.id, approved)
    [(_, action, sent)] = [e for e in provider.executed if e[1] == "calendar.create_event"]
    assert datetime.fromisoformat(sent["start"]) == _expected("2026-10-04T11:00", user_tz)
    assert not set(LOCAL_ONLY_FIELDS) & set(sent)  # the timezone hint never reaches the provider


def _all_args_models() -> list[tuple[str, type[BaseModel]]]:
    from mavis.tools import load_builtin_tools
    from mavis.tools.integrations.actions import ACTIONS
    from mavis.tools.registry import ToolRegistry

    reg = ToolRegistry()
    load_builtin_tools(reg)
    models = [(t.name, t.args_model) for t in reg._tools.values()]
    models += [(name, spec.args_model) for name, spec in ACTIONS.items()]  # every spec, flag on or off
    return models


def _datetime_fields(model: type[BaseModel]) -> list[str]:
    import typing

    out = []
    for name, f in model.model_fields.items():
        types = typing.get_args(f.annotation) or (f.annotation,)
        if datetime in types:
            out.append(name)
    return out


def test_every_tool_datetime_field_states_the_wall_clock_rule():
    """New tools cannot regress: any datetime argument must say it is the user's wall-clock time and the
    model must carry the optional timezone field."""
    checked = 0
    for name, model in _all_args_models():
        fields = _datetime_fields(model)
        for field in fields:
            desc = model.model_fields[field].description or ""
            assert WALL_CLOCK_MARK in desc, f"{name}.{field}: {desc!r}"
            checked += 1
        if fields:
            assert issubclass(model, LocalTimes), name
    assert checked >= 8


def test_date_only_fields_are_not_touched():
    class DueArgs(LocalTimes):
        due: date

    out = localize_args(DueArgs(due=date(2026, 10, 4)), "Pacific/Auckland")
    assert out.due == date(2026, 10, 4)


# --- fix round 2: the edit path uses the same normalisation, so preview == execution ------------------


@pytest.mark.parametrize("user_tz", ZONES)
@pytest.mark.parametrize("revised_start,tzfield,wall,zone", [
    ("2026-10-04T08:30", None, "2026-10-04T08:30", None),
    ("2026-10-04T08:30:00Z", None, "2026-10-04T08:30", None),
    ("2026-10-04T08:30:00+09:00", None, "2026-10-04T08:30", None),
    ("2026-10-04T08:30:00Z", "Europe/Paris", "2026-10-04T08:30", "Europe/Paris"),
])
async def test_revised_args_are_normalised_before_preview_and_storage(user, fake_llm, rec_bus, sent,
                                                                     fresh_registry, user_tz, revised_start,
                                                                     tzfield, wall, zone):
    from mavis.agents.orchestrator_graph import revise_approval
    from mavis.store.db import utcnow
    from mavis.store.repo import approvals, users
    from mavis.tools.integrations.tools import register_integration_tools

    await users.update(user.id, timezone=user_tz)
    register_integration_tools(fresh_registry)
    tool = fresh_registry.get("calendar_create_event")
    aid = await approvals.create(user.id, None, "calendar_create_event",
                                 {"summary": "Sync", "start": "2026-10-04T11:00:00+05:30"}, "old",
                                 utcnow() + __import__("datetime").timedelta(hours=48))
    fake_llm.push_structured(tool.args_model.model_validate(
        {"summary": "Sync", "start": revised_start, "timezone": tzfield}))
    await revise_approval(await approvals.get(aid), "move it to 8:30")
    row = await approvals.get(aid)
    want = _expected(wall, zone or user_tz)
    stored = tool.args_model.model_validate(row.arguments)
    assert stored.start == want
    executed = localize_args(stored, user_tz)  # what execute_approved will run
    assert executed.start == want
    local = want.astimezone(ZoneInfo(user_tz))
    assert f"{local:%H:%M} to" in row.preview  # the card shows exactly what will run

"""H2: one semantic argument rule for every model-facing tool, applied before risk, preview and approval."""

from __future__ import annotations

from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from mavis.agents.react import react_loop
from mavis.domain.args import ToolArgs, email_address
from mavis.domain.policy import RiskClass
from mavis.store.repo import approvals
from mavis.tools.assistant import TrackLoopArgs, WakeMeArgs
from mavis.tools.chat_tools import StartTaskArgs
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
    CalendarUpdateArgs,
    DriveShareArgs,
    MailComposeArgs,
    MailReplyArgs,
    SheetAppendArgs,
    SlackSendArgs,
    TaskAddArgs,
    TaskUpdateArgs,
)
from mavis.tools.integrations.tools import register_integration_tools
from mavis.tools.registry import ToolContext, ToolRegistry
from mavis.tools.web import SearchArgs

CTX = ToolContext(user_id=1, timezone="Asia/Kolkata")
START = datetime(2026, 10, 8, 14, 0)


def _risk_and_preview(action: str, args):
    spec = ACTIONS[action]
    return spec.risk_for(args), spec.preview(args, CTX.timezone)


# --- blank list items are dropped, so they cannot change risk or preview ----------------------------


@pytest.mark.parametrize("blank", [[""], ["  "], ["", "\t"], [" ", "\n"]])
def test_calendar_create_blank_guests_mean_no_guests(blank):
    args = CalendarCreateArgs(summary="Focus block", start=START, duration_minutes=60, attendees=blank)
    risk, preview = _risk_and_preview("calendar.create_event", args)
    assert args.attendees == []
    assert risk is RiskClass.WRITE_SELF
    assert "With:" not in preview and "Just you" in preview


def test_calendar_create_keeps_real_guests_and_strips_them():
    args = CalendarCreateArgs(summary="Sync", start=START, attendees=[" a@x.io ", "", "b@y.org"])
    risk, preview = _risk_and_preview("calendar.create_event", args)
    assert args.attendees == ["a@x.io", "b@y.org"]
    assert risk is RiskClass.OUTWARD
    assert "With: a@x.io, b@y.org" in preview


@pytest.mark.parametrize("guest", ["Ravi", "priya at example dot com", "@nohost", "a@b", "mailto:",
                                   "x:y@z.io"])
def test_calendar_guests_must_be_email_addresses(guest):
    with pytest.raises(ValidationError, match="not an email address"):
        CalendarCreateArgs(summary="Lunch", start=START, attendees=[guest])


def test_calendar_update_blank_guest_list_changes_nothing_but_empty_list_removes_all():
    blank = CalendarUpdateArgs(event_id="ev1", attendees=[""])
    assert blank.attendees is None
    assert "Guests" not in ACTIONS["calendar.update_event"].preview(blank, CTX.timezone)
    cleared = CalendarUpdateArgs(event_id="ev1", attendees=[])
    assert "all removed" in ACTIONS["calendar.update_event"].preview(cleared, CTX.timezone)


@pytest.mark.parametrize("model,kwargs,field", [
    (CalendarCreateArgs, {"summary": "   ", "start": START}, "summary"),
    (CalendarUpdateArgs, {"event_id": " "}, "event_id"),
    (MailComposeArgs, {"to": ["", " "], "subject": "Hi", "body": "Hello"}, "to"),
    (MailComposeArgs, {"to": ["a@x.io"], "subject": "Hi", "body": "\n\n"}, "body"),
    (MailReplyArgs, {"thread_id": "t1", "to": "", "body": "Thanks"}, "to"),
    (SlackSendArgs, {"channel": "#general", "text": "   "}, "text"),
    (DriveShareArgs, {"file_id": "", "email": "p@x.io"}, "file_id"),
    (TaskAddArgs, {"title": "  "}, "title"),
    (StartTaskArgs, {"goal": "     "}, "goal"),
    (WakeMeArgs, {"at": START, "reason": "  "}, "reason"),
])
def test_blank_required_strings_are_rejected(model, kwargs, field):
    with pytest.raises(ValidationError) as exc:
        model(**kwargs)
    assert f"{field} is empty" in str(exc.value)


def test_mail_addresses_are_normalised_and_checked():
    args = MailComposeArgs(to=[" ana@x.io", ""], cc=["", " Bo <bo@y.org> ", "mailto:Cy@z.io"],
                           subject="Q3", body="Hi,\n\n  - item\n")
    assert args.to == ["ana@x.io"] and args.cc == ["bo@y.org", "Cy@z.io"]
    assert args.body == "Hi,\n\n  - item\n"  # non-blank text is kept exactly as written
    reply = MailReplyArgs(thread_id="t", to="Meetup <info@meetup.com>", body="ok")
    assert reply.to == "info@meetup.com"
    with pytest.raises(ValidationError, match="not an email address"):
        MailComposeArgs(to=["Kiran"], subject="s", body="b")
    with pytest.raises(ValidationError, match="not an email address"):
        DriveShareArgs(file_id="f1", email="priya")
    assert DriveShareArgs(file_id="f1", email=" p@x.io ").email == "p@x.io"


def test_optional_blank_strings_fall_back_to_defaults():
    assert TaskUpdateArgs(task_id="t1", title="").title is None  # may not be empty: "keep the current one"
    assert TaskAddArgs(title="Pay rent", notes="   ").notes == ""
    assert CalendarCreateArgs(summary="x", start=START, description=" ").description == ""
    assert SearchArgs(query="  standing desks  ").query == "  standing desks  "  # text kept as written


@pytest.mark.parametrize("model,kwargs,field", [
    (CalendarUpdateArgs, {"event_id": "ev1", "description": ""}, "description"),
    (CalendarUpdateArgs, {"event_id": "ev1", "description": "   "}, "description"),
    (TaskUpdateArgs, {"task_id": "t1", "notes": ""}, "notes"),
    (TaskUpdateArgs, {"task_id": "t1", "notes": " \n"}, "notes"),
])
def test_blank_patch_fields_still_mean_clear_it(model, kwargs, field):
    """I3: on an update, "" clears the field; leaving it out keeps it. The two must stay different."""
    assert getattr(model(**kwargs), field) == ""
    assert getattr(model(**{k: v for k, v in kwargs.items() if k != field}), field) is None


def test_cleared_patch_fields_reach_the_provider_as_empty():
    from mavis.tools.integrations.composio_map import COMPOSIO_ACTIONS

    cleared = COMPOSIO_ACTIONS["calendar.update_event"].translate(
        CalendarUpdateArgs(event_id="ev1", description=""))
    assert cleared.get("description") == ""
    kept = COMPOSIO_ACTIONS["calendar.update_event"].translate(CalendarUpdateArgs(event_id="ev1"))
    assert "description" not in kept


def test_other_list_fields_drop_blanks_but_cells_keep_them():
    loop = TrackLoopArgs(kind="WAITING_ON", title="Quote from the builder", entities=["", " Asha ", " "])
    assert loop.entities == ["Asha"]
    row = SheetAppendArgs(spreadsheet_id="s1", values=["Rent", "", 1200])
    assert row.values == ["Rent", "", 1200]  # a blank cell is a real cell: columns must not shift


def test_email_helper_accepts_display_form_only_with_an_address():
    assert email_address("Dee <dee@z.co>") == "dee@z.co"
    with pytest.raises(ValueError):
        email_address("Dee <>")


# --- every model-facing tool follows the rule ------------------------------------------------------------


def test_every_model_facing_tool_uses_tool_args(workspace_on):
    from mavis.tools import load_builtin_tools

    registry = ToolRegistry()
    load_builtin_tools(registry)  # chat, memory, web and (Workspace on) every integration tool
    tools = list(registry._tools.values())
    assert len(tools) > 30
    loose = [t.name for t in tools if not issubclass(t.args_model, ToolArgs)]
    assert loose == []


# --- a validation error is a tool error in the same turn, never an approval card -------------------------


@pytest.mark.parametrize("tool,args", [
    ("mail_send", {"to": [""], "subject": "Invoice", "body": "Attached."}),
    ("calendar_create_event", {"summary": "Standup", "start": "2026-10-08T09:00", "attendees": ["Ravi"]}),
    ("mail_reply", {"thread_id": "t9", "to": "  ", "body": "Sounds good"}),
    ("slack_send", {"channel": "random", "text": ""}),
])
async def test_invalid_args_return_to_the_model_and_queue_nothing(user, fake_llm, tool, args):
    registry = ToolRegistry()
    register_integration_tools(registry)
    lc_tools = registry.for_agent("conversation", user.id) + registry.for_agent("comms", user.id)
    fake_llm.push_ai(AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": "c1"}]))
    fake_llm.push_text("Which address should I use?")
    res = await react_loop(lc_tools, [HumanMessage("do it")], max_steps=3)
    tool_msg = res.messages[-2]
    assert tool_msg.content.startswith("Tool error")
    assert res.queued_approvals == []
    assert await approvals.open_for_user(user.id) == []

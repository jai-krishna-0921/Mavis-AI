from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability, RiskClass
from mavis.memory.extractor import wrap_untrusted
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
    CalendarUpdateArgs,
    MailComposeArgs,
    localize,
)
from mavis.tools.integrations.base import MAX_RESULT_CHARS, render_result

START = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)  # 10:00 IST, Monday


def test_catalog_covers_spec_actions():
    expected = {
        "mail.search", "mail.read", "mail.thread", "mail.draft", "mail.send", "mail.reply",
        "calendar.list", "calendar.find", "calendar.free_slots", "calendar.create_event",
        "calendar.update_event",
        "slack.channels", "slack.history", "slack.send",
        "notion.search", "notion.read", "notion.create_page",
        # Google Workspace reads (spec 2026-10-03 section 4) and their internal helpers
        "drive.search", "drive.list_recent", "drive.read", "docs.read", "sheets.find", "sheets.read",
        "tasks.list", "contacts.search", "meet.transcript",
        "drive.create_folder", "drive.move", "drive.share", "docs.create", "docs.comment", "sheets.create",
        "tasks.add", "tasks.complete", "tasks.update", "tasks.delete", "meet.create",
        "drive.upload", "drive.upload_file",
        "drive.meta", "drive.permissions", "drive.download", "tasks.get", "mail.profile",
        "contacts.list",
        "docs.append", "sheets.append_row", "sheets.update_range", "docs.insert_text", "tasks.patch",
        # full Workspace read and write
        "mail.archive", "mail.mark_read", "mail.mark_unread", "mail.label", "mail.trash", "mail.untrash",
        "calendar.calendars", "calendar.get", "calendar.delete_event", "calendar.respond",
        "contacts.create", "contacts.update", "slides.read", "slides.create", "forms.read",
        "forms.responses", "meet.recent",
        # Docs, Sheets and Slides sent to the user as files (2026-10-10)
        "drive.export", "drive.export_file",
    }
    assert set(ACTIONS) == expected


def test_calendar_create_without_attendees_is_write_self():
    spec = ACTIONS["calendar.create_event"]
    assert spec.risk_for(CalendarCreateArgs(summary="Focus", start=START)) is RiskClass.WRITE_SELF


def test_calendar_create_with_attendees_is_outward():
    spec = ACTIONS["calendar.create_event"]
    args = CalendarCreateArgs(summary="Interview prep", start=START, attendees=["jawahar@example.com"])
    assert spec.risk_for(args) is RiskClass.OUTWARD


def test_calendar_preview_in_user_timezone():
    spec = ACTIONS["calendar.create_event"]
    args = CalendarCreateArgs(summary="Interview prep", start=START, attendees=["jawahar@example.com"])
    text = spec.preview(args, "Asia/Kolkata")
    assert "Mon 05 Oct, 10:00 to 11:00 (Asia/Kolkata)" in text  # default length: 60 min
    assert "jawahar@example.com" in text
    assert "—" not in text and "–" not in text


def test_mail_send_preview_shows_recipient_subject_body():
    spec = ACTIONS["mail.send"]
    text = spec.preview(MailComposeArgs(to=["a@x.com"], subject="Hi", body="Hello there"), "Asia/Kolkata")
    assert "a@x.com" in text and "Hi" in text and "Hello there" in text


def test_every_approval_action_has_preview():
    for spec in ACTIONS.values():
        if spec.risk.needs_approval or spec.risk_fn is not None:
            assert spec.preview is not None, spec.name


def test_capabilities_match_action_prefix():
    prefix_caps = {"mail": Capability.GMAIL, "calendar": Capability.CALENDAR,
                   "slack": Capability.SLACK, "notion": Capability.NOTION,
                   "drive": Capability.DRIVE, "docs": Capability.DOCS, "sheets": Capability.SHEETS,
                   "tasks": Capability.TASKS, "contacts": Capability.CONTACTS, "meet": Capability.MEET,
                   "slides": Capability.DOCS, "forms": Capability.DOCS}
    for name, spec in ACTIONS.items():
        assert spec.capability is prefix_caps[name.split(".")[0]]


def test_localize_naive_datetimes_to_user_tz():
    naive = CalendarCreateArgs(summary="Interview prep", start=datetime(2026, 10, 5, 10, 0))
    out = localize(naive, "Asia/Kolkata")
    assert out.start.astimezone(UTC) == START


def test_localize_keeps_the_wall_clock_of_aware_datetimes():
    """Phase A7: a model-written offset is never trusted; the wall clock is, in the user's zone."""
    aware = CalendarCreateArgs(summary="x", start=START)
    out = localize(aware, "Asia/Kolkata").start
    assert out.replace(tzinfo=None) == START.replace(tzinfo=None) and str(out.tzinfo) == "Asia/Kolkata"


def test_render_result_truncates():
    text = render_result(ToolResult(ok=True, data={"x": "a" * 10_000}))
    assert len(text) <= MAX_RESULT_CHARS + len(" …[truncated]")
    assert text.endswith("…[truncated]")


def test_render_result_error():
    out = render_result(ToolResult(ok=False, error="nope"))
    assert out == "error: " + wrap_untrusted("nope", "provider_error")


def test_render_result_error_keeps_closing_tag_when_truncated():
    out = render_result(ToolResult(ok=False, error="x" * 20_000))
    assert out.endswith("</untrusted>") and len(out) <= MAX_RESULT_CHARS


def test_calendar_update_is_always_outward():
    """I7: any update can notify or remove existing guests, which the arguments cannot show."""
    from mavis.tools.integrations.tools import _make_tool

    spec = ACTIONS["calendar.update_event"]
    cases = [
        CalendarUpdateArgs(event_id="e1", attendees=[]),  # removes every guest
        CalendarUpdateArgs(event_id="e1", attendees=["a@example.com"]),
        CalendarUpdateArgs(event_id="e1", description="private notes"),
        CalendarUpdateArgs(event_id="e1", start=START, duration_minutes=30),
    ]
    tool = _make_tool(spec)
    for args in cases:
        assert spec.risk_for(args) is RiskClass.OUTWARD
        assert tool.effective_risk(args).needs_approval


def test_calendar_update_preview_shows_exact_times_and_that_the_rest_stays():
    spec = ACTIONS["calendar.update_event"]
    text = spec.preview(CalendarUpdateArgs(event_id="e1", start=START, duration_minutes=90), "UTC")
    end = START + timedelta(minutes=90)
    assert f"to {end:%H:%M}" in text and "everything else stays" in text.lower()


def test_calendar_update_preview_says_the_guest_list_is_replaced():
    spec = ACTIONS["calendar.update_event"]
    text = spec.preview(CalendarUpdateArgs(event_id="e1", attendees=["a@example.com"]), "UTC")
    assert "replaced" in text.lower() and "exactly" in text.lower() and "a@example.com" in text
    text = spec.preview(CalendarUpdateArgs(event_id="e1", attendees=[]), "UTC")
    assert "remov" in text.lower()


async def test_old_approval_with_start_but_no_duration_fails_with_a_clear_message(user, fresh_registry):
    from mavis.domain.errors import ActionFailed
    from mavis.store.db import utcnow
    from mavis.store.repo import approvals
    from mavis.tools.integrations.tools import _make_tool

    fresh_registry.register(_make_tool(ACTIONS["calendar.update_event"]))
    aid = await approvals.create(user.id, None, "calendar_update_event",
                                 {"event_id": "e1", "start": "2026-10-05T10:00:00+05:30"},
                                 "📅 Update event e1", utcnow())
    with pytest.raises(ActionFailed) as exc:
        await fresh_registry.execute_approved(aid)
    assert "how long" in exc.value.reason and "validation" not in exc.value.reason.lower()

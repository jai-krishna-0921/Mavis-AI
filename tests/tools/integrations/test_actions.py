from datetime import UTC, datetime

from mavis.domain.integrations import ToolResult
from mavis.domain.policy import Capability, RiskClass
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
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
    assert "Mon 05 Oct, 10:00 to 10:30 (Asia/Kolkata)" in text
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
                   "slack": Capability.SLACK, "notion": Capability.NOTION}
    for name, spec in ACTIONS.items():
        assert spec.capability is prefix_caps[name.split(".")[0]]


def test_localize_naive_datetimes_to_user_tz():
    naive = CalendarCreateArgs(summary="Interview prep", start=datetime(2026, 10, 5, 10, 0))
    out = localize(naive, "Asia/Kolkata")
    assert out.start.astimezone(UTC) == START


def test_localize_keeps_aware_datetimes():
    aware = CalendarCreateArgs(summary="x", start=START)
    assert localize(aware, "Asia/Kolkata").start == START


def test_render_result_truncates():
    text = render_result(ToolResult(ok=True, data={"x": "a" * 10_000}))
    assert len(text) <= MAX_RESULT_CHARS + len(" …[truncated]")
    assert text.endswith("…[truncated]")


def test_render_result_error():
    assert render_result(ToolResult(ok=False, error="nope")) == "error: nope"

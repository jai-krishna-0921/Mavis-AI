from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mavis.domain.policy import Capability
from mavis.tools.integrations.actions import (
    ACTIONS,
    CalendarCreateArgs,
    MailComposeArgs,
    MailSearchArgs,
)
from mavis.tools.integrations.composio_map import (
    COMPOSIO_ACTIONS,
    COMPOSIO_TRIGGERS,
    MAVIS_TRIGGERS,
    toolkit_of_slug,
)


def test_every_action_is_mapped():
    assert set(COMPOSIO_ACTIONS) == set(ACTIONS)


def test_slug_toolkit_matches_action_capability():
    for name, mapping in COMPOSIO_ACTIONS.items():
        assert toolkit_of_slug(mapping.slug) == ACTIONS[name].capability.value, name


def test_mail_search_translation():
    out = COMPOSIO_ACTIONS["mail.search"].translate(MailSearchArgs(query="is:unread", max_results=5))
    assert out == {"query": "is:unread", "max_results": 5}


def test_mail_send_splits_extra_recipients():
    out = COMPOSIO_ACTIONS["mail.send"].translate(
        MailComposeArgs(to=["a@x.com", "b@x.com"], subject="S", body="B", cc=["c@x.com"])
    )
    assert out["recipient_email"] == "a@x.com"
    assert out["extra_recipients"] == ["b@x.com"]
    assert out["cc"] == ["c@x.com"]
    assert out["subject"] == "S" and out["body"] == "B"


def test_calendar_create_duration_and_timezone():
    start = datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    out = COMPOSIO_ACTIONS["calendar.create_event"].translate(
        CalendarCreateArgs(summary="Prep", start=start, duration_minutes=90, attendees=["j@x.com"])
    )
    assert out["start_datetime"] == "2026-10-05T10:00:00"
    assert out["event_duration_hour"] == 1 and out["event_duration_minutes"] == 30
    assert out["timezone"] == "Asia/Kolkata"
    assert out["attendees"] == ["j@x.com"]


def test_calendar_create_utc_timezone_fallback():
    out = COMPOSIO_ACTIONS["calendar.create_event"].translate(
        CalendarCreateArgs(summary="x", start=datetime(2026, 10, 5, 4, 30, tzinfo=UTC))
    )
    assert out["timezone"] == "UTC"
    assert out["start_datetime"] == "2026-10-05T04:30:00"


def test_calendar_create_fixed_offset_converts_to_naive_utc():
    from datetime import timedelta, timezone

    start = datetime(2026, 10, 5, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out = COMPOSIO_ACTIONS["calendar.create_event"].translate(CalendarCreateArgs(summary="x", start=start))
    assert out["start_datetime"] == "2026-10-05T04:30:00" and out["timezone"] == "UTC"


def test_triggers_cover_all_capabilities_and_map_to_slugs():
    for cap in (Capability.GMAIL, Capability.CALENDAR, Capability.SLACK, Capability.NOTION):
        assert MAVIS_TRIGGERS[cap]
        for trig in MAVIS_TRIGGERS[cap]:
            assert toolkit_of_slug(COMPOSIO_TRIGGERS[trig]) == cap.value

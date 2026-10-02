"""Mavis action/trigger names to Composio slugs and argument shapes. TEMPORARY provider glue.

Slugs and argument keys are verified against the live catalog by scripts/verify_composio.py (Task 17).
If that script reports a mismatch, fix it HERE and nowhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from mavis.domain.policy import Capability


@dataclass(frozen=True)
class SlugMapping:
    slug: str
    translate: Callable[[Any], dict[str, Any]]


def _wall_and_tz(dt: datetime) -> tuple[str, str]:
    """Composio's start_datetime must be naive (no offset or Z), paired with an IANA timezone.

    A ZoneInfo datetime keeps its wall clock; anything else (fixed offset, UTC) is converted to naive UTC.
    """
    if isinstance(dt.tzinfo, ZoneInfo):
        return dt.replace(tzinfo=None).isoformat(timespec="seconds"), dt.tzinfo.key
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), "UTC"


def _compose(a: Any) -> dict[str, Any]:
    return {
        "recipient_email": a.to[0], "extra_recipients": list(a.to[1:]), "cc": list(a.cc),
        "subject": a.subject, "body": a.body,
    }


def _events_list(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "timeMin": a.time_min.isoformat(), "timeMax": a.time_max.isoformat(),
        "maxResults": a.max_results, "singleEvents": True, "orderBy": "startTime",
    }
    if a.updated_min is not None:
        out["updatedMin"] = a.updated_min.isoformat()
    return out


def _create_event(a: Any) -> dict[str, Any]:
    wall, tz = _wall_and_tz(a.start)
    return {
        "summary": a.summary, "start_datetime": wall,
        "event_duration_hour": a.duration_minutes // 60,
        "event_duration_minutes": a.duration_minutes % 60,
        "attendees": list(a.attendees), "description": a.description, "timezone": tz,
    }


def _update_event(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"event_id": a.event_id}
    if a.summary is not None:
        out["summary"] = a.summary
    if a.start is not None:
        out["start_datetime"], out["timezone"] = _wall_and_tz(a.start)
    if a.duration_minutes is not None:
        out["event_duration_hour"] = a.duration_minutes // 60
        out["event_duration_minutes"] = a.duration_minutes % 60
    if a.attendees is not None:
        out["attendees"] = list(a.attendees)
    if a.description is not None:
        out["description"] = a.description
    return out


COMPOSIO_ACTIONS: dict[str, SlugMapping] = {
    "mail.search": SlugMapping(
        "GMAIL_FETCH_EMAILS", lambda a: {"query": a.query, "max_results": a.max_results}
    ),
    "mail.read": SlugMapping("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", lambda a: {"message_id": a.message_id}),
    "mail.thread": SlugMapping("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", lambda a: {"thread_id": a.thread_id}),
    "mail.draft": SlugMapping("GMAIL_CREATE_EMAIL_DRAFT", _compose),
    "mail.send": SlugMapping("GMAIL_SEND_EMAIL", _compose),
    "mail.reply": SlugMapping(
        "GMAIL_REPLY_TO_THREAD",
        lambda a: {"thread_id": a.thread_id, "recipient_email": a.to, "message_body": a.body},
    ),
    "calendar.list": SlugMapping("GOOGLECALENDAR_EVENTS_LIST", _events_list),
    "calendar.find": SlugMapping("GOOGLECALENDAR_FIND_EVENT", lambda a: {"query": a.query}),
    "calendar.free_slots": SlugMapping(
        "GOOGLECALENDAR_FIND_FREE_SLOTS",
        lambda a: {"time_min": a.time_min.isoformat(), "time_max": a.time_max.isoformat()},
    ),
    "calendar.create_event": SlugMapping("GOOGLECALENDAR_CREATE_EVENT", _create_event),
    "calendar.update_event": SlugMapping("GOOGLECALENDAR_UPDATE_EVENT", _update_event),
    "slack.channels": SlugMapping("SLACK_LIST_ALL_CHANNELS", lambda a: {}),
    "slack.history": SlugMapping(
        "SLACK_FETCH_CONVERSATION_HISTORY", lambda a: {"channel": a.channel, "limit": a.limit}
    ),
    "slack.send": SlugMapping("SLACK_SEND_MESSAGE", lambda a: {"channel": a.channel, "text": a.text}),
    "notion.search": SlugMapping("NOTION_SEARCH_NOTION_PAGE", lambda a: {"query": a.query}),
    "notion.read": SlugMapping("NOTION_FETCH_BLOCK_CONTENTS", lambda a: {"block_id": a.page_id}),
    "notion.create_page": SlugMapping(
        "NOTION_CREATE_NOTION_PAGE",
        # The live action creates an EMPTY page and has no body argument; content needs a follow-up
        # NOTION_ADD_PAGE_CONTENT call (not built; Notion is outside the Gmail slice).
        lambda a: {"parent_id": a.parent_id, "title": a.title},
    ),
}

# Provider-agnostic trigger names Mavis subscribes to per capability.
MAVIS_TRIGGERS: dict[Capability, tuple[str, ...]] = {
    Capability.GMAIL: ("mail.new_message",),
    Capability.CALENDAR: ("calendar.event_changed",),
    Capability.SLACK: ("slack.message",),
    Capability.NOTION: ("notion.page_changed",),
}

COMPOSIO_TRIGGERS: dict[str, str] = {
    "mail.new_message": "GMAIL_NEW_GMAIL_MESSAGE",
    "calendar.event_changed": "GOOGLECALENDAR_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER",
    "slack.message": "SLACK_RECEIVE_MESSAGE",
    "notion.page_changed": "NOTION_PAGE_UPDATED_TRIGGER",
}

_TOOLKIT_PREFIX = {
    "GMAIL": "gmail", "GOOGLECALENDAR": "googlecalendar", "SLACK": "slack", "NOTION": "notion",
}


def toolkit_of_slug(slug: str) -> str:
    return _TOOLKIT_PREFIX.get(slug.split("_", 1)[0].upper(), slug.split("_", 1)[0].lower())


def translate(action: str, args: BaseModel) -> tuple[str, dict[str, Any]]:
    m = COMPOSIO_ACTIONS[action]
    return m.slug, m.translate(args)

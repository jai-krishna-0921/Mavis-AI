"""Mavis action/trigger names to Composio slugs and argument shapes. TEMPORARY provider glue.

Slugs and argument keys are verified against the live catalog by scripts/verify_composio.py (Task 17).
If that script reports a mismatch, fix it HERE and nowhere else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from mavis.domain.policy import Capability


@dataclass(frozen=True)
class SlugMapping:
    slug: str
    translate: Callable[[Any], dict[str, Any]]
    file_arg: str | None = None  # the provider stages args.path and sends it under this key (uploads)

    @property
    def suffix(self) -> str:
        """The slug without its toolkit prefix: GMAIL_FETCH_EMAILS -> FETCH_EMAILS."""
        return self.slug.split("_", 1)[1]


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
    # calendarId is REQUIRED by the live GOOGLECALENDAR_EVENTS_LIST schema (no default): without it
    # every call fails with "Following fields are missing: {'calendarId'}".
    out: dict[str, Any] = {
        "calendarId": "primary",
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
        **({"location": a.location} if a.location else {}),
    }


def _update_event(a: Any) -> dict[str, Any]:
    """GOOGLECALENDAR_PATCH_EVENT: only the fields being changed are sent; the rest of the event stays.
    (GOOGLECALENDAR_UPDATE_EVENT is a full PUT replacement: its live description says unspecified
    fields "may be cleared or reset", and it requires start_datetime even for a rename. Composio has
    no read-by-id slug to merge from, so a patch is the safe update.) A move sends start AND end:
    CalendarUpdateArgs requires the duration with the start, so the end is never guessed."""
    out: dict[str, Any] = {"calendar_id": "primary", "event_id": a.event_id}
    if a.summary is not None:
        out["summary"] = a.summary
    if a.start is not None and a.duration_minutes is not None:
        out["start_time"] = a.start.isoformat(timespec="seconds")
        out["end_time"] = (a.start + timedelta(minutes=a.duration_minutes)).isoformat(timespec="seconds")
        if isinstance(a.start.tzinfo, ZoneInfo):
            out["timezone"] = a.start.tzinfo.key
    if a.attendees is not None:
        out["attendees"] = list(a.attendees)
    if a.location is not None:
        out["location"] = a.location
    if a.description is not None:
        out["description"] = a.description
    return out


# --- Google Workspace translations (argument keys verified live 2026-10-03; plan appendix A) ---------------
TASKLIST = "@default"  # Google's alias for the user's primary task list (the trigger config default too)
FILE_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,sharedWithMeTime,"
    "owners(displayName,emailAddress,me),sharingUser(displayName,emailAddress))"
)


def rfc3339(dt: datetime) -> str:
    """Tasks wants RFC 3339 in UTC with a Z."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _drive_query(query: str) -> str:
    q = query.strip()
    return f"({q}) and trashed = false" if q else "trashed = false"


def _drive_search(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"q": _drive_query(a.query), "pageSize": a.max_results, "fields": FILE_FIELDS}
    if "fulltext" not in a.query.lower():  # Drive refuses orderBy together with fullText terms
        out["orderBy"] = "modifiedTime desc"
    return out


def _drive_recent(a: Any) -> dict[str, Any]:
    if a.shared_with_me:
        return {"q": "sharedWithMe and trashed = false", "orderBy": "sharedWithMeTime desc",
                "pageSize": a.max_results, "fields": FILE_FIELDS}
    return {"q": "trashed = false", "orderBy": "modifiedTime desc", "pageSize": a.max_results,
            "fields": FILE_FIELDS}


def _download(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"file_id": a.file_id}
    if a.mime_type:
        out["mime_type"] = a.mime_type
    return out


def _sheets_find(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"max_results": a.max_results}
    if a.query.strip():
        out["query"] = a.query.strip()
    return out


def _sheets_read(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"spreadsheet_id": a.spreadsheet_id}
    if a.range.strip():
        out["ranges"] = [a.range.strip()]
    return out


def _tasks_list(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tasklist_id": TASKLIST, "showCompleted": a.show_completed, "maxResults": a.max_results,
    }
    if a.due_before is not None:
        out["dueMax"] = rfc3339(a.due_before)
    return out


def _due(day: Any) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"  # Tasks keeps the date only


def _folder(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"folder_name": a.name}
    if a.parent_id:
        out["parent_id"] = a.parent_id
    return out


def _move(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"file_id": a.file_id, "add_parents": a.to_folder_id}
    if a.from_folder_id:
        out["remove_parents"] = a.from_folder_id
    return out


def _task_add(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"tasklist_id": TASKLIST, "title": a.title, "status": "needsAction"}
    if a.notes:
        out["notes"] = a.notes
    if a.due is not None:
        out["due"] = _due(a.due)
    return out


def _task_patch(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tasklist_id": TASKLIST, "task_id": a.task_id, "title": a.title, "status": a.status,
    }
    if a.notes is not None:
        out["notes"] = a.notes
    if a.due is not None:
        out["due"] = _due(a.due)
    return out


FORMULA_PREFIXES = ("=", "+", "-", "@")


def input_option(values: list[Any]) -> str:
    """RAW when any cell starts like a formula: text from a file or email must never become =IMAGE(...)."""
    cells = [c for v in values for c in (v if isinstance(v, list) else [v])]
    formula = any(isinstance(c, str) and c.startswith(FORMULA_PREFIXES) for c in cells)
    return "RAW" if formula else "USER_ENTERED"


# Full-scope Workspace actions that only the native Google executor serves (no Composio slug is mapped).
# Their router entries (native/router.ACTION_SCOPES) are what let them run; for a Composio-only account the
# provider answers "unknown action".
NATIVE_ONLY_ACTIONS: frozenset[str] = frozenset({
    "mail.archive", "mail.mark_read", "mail.mark_unread", "mail.label", "mail.trash", "mail.untrash",
    "calendar.calendars", "calendar.get", "calendar.delete_event", "calendar.respond",
    "contacts.create", "contacts.update", "slides.read", "slides.create", "forms.read", "forms.responses",
    "meet.recent", "drive.export_file",
})

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
    "calendar.update_event": SlugMapping("GOOGLECALENDAR_PATCH_EVENT", _update_event),
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
    # Google Workspace: googlesuper only, so the stored slug already carries the GOOGLESUPER_ prefix.
    # drive.read has no mapping: workspace_tools.drive_read calls drive.meta and drive.download.
    "drive.search": SlugMapping("GOOGLESUPER_FIND_FILE", _drive_search),
    "drive.list_recent": SlugMapping("GOOGLESUPER_LIST_FILES", _drive_recent),
    "docs.read": SlugMapping("GOOGLESUPER_GET_DOCUMENT_BY_ID", lambda a: {"id": a.document_id}),
    "sheets.find": SlugMapping("GOOGLESUPER_SEARCH_SPREADSHEETS", _sheets_find),
    "sheets.read": SlugMapping("GOOGLESUPER_BATCH_GET", _sheets_read),
    "tasks.list": SlugMapping("GOOGLESUPER_LIST_TASKS", _tasks_list),
    "contacts.search": SlugMapping(
        "GOOGLESUPER_SEARCH_PEOPLE", lambda a: {"query": a.query, "pageSize": a.max_results}
    ),
    "meet.transcript": SlugMapping(
        "GOOGLESUPER_GET_TRANSCRIPTS_BY_CONFERENCE_RECORD_ID",
        lambda a: {"conferenceRecord_id": a.conference_record_id},
    ),
    "drive.create_folder": SlugMapping("GOOGLESUPER_CREATE_FOLDER", _folder),
    "drive.move": SlugMapping("GOOGLESUPER_MOVE_FILE", _move),
    "drive.share": SlugMapping(
        "GOOGLESUPER_ADD_FILE_SHARING_PREFERENCE",
        lambda a: {"file_id": a.file_id, "role": a.role, "type": "user", "email_address": a.email},
    ),
    "docs.create": SlugMapping(
        "GOOGLESUPER_CREATE_DOCUMENT_MARKDOWN", lambda a: {"title": a.title, "markdown_text": a.markdown}
    ),
    "docs.comment": SlugMapping(
        "GOOGLESUPER_CREATE_COMMENT", lambda a: {"file_id": a.file_id, "content": a.content}
    ),
    "sheets.create": SlugMapping("GOOGLESUPER_CREATE_GOOGLE_SHEET1", lambda a: {"title": a.title}),
    "tasks.add": SlugMapping("GOOGLESUPER_INSERT_TASK", _task_add),
    # tasks.complete and tasks.update have no mapping: workspace_tools reads the task (tasks.get) and
    # sends its real title and status through tasks.patch (PATCH_TASK requires both)
    "tasks.patch": SlugMapping("GOOGLESUPER_PATCH_TASK", _task_patch),
    "tasks.delete": SlugMapping(
        "GOOGLESUPER_DELETE_TASK", lambda a: {"tasklist_id": TASKLIST, "task_id": a.task_id}
    ),
    "meet.create": SlugMapping("GOOGLESUPER_CREATE_MEET", lambda a: {}),
    # docs.append has no mapping: workspace_tools.docs_append finds the end index, then calls this
    "docs.insert_text": SlugMapping(
        "GOOGLESUPER_INSERT_TEXT_ACTION",
        lambda a: {"document_id": a.document_id, "text_to_insert": a.text, "insertion_index": a.index},
    ),
    "sheets.append_row": SlugMapping(
        "GOOGLESUPER_SPREADSHEETS_VALUES_APPEND",
        lambda a: {"spreadsheetId": a.spreadsheet_id, "range": a.range,
                   "valueInputOption": input_option(a.values), "insertDataOption": "INSERT_ROWS",
                   "values": [list(a.values)]},
    ),
    "sheets.update_range": SlugMapping(
        "GOOGLESUPER_BATCH_UPDATE",
        lambda a: {"spreadsheet_id": a.spreadsheet_id, "sheet_name": a.sheet_name,
                   "first_cell_location": a.start_cell.upper(), "values": [list(r) for r in a.values],
                   "valueInputOption": input_option(a.values)},
    ),
    # drive.upload has no mapping: workspace_tools.drive_upload resolves the artifact, then calls this
    "drive.upload_file": SlugMapping(
        "GOOGLESUPER_UPLOAD_FILE",
        lambda a: {"folder_to_upload_to": a.folder_id} if a.folder_id else {},
        file_arg="file_to_upload",
    ),
    "drive.meta": SlugMapping("GOOGLESUPER_GET_FILE_METADATA", lambda a: {"fileId": a.file_id}),
    "drive.permissions": SlugMapping("GOOGLESUPER_LIST_PERMISSIONS", lambda a: {"fileId": a.file_id}),
    "drive.download": SlugMapping("GOOGLESUPER_DOWNLOAD_FILE", _download),
    "tasks.get": SlugMapping(
        "GOOGLESUPER_GET_TASK", lambda a: {"tasklist_id": TASKLIST, "task_id": a.task_id}
    ),
    "mail.profile": SlugMapping("GMAIL_GET_PROFILE", lambda a: {}),
    "contacts.list": SlugMapping("GOOGLESUPER_GET_CONTACTS", lambda a: {"person_fields": "emailAddresses"}),
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

# --- Google Workspace routing (spec 2026-10-03 section 3.2) -----------------------------------------
# Every Google capability resolves to googlesuper; Gmail and Calendar fall back to their legacy toolkits.
GOOGLESUPER = "googlesuper"
GOOGLESUPER_PREFIX = "GOOGLESUPER_"
LEGACY_TOOLKITS: dict[Capability, str] = {Capability.GMAIL: "gmail", Capability.CALENDAR: "googlecalendar"}
# /disconnect names for the legacy accounts, which a googlesuper disconnect leaves untouched
LEGACY_ALIASES: dict[str, str] = {
    "gmail-legacy": "gmail", "calendar-legacy": "googlecalendar", "googlecalendar-legacy": "googlecalendar",
}

GOOGLESUPER_TRIGGERS: dict[str, str] = {
    "mail.new_message": "GOOGLESUPER_NEW_MESSAGE",
    "calendar.event_changed": "GOOGLESUPER_GOOGLE_CALENDAR_EVENT_CHANGE_TRIGGER",
    "drive.file_shared": "GOOGLESUPER_FILE_SHARED_PERMISSIONS_ADDED",
    "docs.comment_added": "GOOGLESUPER_COMMENT_ADDED_TRIGGER",
    "tasks.created": "GOOGLESUPER_NEW_TASK_CREATED_TRIGGER",
    "tasks.updated": "GOOGLESUPER_TASK_UPDATED_TRIGGER",
}
WORKSPACE_TRIGGERS: dict[Capability, tuple[str, ...]] = {
    Capability.DRIVE: ("drive.file_shared",),
    Capability.DOCS: ("docs.comment_added",),
    Capability.TASKS: ("tasks.created", "tasks.updated"),
}
# Composio polls these itself; slower than its 2-minute default to spare the user's Drive quota.
TRIGGER_CONFIGS: dict[str, dict[str, Any]] = {
    "drive.file_shared": {"interval": 5},
    "docs.comment_added": {"interval": 10, "max_files": 25},
    "tasks.created": {"interval": 15, "tasklist_id": "@default"},
    "tasks.updated": {"interval": 15, "tasklist_id": "@default"},
}


def triggers_for(capability: Capability, *, workspace: bool) -> tuple[str, ...]:
    """Mavis trigger names to subscribe when `capability` becomes active."""
    if workspace and capability in WORKSPACE_TRIGGERS:
        return WORKSPACE_TRIGGERS[capability]
    return MAVIS_TRIGGERS.get(capability, ())


def slug_for(action: str, toolkit: str) -> str:
    """The Composio slug for `action` on `toolkit`. Nothing outside this module knows the prefixes."""
    mapping = COMPOSIO_ACTIONS[action]
    if toolkit == GOOGLESUPER:
        return GOOGLESUPER_PREFIX + mapping.suffix
    return mapping.slug


_TOOLKIT_PREFIX = {
    "GMAIL": "gmail", "GOOGLECALENDAR": "googlecalendar", "SLACK": "slack", "NOTION": "notion",
    "GOOGLESUPER": GOOGLESUPER,
}


def toolkit_of_slug(slug: str) -> str:
    return _TOOLKIT_PREFIX.get(slug.split("_", 1)[0].upper(), slug.split("_", 1)[0].lower())


def translate(action: str, args: BaseModel) -> tuple[str, dict[str, Any]]:
    m = COMPOSIO_ACTIONS[action]
    return m.slug, m.translate(args)

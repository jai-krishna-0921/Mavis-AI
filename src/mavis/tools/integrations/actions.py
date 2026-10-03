"""Mavis action catalog: provider-agnostic names, argument models, risk and previews.

Adding a provider means mapping these names; adding an action means one entry here plus a mapping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from mavis.config import get_settings
from mavis.domain.policy import Capability, RiskClass

INTEGRATION_CAPABILITIES: tuple[Capability, ...] = (
    Capability.GMAIL, Capability.CALENDAR, Capability.SLACK, Capability.NOTION,
)
# Google Workspace (spec 2026-10-03): one googlesuper consent covers all eight Google capabilities.
WORKSPACE_CAPABILITIES: tuple[Capability, ...] = (
    Capability.DRIVE, Capability.DOCS, Capability.SHEETS, Capability.TASKS, Capability.CONTACTS,
    Capability.MEET,
)
GOOGLE_CAPABILITIES: tuple[Capability, ...] = (Capability.GMAIL, Capability.CALENDAR, *WORKSPACE_CAPABILITIES)
GOOGLE_NAME = "Google"
WORKSPACE_ROW = "Google Workspace"
# The capability a "/connect google" starts: only googlesuper can make it ACTIVE, so a user with just the
# legacy Gmail connection still gets the upgrade link instead of "already connected".
GOOGLE_ANCHOR = Capability.DRIVE
GOOGLE_ABILITIES = "Gmail, Calendar, Drive, Docs, Sheets, Tasks, Contacts and Meet"
DISPLAY_NAMES: dict[Capability, str] = {
    Capability.GMAIL: "Gmail", Capability.CALENDAR: "Google Calendar",
    Capability.SLACK: "Slack", Capability.NOTION: "Notion",
    Capability.DRIVE: "Google Drive", Capability.DOCS: "Google Docs", Capability.SHEETS: "Google Sheets",
    Capability.TASKS: "Google Tasks", Capability.CONTACTS: "Google Contacts", Capability.MEET: "Google Meet",
}
BRANDS: dict[Capability, str] = {
    Capability.GMAIL: "Google", Capability.CALENDAR: "Google",
    Capability.SLACK: "Slack", Capability.NOTION: "Notion",
    **{c: "Google" for c in WORKSPACE_CAPABILITIES},
}
CAPABILITY_PURPOSE: dict[Capability, str] = {
    Capability.GMAIL: "check and handle your email",
    Capability.CALENDAR: "work with your calendar",
    Capability.SLACK: "work with your Slack",
    Capability.NOTION: "work with your Notion pages",
    Capability.DRIVE: "find and work with your Drive files",
    Capability.DOCS: "read and write your Google Docs",
    Capability.SHEETS: "work with your Google Sheets",
    Capability.TASKS: "manage your to-do list in Google Tasks",
    Capability.CONTACTS: "look up your contacts",
    Capability.MEET: "set up Google Meet calls",
}


def workspace_enabled() -> bool:
    return get_settings().google_workspace_enabled


def active_capabilities() -> tuple[Capability, ...]:
    """Capabilities Mavis offers right now. With the Workspace flag off this is exactly the old four."""
    if workspace_enabled():
        return INTEGRATION_CAPABILITIES + WORKSPACE_CAPABILITIES
    return INTEGRATION_CAPABILITIES


def is_google(capability: Capability) -> bool:
    """Routed through the one Google consent (only while the Workspace flag is on)."""
    return workspace_enabled() and capability in GOOGLE_CAPABILITIES


def display_name(capability: Capability) -> str:
    """What connect prompts call the account: "Google" for every Google capability when Workspace is on."""
    return GOOGLE_NAME if is_google(capability) else DISPLAY_NAMES.get(capability, capability.value)


# --- argument models -----------------------------------------------------------------------------


class MailSearchArgs(BaseModel):
    query: str = Field(
        default="", description="Gmail search syntax, e.g. 'from:alice newer_than:7d is:unread'"
    )
    max_results: int = Field(default=10, ge=1, le=50)


class MailReadArgs(BaseModel):
    message_id: str


class MailThreadArgs(BaseModel):
    thread_id: str


class MailComposeArgs(BaseModel):
    to: list[str] = Field(min_length=1, description="Recipient email addresses")
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)


class MailReplyArgs(BaseModel):
    thread_id: str
    to: str
    body: str


class CalendarListArgs(BaseModel):
    time_min: datetime
    time_max: datetime
    max_results: int = Field(default=20, ge=1, le=100)
    updated_min: datetime | None = Field(
        default=None, description="Only events changed since (used by sync)"
    )


class CalendarFindArgs(BaseModel):
    query: str


class CalendarSlotsArgs(BaseModel):
    time_min: datetime
    time_max: datetime


class CalendarCreateArgs(BaseModel):
    summary: str
    start: datetime = Field(description="Start time; include the offset if known")
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    attendees: list[str] = Field(
        default_factory=list, description="Guest emails; adding guests sends invites"
    )
    description: str = ""


class CalendarUpdateArgs(BaseModel):
    event_id: str
    summary: str | None = None
    start: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=1440)
    attendees: list[str] | None = None
    description: str | None = None


class SlackChannelsArgs(BaseModel):
    pass


class SlackHistoryArgs(BaseModel):
    channel: str = Field(description="Channel id or name")
    limit: int = Field(default=20, ge=1, le=100)


class SlackSendArgs(BaseModel):
    channel: str
    text: str


class NotionSearchArgs(BaseModel):
    query: str = ""


class NotionReadArgs(BaseModel):
    page_id: str


class NotionCreateArgs(BaseModel):
    parent_id: str = Field(description="Parent page id")
    title: str
    content: str = Field(default="", description="Markdown body")


# --- Google Workspace argument models (spec 2026-10-03 section 4) ---------------------------------------


class NoArgs(BaseModel):
    pass


class DriveSearchArgs(BaseModel):
    query: str = Field(
        default="",
        description="Drive search syntax, e.g. \"name contains 'budget'\", \"fullText contains 'Priya'\", "
                    "\"'priya@example.com' in owners\", \"modifiedTime > '2026-10-01T00:00:00'\"",
    )
    max_results: int = Field(default=10, ge=1, le=25)


class DriveRecentArgs(BaseModel):
    shared_with_me: bool = Field(default=False, description="Only files other people shared with the user")
    max_results: int = Field(default=10, ge=1, le=25)


class FileArgs(BaseModel):
    file_id: str = Field(min_length=1, description="Drive file id (from drive_search or drive_list_recent)")


class DriveDownloadArgs(BaseModel):
    file_id: str = Field(min_length=1)
    mime_type: str = Field(default="", description="Export type for Google Docs, Sheets and Slides")


class DocArgs(BaseModel):
    document_id: str = Field(min_length=1, description="Google Doc id (a Drive file id)")


class SheetsFindArgs(BaseModel):
    query: str = Field(default="", description="e.g. \"name contains 'budget'\"; empty lists recent sheets")
    max_results: int = Field(default=10, ge=1, le=25)


class SheetsReadArgs(BaseModel):
    spreadsheet_id: str = Field(min_length=1)
    range: str = Field(default="", description="A1 range like 'Sheet1!A1:F50'; empty reads the first sheet")


class TasksListArgs(BaseModel):
    due_before: datetime | None = Field(
        default=None, description="Only tasks due before this time (now = overdue, end of today = due today)"
    )
    show_completed: bool = False
    max_results: int = Field(default=50, ge=1, le=100)


class TaskRefArgs(BaseModel):
    task_id: str = Field(min_length=1, description="Task id from tasks_list")


class ContactsSearchArgs(BaseModel):
    query: str = Field(min_length=2, description="A name, email or phone number")
    max_results: int = Field(default=10, ge=1, le=30)


class MeetTranscriptArgs(BaseModel):
    conference_record_id: str = Field(min_length=1, description="Conference record id, e.g. 'abc-123'")


# --- helpers --------------------------------------------------------------------------------------


def localize(args: BaseModel, timezone: str) -> BaseModel:
    """Models often emit naive datetimes ('2026-10-05T10:00'). Interpret those in the user's timezone."""
    tz = ZoneInfo(timezone)
    updates = {
        name: value.replace(tzinfo=tz)
        for name in type(args).model_fields
        if isinstance(value := getattr(args, name), datetime) and value.tzinfo is None
    }
    return args.model_copy(update=updates) if updates else args


def _when(start: datetime, minutes: int, tz: str) -> str:
    z = ZoneInfo(tz)
    s = start.astimezone(z)
    e = (start + timedelta(minutes=minutes)).astimezone(z)
    return f"{s:%a %d %b}, {s:%H:%M} to {e:%H:%M} ({tz})"


def _attendee_risk(args: BaseModel) -> RiskClass:
    return RiskClass.OUTWARD if getattr(args, "attendees", None) else RiskClass.WRITE_SELF


def _update_risk(args: BaseModel) -> RiskClass:
    """An update is OUTWARD whatever it changes. The arguments cannot show whether the event already has
    guests, and Google tells every guest about a change (time, title, description) or a removal; an
    empty `attendees` list removes them all and sends cancellations. Checking the live event would need a
    read before the risk decision, so every update waits for the user's OK."""
    return RiskClass.OUTWARD


def _preview_mail(args: MailComposeArgs, tz: str) -> str:
    cc = f"\nCc: {', '.join(args.cc)}" if args.cc else ""
    return f"✉️ To: {', '.join(args.to)}{cc}\nSubject: {args.subject}\n\n{args.body}"


def _preview_draft(args: MailComposeArgs, tz: str) -> str:
    return "📝 Draft\n" + _preview_mail(args, tz)


def _preview_reply(args: MailReplyArgs, tz: str) -> str:
    return f"↩️ Reply to {args.to}\n\n{args.body}"


def _preview_create(args: CalendarCreateArgs, tz: str) -> str:
    who = f"\nWith: {', '.join(args.attendees)}" if args.attendees else "\nJust you"
    desc = f"\n{args.description}" if args.description else ""
    return f"📅 {args.summary}\n{_when(args.start, args.duration_minutes, tz)}{who}{desc}"


def _preview_update(args: CalendarUpdateArgs, tz: str) -> str:
    lines = [f"📅 Update event {args.event_id}"]
    if args.summary:
        lines.append(f"Title: {args.summary}")
    if args.start:
        lines.append(f"When: {_when(args.start, args.duration_minutes or 30, tz)}")
    if args.attendees is not None:
        lines.append(f"Guests: {', '.join(args.attendees) or 'none'}")
    if args.description is not None:
        lines.append(f"Notes: {args.description}")
    return "\n".join(lines)


def _preview_slack(args: SlackSendArgs, tz: str) -> str:
    return f"💬 #{args.channel.lstrip('#')}\n{args.text}"


def _preview_notion(args: NotionCreateArgs, tz: str) -> str:
    return f"🗒️ New Notion page: {args.title}\n{args.content[:400]}"


# --- catalog --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    name: str
    capability: Capability
    description: str
    args_model: type[BaseModel]
    risk: RiskClass
    agents: frozenset[str]
    risk_fn: Callable[[BaseModel], RiskClass] | None = None
    preview: Callable[[BaseModel, str], str] | None = None  # (args, timezone) -> text
    priority: int = 50  # registry.select tie-break when a chat message shares no words with any tool
    taint_approve: bool = False  # after untrusted output in the run, queue for approval (Workspace spec 4.3)

    def risk_for(self, args: BaseModel) -> RiskClass:
        return self.risk_fn(args) if self.risk_fn else self.risk


def _a(*names: str) -> frozenset[str]:
    return frozenset(names)


_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("mail.search", Capability.GMAIL,
               "Search or find emails in the user's Gmail. Returns message ids, senders, subjects, dates and "
               "short previews.",
               MailSearchArgs, RiskClass.READ, _a("inbox", "conversation"), priority=58),
    ActionSpec("mail.read", Capability.GMAIL,
               "Read one email in full by message id (from mail_search): headers and body text.",
               MailReadArgs, RiskClass.READ, _a("inbox", "conversation")),
    ActionSpec("mail.thread", Capability.GMAIL, "Read every message in an email thread.",
               MailThreadArgs, RiskClass.READ, _a("inbox")),
    ActionSpec("mail.draft", Capability.GMAIL, "Create a Gmail draft (not sent).",
               MailComposeArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation"), preview=_preview_draft),
    ActionSpec("mail.send", Capability.GMAIL, "Send an email. The user is asked to approve first.",
               MailComposeArgs, RiskClass.OUTWARD, _a("inbox", "conversation"), preview=_preview_mail),
    ActionSpec("mail.reply", Capability.GMAIL,
               "Reply on an existing thread. The user is asked to approve first.",
               MailReplyArgs, RiskClass.OUTWARD, _a("inbox", "conversation"), preview=_preview_reply),
    ActionSpec("calendar.list", Capability.CALENDAR, "List calendar events in a time window.",
               CalendarListArgs, RiskClass.READ, _a("calendar", "conversation"), priority=56),
    ActionSpec("calendar.find", Capability.CALENDAR, "Find calendar events matching text.",
               CalendarFindArgs, RiskClass.READ, _a("calendar", "conversation")),
    ActionSpec("calendar.free_slots", Capability.CALENDAR, "Find free time between two times.",
               CalendarSlotsArgs, RiskClass.READ, _a("calendar", "conversation")),
    ActionSpec("calendar.create_event", Capability.CALENDAR,
               "Create a calendar event or meeting. With guests, invites are sent after the user approves.",
               CalendarCreateArgs, RiskClass.WRITE_SELF, _a("calendar", "conversation"),
               risk_fn=_attendee_risk, preview=_preview_create),
    ActionSpec("calendar.update_event", Capability.CALENDAR,
               "Change an event. The user is asked to approve first (guests may be notified).",
               CalendarUpdateArgs, RiskClass.WRITE_SELF, _a("calendar"),
               risk_fn=_update_risk, preview=_preview_update),
    ActionSpec("slack.channels", Capability.SLACK, "List Slack channels.",
               SlackChannelsArgs, RiskClass.READ, _a("comms")),
    ActionSpec("slack.history", Capability.SLACK, "Recent messages in a Slack channel.",
               SlackHistoryArgs, RiskClass.READ, _a("comms")),
    ActionSpec("slack.send", Capability.SLACK, "Post a Slack message. The user is asked to approve first.",
               SlackSendArgs, RiskClass.OUTWARD, _a("comms"), preview=_preview_slack),
    ActionSpec("notion.search", Capability.NOTION, "Search Notion pages by text.",
               NotionSearchArgs, RiskClass.READ, _a("knowledge")),
    ActionSpec("notion.read", Capability.NOTION, "Read a Notion page.",
               NotionReadArgs, RiskClass.READ, _a("knowledge")),
    ActionSpec("notion.create_page", Capability.NOTION, "Create a Notion page with markdown content.",
               NotionCreateArgs, RiskClass.WRITE_SELF, _a("knowledge"), preview=_preview_notion),
)

# Workspace exposure (spec 4.4): chat gets reads plus a few self-only writes; spawned workers get all.
_CHAT = _a("conversation", "spawn")
_INTERNAL = frozenset[str]()  # used by other actions and polls, never offered to a model

_WORKSPACE_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("drive.search", Capability.DRIVE,
               "Search the user's Google Drive files (docs, sheets, slides, decks, PDFs) by name, text, "
               "owner or date. Returns file ids, titles, owners and dates.",
               DriveSearchArgs, RiskClass.READ, _CHAT, priority=55),
    ActionSpec("drive.list_recent", Capability.DRIVE,
               "List recently changed Google Drive files, or files recently shared with the user.",
               DriveRecentArgs, RiskClass.READ, _CHAT),
    ActionSpec("drive.read", Capability.DRIVE,
               "Read the text of a Drive file by file id: Google Docs, Sheets (as CSV), Slides, text files.",
               FileArgs, RiskClass.READ, _CHAT),
    ActionSpec("docs.read", Capability.DOCS, "Read a Google Doc by document id: title and text.",
               DocArgs, RiskClass.READ, _CHAT),
    ActionSpec("sheets.find", Capability.SHEETS, "Find Google Sheets spreadsheets by name or content.",
               SheetsFindArgs, RiskClass.READ, _CHAT),
    ActionSpec("sheets.read", Capability.SHEETS,
               "Read cells from a Google Sheet by spreadsheet id and optional A1 range (first 50 rows).",
               SheetsReadArgs, RiskClass.READ, _CHAT),
    ActionSpec("tasks.list", Capability.TASKS,
               "Show the user's to-do list (Google Tasks): open tasks and due dates. Set due_before to now "
               "for overdue tasks, or to the end of today for what is due today.",
               TasksListArgs, RiskClass.READ, _CHAT, priority=57),
    ActionSpec("contacts.search", Capability.CONTACTS,
               "Look up a person in the user's Google Contacts: name to email address and phone number.",
               ContactsSearchArgs, RiskClass.READ, _CHAT),
    ActionSpec("meet.transcript", Capability.MEET,
               "List the transcripts of a Google Meet conference; each transcript is a Google Doc to read "
               "with docs_read.",
               MeetTranscriptArgs, RiskClass.READ, _CHAT),
    # internal: file metadata and permissions (risk escalation, allowlist), downloads, task lookup, profile
    ActionSpec("drive.meta", Capability.DRIVE, "File name and type.", FileArgs, RiskClass.READ, _INTERNAL),
    ActionSpec("drive.permissions", Capability.DRIVE, "Who can access a file.", FileArgs, RiskClass.READ,
               _INTERNAL),
    ActionSpec("drive.download", Capability.DRIVE, "Export or download a file.", DriveDownloadArgs,
               RiskClass.READ, _INTERNAL),
    ActionSpec("tasks.get", Capability.TASKS, "One task by id.", TaskRefArgs, RiskClass.READ, _INTERNAL),
    ActionSpec("mail.profile", Capability.GMAIL, "The user's own email address.", NoArgs, RiskClass.READ,
               _INTERNAL),
)

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in (*_SPECS, *_WORKSPACE_SPECS)}

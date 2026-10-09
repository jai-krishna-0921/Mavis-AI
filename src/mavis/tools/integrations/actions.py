"""Mavis action catalog: provider-agnostic names, argument models, risk and previews.

Adding a provider means mapping these names; adding an action means one entry here plus a mapping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from mavis.config import get_settings
from mavis.domain.args import Email, ToolArgs
from mavis.domain.errors import NeedsUserDetail
from mavis.domain.localtime import LocalTimes, localize_args, wall_clock
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
GOOGLE_UNVERIFIED_NOTICE = "Google will say it has not verified Mavis yet. Tap Advanced, then Go to Mavis AI."


def consent_notice(capability: Capability) -> str:
    """One short line to read before opening a consent screen that will look alarming. Today that is Google's
    "has not verified this app" page for our own (External, unverified) OAuth app; empty everywhere else."""
    s = get_settings()
    if BRANDS.get(capability) != "Google" or s.integration_provider != "native" or s.google_oauth_verified:
        return ""
    return GOOGLE_UNVERIFIED_NOTICE


CAPABILITY_PURPOSE: dict[Capability, str] = {
    Capability.GMAIL: "check and handle your email",
    Capability.CALENDAR: "work with your calendar",
    Capability.SLACK: "work with your Slack",
    Capability.NOTION: "work with your Notion pages",
    Capability.DRIVE: "find and work with your Drive files",
    Capability.DOCS: "read and write your Google Docs, Slides and Forms",
    Capability.SHEETS: "work with your Google Sheets",
    Capability.TASKS: "manage your to-do list in Google Tasks",
    Capability.CONTACTS: "look up your contacts",
    Capability.MEET: "set up Google Meet calls",
}


# Capabilities Mavis can serve itself (its own loops and reminders) when the external account is not
# linked: a chat turn then never offers those tools, so a missing link cannot hijack the turn with a
# connect prompt. Accounts with no internal equivalent (mail, calendar...) are not listed here.
INTERNAL_EQUIVALENT: frozenset[Capability] = frozenset({Capability.TASKS})


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


class MailSearchArgs(ToolArgs):
    query: str = Field(
        default="", description="Gmail search syntax, e.g. 'from:alice newer_than:7d is:unread'"
    )
    max_results: int = Field(default=10, ge=1, le=50)


class MailReadArgs(ToolArgs):
    message_id: str


class MailThreadArgs(ToolArgs):
    thread_id: str


class MailComposeArgs(ToolArgs):
    to: list[Email] = Field(min_length=1, description="Recipient email addresses")
    subject: str
    body: str
    cc: list[Email] = Field(default_factory=list)


class MailReplyArgs(ToolArgs):
    thread_id: str
    to: Email
    body: str


CALENDAR_ID_HELP = ("Calendar id from calendar_calendars (a shared or secondary calendar); "
                    "'primary' is the user's own calendar")


class CalendarListArgs(LocalTimes):
    calendar_id: str = Field(default="primary", description=CALENDAR_ID_HELP)
    time_min: datetime = Field(description=wall_clock("Window start"))
    time_max: datetime = Field(description=wall_clock("Window end"))
    max_results: int = Field(default=20, ge=1, le=100)
    updated_min: datetime | None = Field(
        default=None, description=wall_clock("Only events changed since (used by sync)")
    )


class CalendarFindArgs(ToolArgs):
    query: str
    calendar_id: str = Field(default="primary", description=CALENDAR_ID_HELP)


class CalendarSlotsArgs(LocalTimes):
    time_min: datetime = Field(description=wall_clock("Window start"))
    time_max: datetime = Field(description=wall_clock("Window end"))


class CalendarCreateArgs(LocalTimes):
    summary: str
    start: datetime = Field(description=wall_clock("Start time"))
    # Only a start given: the configured default length (hotfix4 H6), never a question about the length.
    duration_minutes: int = Field(
        default_factory=lambda: get_settings().default_event_minutes, ge=5, le=1440,
        description="Length in minutes; leave it out when they gave only a start (the default is used)")
    attendees: list[Email] = Field(
        default_factory=list, description="Guest emails; adding guests sends invites"
    )
    description: str = ""


class CalendarUpdateArgs(LocalTimes):
    """Only the fields that change; everything else on the event stays as it is."""

    event_id: str
    # a title identifies the event: it can change, never be blanked (blank = keep it, see domain.args)
    summary: str | None = Field(default=None, min_length=1, description="A new title; empty keeps it")
    start: datetime | None = Field(
        default=None,
        description=wall_clock("New start time. Moving an event needs duration_minutes too (its current "
                               "length if that is not changing)"),
    )
    duration_minutes: int | None = Field(
        default=None, ge=5, le=1440,
        description="Length in minutes; goes with start (pass the event's current start to change only "
                    "the length)",
    )
    attendees: list[Email] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _start_with_duration(self) -> CalendarUpdateArgs:
        # The provider takes a start and an end; an end is never guessed from a default length.
        if self.start is not None and self.duration_minutes is None:
            raise NeedsUserDetail(
                "duration_minutes is required with start: pass the event's current length if it is "
                "not changing",
                "I need to know how long the event should be to move it. Tell me the length and I'll "
                "set it up again.")
        if self.duration_minutes is not None and self.start is None:
            raise NeedsUserDetail(
                "start is required with duration_minutes: pass the event's current start if it is "
                "not moving",
                "I need to know when the event starts to change its length. Tell me and I'll set it "
                "up again.")
        return self


class SlackChannelsArgs(ToolArgs):
    pass


class SlackHistoryArgs(ToolArgs):
    channel: str = Field(description="Channel id or name")
    limit: int = Field(default=20, ge=1, le=100)
    thread_ts: str = Field(default="", description="Read the replies of the thread that starts at this ts")
    oldest: str = Field(default="", description="Only messages after this Slack ts")
    cursor: str = Field(default="", description="next_cursor from the previous page")


class SlackSendArgs(ToolArgs):
    channel: str
    text: str
    thread_ts: str = Field(default="", description="Reply inside the thread that starts at this ts")


class NotionSearchArgs(ToolArgs):
    query: str = ""


class NotionReadArgs(ToolArgs):
    page_id: str


class NotionCreateArgs(ToolArgs):
    parent_id: str = Field(description="Parent page id")
    title: str
    content: str = Field(default="", description="Markdown body")


# --- Google Workspace argument models (spec 2026-10-03 section 4) ---------------------------------------


class NoArgs(ToolArgs):
    pass


class DriveSearchArgs(ToolArgs):
    query: str = Field(
        default="",
        description="Drive search syntax, e.g. \"name contains 'budget'\", \"fullText contains 'Priya'\", "
                    "\"'priya@example.com' in owners\", \"modifiedTime > '2026-10-01T00:00:00'\"",
    )
    max_results: int = Field(default=10, ge=1, le=25)


class DriveRecentArgs(ToolArgs):
    shared_with_me: bool = Field(default=False, description="Only files other people shared with the user")
    max_results: int = Field(default=10, ge=1, le=25)


class FileArgs(ToolArgs):
    file_id: str = Field(min_length=1, description="Drive file id (from drive_search or drive_list_recent)")


class DriveDownloadArgs(ToolArgs):
    file_id: str = Field(min_length=1)
    mime_type: str = Field(default="", description="Export type for Google Docs, Sheets and Slides")


class DocArgs(ToolArgs):
    document_id: str = Field(min_length=1, description="Google Doc id (a Drive file id)")


class SheetsFindArgs(ToolArgs):
    query: str = Field(default="", description="e.g. \"name contains 'budget'\"; empty lists recent sheets")
    max_results: int = Field(default=10, ge=1, le=25)


class SheetsReadArgs(ToolArgs):
    spreadsheet_id: str = Field(min_length=1)
    range: str = Field(default="", description="A1 range like 'Sheet1!A1:F50'; empty reads the first sheet")


class TasksListArgs(LocalTimes):
    due_before: datetime | None = Field(
        default=None,
        description=wall_clock("Only tasks due before this time (now = overdue, end of today = due today)"),
    )
    show_completed: bool = False
    max_results: int = Field(default=50, ge=1, le=100)


class TaskRefArgs(ToolArgs):
    task_id: str = Field(min_length=1, description="Task id from tasks_list")


class ContactsSearchArgs(ToolArgs):
    query: str = Field(min_length=2, description="A name, email or phone number")
    max_results: int = Field(default=10, ge=1, le=30)


class MeetTranscriptArgs(ToolArgs):
    conference_record_id: str = Field(
        min_length=1, description="Conference record id from meet_recent, e.g. 'abc-123'")


class FolderCreateArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(default="", description="Parent folder id; empty puts it in My Drive")


class DriveMoveArgs(ToolArgs):
    file_id: str = Field(min_length=1)
    to_folder_id: str = Field(min_length=1, description="Destination folder id")
    from_folder_id: str = Field(default="", description="Current folder id, when known")


class DriveShareArgs(ToolArgs):
    file_id: str = Field(min_length=1)
    email: Email = Field(description="Email of the person to share with")
    role: Literal["reader", "commenter", "writer"] = "reader"


class DocCreateArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(default="", description="The document body as Markdown")


class DocCommentArgs(ToolArgs):
    file_id: str = Field(min_length=1, description="Doc, Sheet or Slides file id")
    content: str = Field(min_length=1, max_length=2000)


class SheetCreateArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=200)


class TaskAddArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=1024)
    notes: str = Field(default="", max_length=8192)
    due: date | None = Field(default=None, description="Due date (Google Tasks keeps the date only)")


class TaskCompleteArgs(ToolArgs):
    task_id: str = Field(min_length=1, description="Task id from tasks_list")


class TaskUpdateArgs(ToolArgs):
    task_id: str = Field(min_length=1, description="Task id from tasks_list")
    title: str | None = Field(default=None, min_length=1, max_length=1024,
                              description="A new title; leave empty to keep the current one")
    notes: str | None = Field(default=None, max_length=8192)
    due: date | None = None
    done: bool | None = Field(default=None,
                              description="True marks it done, False reopens it; empty keeps it as is")


class TaskPatchArgs(BaseModel):
    """Internal: the full PATCH_TASK body. Built by tasks.complete/update from the task as Google has it."""

    task_id: str
    title: str = Field(min_length=1)
    status: Literal["needsAction", "completed"]
    notes: str | None = None
    due: date | None = None


class TaskDeleteArgs(ToolArgs):
    task_id: str = Field(min_length=1)


class DriveUploadArgs(ToolArgs):
    artifact_id: int = Field(description="Id of a file this task produced (deck, report, sheet)")
    folder_id: str = Field(default="", description="Drive folder id; empty puts it in My Drive")


class DriveUploadFileArgs(BaseModel):
    """Internal: a local artifact the provider stages and uploads (built by drive.upload, not a model)."""

    path: str
    name: str
    mime: str
    folder_id: str = ""


CellValue = str | int | float | bool


class DocAppendArgs(ToolArgs):
    document_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20000, description="Plain text added at the end of the doc")


class DocInsertArgs(BaseModel):
    """Internal: text at a document index (docs.append finds the end first)."""

    document_id: str
    text: str
    index: int = Field(ge=1)


class SheetAppendArgs(ToolArgs):
    spreadsheet_id: str = Field(min_length=1)
    range: str = Field(default="Sheet1", description="Sheet name (or table range); the row goes at the end")
    values: list[CellValue] = Field(min_length=1, max_length=50, description="One row of cells")


class SheetUpdateArgs(ToolArgs):
    spreadsheet_id: str = Field(min_length=1)
    sheet_name: str = Field(min_length=1)
    start_cell: str = Field(pattern=r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$", description="Top-left cell, e.g. 'B2'")
    values: list[list[CellValue]] = Field(min_length=1, max_length=200, description="Rows of cells")


# --- Full Workspace read and write: mail triage, calendars, contacts, Slides, Forms, Meet ---------------

MAX_MAIL_IDS = 50


class MailIdsArgs(ToolArgs):
    """Which mail an organising action touches: messages (from mail_search) and/or whole threads."""

    message_ids: list[str] = Field(default_factory=list, max_length=MAX_MAIL_IDS,
                                   description="Message ids from mail_search or mail_read")
    thread_ids: list[str] = Field(default_factory=list, max_length=MAX_MAIL_IDS,
                                  description="Thread ids, to act on every message in a conversation")

    @model_validator(mode="after")
    def _some_mail(self) -> MailIdsArgs:
        if not self.message_ids and not self.thread_ids:
            raise ValueError("give at least one message_id or thread_id (find them with mail_search)")
        return self


class MailLabelArgs(ToolArgs):
    """Add or remove labels on mail; with no ids and no labels it lists the user's labels."""

    message_ids: list[str] = Field(default_factory=list, max_length=MAX_MAIL_IDS)
    thread_ids: list[str] = Field(default_factory=list, max_length=MAX_MAIL_IDS)
    add: list[str] = Field(default_factory=list, max_length=10,
                           description="Label names to add (created when they do not exist), or STARRED, "
                                       "IMPORTANT")
    remove: list[str] = Field(default_factory=list, max_length=10, description="Label names to remove")

    @model_validator(mode="after")
    def _consistent(self) -> MailLabelArgs:
        has_mail = bool(self.message_ids or self.thread_ids)
        changes = bool(self.add or self.remove)
        if changes and not has_mail:
            raise ValueError("give the message_ids or thread_ids to label (find them with mail_search)")
        if has_mail and not changes:
            raise ValueError("say which labels to add or remove; leave everything empty to list labels")
        return self

    @property
    def lists_labels(self) -> bool:
        return not (self.message_ids or self.thread_ids or self.add or self.remove)


class CalendarsArgs(ToolArgs):
    pass


class CalendarGetArgs(ToolArgs):
    event_id: str = Field(min_length=1)
    calendar_id: str = Field(default="primary", description=CALENDAR_ID_HELP)


class CalendarDeleteArgs(ToolArgs):
    event_id: str = Field(min_length=1, description="Event id from calendar_list or calendar_find")
    calendar_id: str = Field(default="primary", description=CALENDAR_ID_HELP)
    notify_guests: bool = Field(
        default=False, description="True only when the user said to tell the guests it is cancelled")


class CalendarRespondArgs(ToolArgs):
    event_id: str = Field(min_length=1, description="Event id of the invite, from calendar_list")
    response: Literal["accepted", "declined", "tentative"]
    calendar_id: str = Field(default="primary", description=CALENDAR_ID_HELP)
    comment: str = Field(default="", max_length=500, description="Optional note the organiser sees")


PERSON_NAME = r"^people/[A-Za-z0-9_-]+$"


class ContactCreateArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=200)
    emails: list[Email] = Field(default_factory=list, max_length=5)
    phones: list[str] = Field(default_factory=list, max_length=5)
    organization: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _something_to_keep(self) -> ContactCreateArgs:
        if not (self.emails or self.phones):
            raise ValueError("a contact needs at least an email or a phone number")
        return self


class ContactUpdateArgs(ToolArgs):
    """Only the fields that change; a list given here replaces that field's current values."""

    resource_name: str = Field(pattern=PERSON_NAME, description="'people/c123' from contacts_search")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    emails: list[Email] | None = None
    phones: list[str] | None = None
    organization: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _changes(self) -> ContactUpdateArgs:
        if all(getattr(self, f) is None for f in ("name", "emails", "phones", "organization", "job_title")):
            raise ValueError("say what to change: name, emails, phones, organization or job_title")
        return self


class SlidesReadArgs(ToolArgs):
    presentation_id: str = Field(min_length=1, description="Slides file id (from drive_search)")


class SlidesCreateArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=200)
    outline: str = Field(
        default="", max_length=30000,
        description="Markdown outline: each '# ' or '## ' heading starts a slide (its title); the lines "
                    "under it are that slide's body")


class FormArgs(ToolArgs):
    form_id: str = Field(min_length=1, description="Google Form id (from drive_search)")


class FormResponsesArgs(ToolArgs):
    form_id: str = Field(min_length=1)
    max_responses: int = Field(default=100, ge=1, le=500)


class MeetRecentArgs(ToolArgs):
    days: int = Field(default=14, ge=1, le=90, description="How far back to look")
    max_results: int = Field(default=10, ge=1, le=25)


# --- helpers --------------------------------------------------------------------------------------


def localize(args: BaseModel, timezone: str) -> BaseModel:
    """The single wall-clock rule (domain.localtime.localize_args); kept under this name for callers."""
    return localize_args(args, timezone)


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
    if args.start and args.duration_minutes:
        lines.append(f"When: {_when(args.start, args.duration_minutes, tz)}")
    if args.attendees:
        lines.append(f"Guests: replaced with exactly {', '.join(args.attendees)} "
                     "(anyone else is removed)")
    elif args.attendees is not None:
        lines.append("Guests: all removed (they get a cancellation)")
    if args.description is not None:
        lines.append(f"Notes: {args.description}")
    lines.append("Everything else stays as it is.")
    return "\n".join(lines)


def _preview_slack(args: SlackSendArgs, tz: str) -> str:
    return f"💬 #{args.channel.lstrip('#')}\n{args.text}"


def _preview_notion(args: NotionCreateArgs, tz: str) -> str:
    return f"🗒️ New Notion page: {args.title}\n{args.content[:400]}"


def _preview_folder(args: FolderCreateArgs, tz: str) -> str:
    return f"📁 New Drive folder: {args.name}"


def _preview_move(args: DriveMoveArgs, tz: str) -> str:
    return f"📁 Move file {args.file_id} into folder {args.to_folder_id}"


def _preview_share(args: DriveShareArgs, tz: str) -> str:
    return f"🔗 Share file {args.file_id} with {args.email} as {args.role}. Google emails them a link."


def _preview_doc(args: DocCreateArgs, tz: str) -> str:
    return f"📄 New Google Doc: {args.title}\n{args.markdown[:400]}"


def _preview_comment(args: DocCommentArgs, tz: str) -> str:
    return f"💬 Comment on file {args.file_id} (everyone with access sees it):\n{args.content}"


def _preview_sheet(args: SheetCreateArgs, tz: str) -> str:
    return f"📊 New Google Sheet: {args.title}"


def _preview_task(args: TaskAddArgs, tz: str) -> str:
    due = f" (due {args.due:%a %d %b})" if args.due else ""
    return f"✅ New task: {args.title}{due}"


def _preview_task_done(args: TaskCompleteArgs, tz: str) -> str:
    return "✅ Mark a task done"  # the prepare step appends the task's real title


def _preview_task_update(args: TaskUpdateArgs, tz: str) -> str:
    lines = ["✅ Update a task"]  # the prepare step appends the task's real title
    if args.title is not None:
        lines.append(f"New title: {args.title}")
    if args.notes is not None:
        lines.append(f"Notes: {args.notes[:400]}")
    if args.due is not None:
        lines.append(f"Due: {args.due:%a %d %b}")
    if args.done is not None:
        lines.append("Mark it done" if args.done else "Mark it not done")
    return "\n".join(lines)


def _preview_task_delete(args: TaskDeleteArgs, tz: str) -> str:
    return f"🗑️ Delete task {args.task_id}"


def _preview_upload(args: DriveUploadArgs, tz: str) -> str:
    where = f"folder {args.folder_id}" if args.folder_id else "My Drive"
    return f"⬆️ Upload file #{args.artifact_id} from this task to {where}"


def _preview_append(args: DocAppendArgs, tz: str) -> str:
    return f"📄 Add to the end of doc {args.document_id}:\n{args.text[:400]}"


def _preview_row(args: SheetAppendArgs, tz: str) -> str:
    row = " | ".join(map(str, args.values))
    return f"📊 Add a row to sheet {args.spreadsheet_id} ({args.range}):\n{row}"


def _preview_cells(args: SheetUpdateArgs, tz: str) -> str:
    cols = max(len(r) for r in args.values)
    head = "\n".join(" | ".join(map(str, r)) for r in args.values[:5])
    return (f"📊 Write {len(args.values)} x {cols} cells at {args.sheet_name}!{args.start_cell.upper()} "
            f"in sheet {args.spreadsheet_id}:\n{head}")


def _preview_meet(args: NoArgs, tz: str) -> str:
    return "📹 New Google Meet link"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _mail_scope(args: MailIdsArgs | MailLabelArgs) -> str:
    parts = []
    if args.message_ids:
        parts.append(_count(len(args.message_ids), "email"))
    if args.thread_ids:
        parts.append(_count(len(args.thread_ids), "conversation"))
    return " and ".join(parts)


def _preview_trash(args: MailIdsArgs, tz: str) -> str:
    return f"🗑️ Move {_mail_scope(args)} to Trash (Gmail empties Trash after 30 days)"


def _preview_untrash(args: MailIdsArgs, tz: str) -> str:
    return f"♻️ Restore {_mail_scope(args)} from Trash"


def _preview_label(args: MailLabelArgs, tz: str) -> str:
    lines = [f"🏷️ Labels on {_mail_scope(args)}"]
    if args.add:
        lines.append(f"Add: {', '.join(args.add)}")
    if args.remove:
        lines.append(f"Remove: {', '.join(args.remove)}")
    return "\n".join(lines)


def _label_risk(args: BaseModel) -> RiskClass:
    """Listing labels reads; changing them writes."""
    return RiskClass.READ if getattr(args, "lists_labels", False) else RiskClass.WRITE_SELF


def _preview_delete_event(args: CalendarDeleteArgs, tz: str) -> str:
    who = "Guests are told it is cancelled." if args.notify_guests else "Guests are not notified."
    return f"🗑️ Delete calendar event {args.event_id}\n{who}"


def _preview_respond(args: CalendarRespondArgs, tz: str) -> str:
    word = {"accepted": "Accept", "declined": "Decline", "tentative": "Reply maybe to"}[args.response]
    note = f"\nNote: {args.comment}" if args.comment else ""
    return f"📅 {word} invite {args.event_id}. The organiser is told.{note}"


def _preview_contact(args: ContactCreateArgs, tz: str) -> str:
    bits = [*args.emails, *args.phones, args.organization]
    return f"👤 New contact: {args.name}\n{' | '.join(b for b in bits if b)}"


def _preview_contact_update(args: ContactUpdateArgs, tz: str) -> str:
    lines = [f"👤 Update contact {args.resource_name}"]
    for label, value in (("Name", args.name), ("Emails", args.emails), ("Phones", args.phones),
                         ("Company", args.organization), ("Title", args.job_title)):
        if value is not None:
            lines.append(f"{label}: {', '.join(value) if isinstance(value, list) else value}")
    return "\n".join(lines)


def _preview_slides(args: SlidesCreateArgs, tz: str) -> str:
    return f"🖼️ New Google Slides deck: {args.title}\n{args.outline[:400]}"


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
    identity: tuple[str, ...] = ()  # see MavisTool.identity
    target: tuple[str, ...] = ()  # see MavisTool.target
    action_time: str | None = None  # see MavisTool.action_time

    def risk_for(self, args: BaseModel) -> RiskClass:
        return self.risk_fn(args) if self.risk_fn else self.risk


def _a(*names: str) -> frozenset[str]:
    return frozenset(names)


_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("mail.search", Capability.GMAIL,
               "Search or find emails in the user's Gmail. Returns message ids, thread ids, senders, "
               "subjects, dates and short previews. Use it first to get the ids that archive, mark read, "
               "label or trash need.",
               MailSearchArgs, RiskClass.READ, _a("inbox", "conversation"), priority=58),
    ActionSpec("mail.read", Capability.GMAIL,
               "Read one email in full by message id (from mail_search): headers and body text, for what "
               "it says, its details or key points.",
               MailReadArgs, RiskClass.READ, _a("inbox", "conversation")),
    ActionSpec("mail.thread", Capability.GMAIL, "Read every message in an email thread.",
               MailThreadArgs, RiskClass.READ, _a("inbox")),
    ActionSpec("mail.draft", Capability.GMAIL, "Create a Gmail draft (not sent).",
               MailComposeArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation"), preview=_preview_draft),
    ActionSpec("mail.send", Capability.GMAIL, "Send an email. The user is asked to approve first.",
               MailComposeArgs, RiskClass.OUTWARD, _a("inbox", "conversation"), preview=_preview_mail,
               identity=("to", "cc", "subject", "body"), target=("to", "subject")),
    ActionSpec("mail.reply", Capability.GMAIL,
               "Reply on an existing thread. The user is asked to approve first.",
               MailReplyArgs, RiskClass.OUTWARD, _a("inbox", "conversation"), preview=_preview_reply),
    ActionSpec("calendar.list", Capability.CALENDAR,
               "List calendar events in a time window, on the user's own calendar or another calendar "
               "(calendar_id from calendar_calendars). Event ids here are for deleting or answering invites.",
               CalendarListArgs, RiskClass.READ, _a("calendar", "conversation"), priority=56),
    ActionSpec("calendar.find", Capability.CALENDAR,
               "Find calendar events matching text, on the user's own or another calendar.",
               CalendarFindArgs, RiskClass.READ, _a("calendar", "conversation")),
    ActionSpec("calendar.free_slots", Capability.CALENDAR, "Find free time between two times.",
               CalendarSlotsArgs, RiskClass.READ, _a("calendar", "conversation")),
    ActionSpec("calendar.create_event", Capability.CALENDAR,
               "Create a calendar event or meeting. With guests, invites are sent after the user approves.",
               CalendarCreateArgs, RiskClass.WRITE_SELF, _a("calendar", "conversation"),
               risk_fn=_attendee_risk, preview=_preview_create,
               identity=("start", "duration_minutes", "attendees", "summary"),
               target=("start", "attendees"), action_time="start"),
    ActionSpec("calendar.update_event", Capability.CALENDAR,
               "Change an event. The user is asked to approve first (guests may be notified).",
               CalendarUpdateArgs, RiskClass.WRITE_SELF, _a("calendar"),
               risk_fn=_update_risk, preview=_preview_update,
               # identity: the event plus every changed field, which is all of its arguments
               target=("event_id",), action_time="start"),

    # Mail organising (gmail.modify). Archive, read state and labels are reversible and touch only the
    # user's own mailbox: WRITE_SELF. Trash is DESTRUCTIVE (the user approves first); restoring is not.
    ActionSpec("mail.archive", Capability.GMAIL,
               "Archive emails: take them out of the inbox without deleting them (they stay searchable). "
               "Pass message_ids or thread_ids from mail_search. Use for 'archive this', 'clear these "
               "from my inbox'.",
               MailIdsArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation")),
    ActionSpec("mail.mark_read", Capability.GMAIL,
               "Mark emails as read (clears unread). Pass message_ids or thread_ids from mail_search.",
               MailIdsArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation")),
    ActionSpec("mail.mark_unread", Capability.GMAIL,
               "Mark emails as unread so they stand out again. Pass message_ids or thread_ids from "
               "mail_search.",
               MailIdsArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation")),
    ActionSpec("mail.label", Capability.GMAIL,
               "Label or tag emails: add or remove labels by name (an unknown name makes the label; STARRED "
               "and IMPORTANT work too) on message_ids or thread_ids. With no ids and no labels it lists "
               "the user's labels.",
               MailLabelArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation"), risk_fn=_label_risk,
               preview=_preview_label),
    ActionSpec("mail.trash", Capability.GMAIL,
               "Delete emails by putting them in Trash (emptied after 30 days). The user approves "
               "first. Pass message_ids or thread_ids from mail_search.",
               MailIdsArgs, RiskClass.DESTRUCTIVE, _a("inbox", "conversation"), preview=_preview_trash),
    ActionSpec("mail.untrash", Capability.GMAIL,
               "Restore emails from Trash back to the mailbox (undo a delete). Pass their ids.",
               MailIdsArgs, RiskClass.WRITE_SELF, _a("inbox", "conversation"), preview=_preview_untrash),
    ActionSpec("calendar.calendars", Capability.CALENDAR,
               "List every calendar the user can see: their own plus shared, team, subscribed and holiday "
               "calendars, with ids and access levels (use an id as calendar_id elsewhere).",
               CalendarsArgs, RiskClass.READ, _a("calendar", "conversation")),
    ActionSpec("calendar.delete_event", Capability.CALENDAR,
               "Delete or cancel a calendar event by event id (from calendar_list or calendar_find). The "
               "user approves first. Set notify_guests only when they said to tell the guests.",
               CalendarDeleteArgs, RiskClass.DESTRUCTIVE, _a("calendar", "conversation"),
               preview=_preview_delete_event, target=("event_id",)),
    ActionSpec("calendar.respond", Capability.CALENDAR,
               "Reply to a calendar invite: accept, decline or say maybe (tentative). The organiser is "
               "told, so the user approves first.",
               CalendarRespondArgs, RiskClass.OUTWARD, _a("calendar", "conversation"),
               preview=_preview_respond, target=("event_id",)),
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
_WORKERS = _a("spawn")
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
               "Show your Google Tasks to-do list (the user's own to-dos, not Mavis's background jobs): "
               "open tasks and due dates. Set due_before to now for overdue tasks, or to the end of today "
               "for what is due today.",
               TasksListArgs, RiskClass.READ, _CHAT, priority=57),
    ActionSpec("contacts.search", Capability.CONTACTS,
               "Look up a person: name to email address and phone number. Searches the user's Google "
               "Contacts, people they have emailed before, and the company directory when the account "
               "has one. Saved contacts show the id contacts_update needs.",
               ContactsSearchArgs, RiskClass.READ, _CHAT),
    ActionSpec("meet.transcript", Capability.MEET,
               "Read what was said in a Google Meet call: the transcript text by speaker, plus the "
               "transcript Doc. Needs a conference_record_id from meet_recent. Says plainly when the call "
               "has no transcript (personal Google accounts and calls without transcription have none).",
               MeetTranscriptArgs, RiskClass.READ, _CHAT),
    ActionSpec("meet.recent", Capability.MEET,
               "List recent Google Meet calls: when they ran, who joined, and the conference_record_id "
               "meet_transcript needs. Use for 'my last meeting', 'what was said on the call'.",
               MeetRecentArgs, RiskClass.READ, _CHAT),
    ActionSpec("slides.read", Capability.DOCS,
               "Read a Google Slides presentation: the text on each slide, in order. Use for 'what's in "
               "my slides', 'summarise this deck'. Needs the presentation id (from drive_search).",
               SlidesReadArgs, RiskClass.READ, _CHAT),
    ActionSpec("forms.read", Capability.DOCS,
               "Read a Google Form: its title and questions (with their choices).",
               FormArgs, RiskClass.READ, _CHAT),
    ActionSpec("forms.responses", Capability.DOCS,
               "Summarise the answers people gave to a Google Form (survey, sign-up): how many, how each "
               "choice question split, and sample text answers. Capped.",
               FormResponsesArgs, RiskClass.READ, _CHAT),
    # writes: every Google WRITE_SELF needs approval after untrusted output in the run (spec 4.3)
    ActionSpec("drive.create_folder", Capability.DRIVE, "Create a folder in the user's Google Drive.",
               FolderCreateArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_folder, taint_approve=True),
    ActionSpec("drive.move", Capability.DRIVE, "Move a Drive file into another folder.",
               DriveMoveArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_move, taint_approve=True),
    ActionSpec("drive.share", Capability.DRIVE,
               "Share a Drive file with a person (reader, commenter or writer). The user approves first.",
               DriveShareArgs, RiskClass.OUTWARD, _WORKERS, preview=_preview_share),
    ActionSpec("docs.create", Capability.DOCS, "Create a new Google Doc from a title and Markdown text.",
               DocCreateArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_doc, taint_approve=True),
    ActionSpec("docs.comment", Capability.DOCS,
               "Add a comment to a Doc, Sheet or Slides file. Collaborators see it; the user approves first.",
               DocCommentArgs, RiskClass.OUTWARD, _WORKERS, preview=_preview_comment),
    ActionSpec("sheets.create", Capability.SHEETS, "Create a new, empty Google Sheet.",
               SheetCreateArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_sheet, taint_approve=True),
    ActionSpec("tasks.add", Capability.TASKS, "Add a to-do to your Google Tasks to-do list.",
               TaskAddArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_task, taint_approve=True),
    ActionSpec("tasks.complete", Capability.TASKS,
               "Mark a to-do in your Google Tasks to-do list as done (task_id from tasks_list).",
               TaskCompleteArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_task_done, taint_approve=True),
    ActionSpec("tasks.update", Capability.TASKS,
               "Change a to-do in your Google Tasks to-do list: title, notes or due date.",
               TaskUpdateArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_task_update,
               taint_approve=True),
    ActionSpec("tasks.delete", Capability.TASKS,
               "Delete a to-do from your Google Tasks to-do list. The user approves first.",
               TaskDeleteArgs, RiskClass.DESTRUCTIVE, _WORKERS, preview=_preview_task_delete),
    ActionSpec("drive.upload", Capability.DRIVE,
               "Upload a file this task produced (deck, report, spreadsheet) to the user's Google Drive.",
               DriveUploadArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_upload, taint_approve=True),
    ActionSpec("docs.append", Capability.DOCS,
               "Add text to the end of an existing Google Doc. The user approves first when the doc is "
               "shared or not theirs.",
               DocAppendArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_append, taint_approve=True),
    ActionSpec("sheets.append_row", Capability.SHEETS,
               "Add one row at the end of a Google Sheet (for example an expense in a budget sheet).",
               SheetAppendArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_row, taint_approve=True),
    ActionSpec("sheets.update_range", Capability.SHEETS,
               "Write cells into a Google Sheet starting at a cell, overwriting what is there.",
               SheetUpdateArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_cells, taint_approve=True),
    ActionSpec("slides.create", Capability.DOCS,
               "Make or build a Google Slides deck (presentation, slideshow) from a title and a Markdown "
               "outline: each heading is a slide title, the lines under it the slide's body.",
               SlidesCreateArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_slides, taint_approve=True),
    ActionSpec("contacts.create", Capability.CONTACTS,
               "Save a new person in the user's Google Contacts (name plus an email or phone).",
               ContactCreateArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_contact, taint_approve=True),
    ActionSpec("contacts.update", Capability.CONTACTS,
               "Change a saved Google Contact: name, emails, phones, company or title (id from "
               "contacts_search). A list you give replaces the old one.",
               ContactUpdateArgs, RiskClass.WRITE_SELF, _CHAT, preview=_preview_contact_update,
               taint_approve=True),
    ActionSpec("meet.create", Capability.MEET, "Create a standalone Google Meet link.",
               NoArgs, RiskClass.WRITE_SELF, _WORKERS, preview=_preview_meet, taint_approve=True),
    # internal: file metadata and permissions (risk escalation, allowlist), downloads, task lookup, profile
    ActionSpec("drive.meta", Capability.DRIVE, "File name and type.", FileArgs, RiskClass.READ, _INTERNAL),
    ActionSpec("drive.permissions", Capability.DRIVE, "Who can access a file.", FileArgs, RiskClass.READ,
               _INTERNAL),
    ActionSpec("drive.download", Capability.DRIVE, "Export or download a file.", DriveDownloadArgs,
               RiskClass.READ, _INTERNAL),
    ActionSpec("tasks.get", Capability.TASKS, "One task by id.", TaskRefArgs, RiskClass.READ, _INTERNAL),
    ActionSpec("drive.upload_file", Capability.DRIVE, "Upload a staged local file.", DriveUploadFileArgs,
               RiskClass.WRITE_SELF, _INTERNAL),
    ActionSpec("docs.insert_text", Capability.DOCS, "Insert text at an index.", DocInsertArgs,
               RiskClass.WRITE_SELF, _INTERNAL),
    ActionSpec("tasks.patch", Capability.TASKS, "Write a task's title, status, notes and due date.",
               TaskPatchArgs, RiskClass.WRITE_SELF, _INTERNAL),
    ActionSpec("calendar.get", Capability.CALENDAR, "One calendar event by id.", CalendarGetArgs,
               RiskClass.READ, _INTERNAL),
    ActionSpec("mail.profile", Capability.GMAIL, "The user's own email address.", NoArgs, RiskClass.READ,
               _INTERNAL),
    ActionSpec("contacts.list", Capability.CONTACTS, "The user's contacts (emails only).", NoArgs,
               RiskClass.READ, _INTERNAL),
)

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in (*_SPECS, *_WORKSPACE_SPECS)}

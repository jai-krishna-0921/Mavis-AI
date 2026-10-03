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
from mavis.domain.errors import NeedsUserDetail
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
    """Only the fields that change; everything else on the event stays as it is."""

    event_id: str
    summary: str | None = None
    start: datetime | None = Field(
        default=None,
        description="New start time. Moving an event needs duration_minutes too (its current length "
                    "if that is not changing)",
    )
    duration_minutes: int | None = Field(
        default=None, ge=5, le=1440,
        description="Length in minutes; goes with start (pass the event's current start to change only "
                    "the length)",
    )
    attendees: list[str] | None = None
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


class FolderCreateArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(default="", description="Parent folder id; empty puts it in My Drive")


class DriveMoveArgs(BaseModel):
    file_id: str = Field(min_length=1)
    to_folder_id: str = Field(min_length=1, description="Destination folder id")
    from_folder_id: str = Field(default="", description="Current folder id, when known")


class DriveShareArgs(BaseModel):
    file_id: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", description="Email of the person to share with")
    role: Literal["reader", "commenter", "writer"] = "reader"


class DocCreateArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(default="", description="The document body as Markdown")


class DocCommentArgs(BaseModel):
    file_id: str = Field(min_length=1, description="Doc, Sheet or Slides file id")
    content: str = Field(min_length=1, max_length=2000)


class SheetCreateArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class TaskAddArgs(BaseModel):
    title: str = Field(min_length=1, max_length=1024)
    notes: str = Field(default="", max_length=8192)
    due: date | None = Field(default=None, description="Due date (Google Tasks keeps the date only)")


class TaskCompleteArgs(BaseModel):
    task_id: str = Field(min_length=1, description="Task id from tasks_list")


class TaskUpdateArgs(BaseModel):
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


class TaskDeleteArgs(BaseModel):
    task_id: str = Field(min_length=1)


class DriveUploadArgs(BaseModel):
    artifact_id: int = Field(description="Id of a file this task produced (deck, report, sheet)")
    folder_id: str = Field(default="", description="Drive folder id; empty puts it in My Drive")


class DriveUploadFileArgs(BaseModel):
    """Internal: a local artifact the provider stages and uploads (built by drive.upload, not a model)."""

    path: str
    name: str
    mime: str
    folder_id: str = ""


CellValue = str | int | float | bool


class DocAppendArgs(BaseModel):
    document_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20000, description="Plain text added at the end of the doc")


class DocInsertArgs(BaseModel):
    """Internal: text at a document index (docs.append finds the end first)."""

    document_id: str
    text: str
    index: int = Field(ge=1)


class SheetAppendArgs(BaseModel):
    spreadsheet_id: str = Field(min_length=1)
    range: str = Field(default="Sheet1", description="Sheet name (or table range); the row goes at the end")
    values: list[CellValue] = Field(min_length=1, max_length=50, description="One row of cells")


class SheetUpdateArgs(BaseModel):
    spreadsheet_id: str = Field(min_length=1)
    sheet_name: str = Field(min_length=1)
    start_cell: str = Field(pattern=r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$", description="Top-left cell, e.g. 'B2'")
    values: list[list[CellValue]] = Field(min_length=1, max_length=200, description="Rows of cells")


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
               "Search or find emails in the user's Gmail. Returns message ids, senders, subjects, dates and "
               "short previews.",
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
    ActionSpec("calendar.list", Capability.CALENDAR, "List calendar events in a time window.",
               CalendarListArgs, RiskClass.READ, _a("calendar", "conversation"), priority=56),
    ActionSpec("calendar.find", Capability.CALENDAR, "Find calendar events matching text.",
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
               "Look up a person in the user's Google Contacts: name to email address and phone number.",
               ContactsSearchArgs, RiskClass.READ, _CHAT),
    ActionSpec("meet.transcript", Capability.MEET,
               "List the transcripts of a Google Meet conference; each transcript is a Google Doc to read "
               "with docs_read.",
               MeetTranscriptArgs, RiskClass.READ, _CHAT),
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
    ActionSpec("mail.profile", Capability.GMAIL, "The user's own email address.", NoArgs, RiskClass.READ,
               _INTERNAL),
    ActionSpec("contacts.list", Capability.CONTACTS, "The user's contacts (emails only).", NoArgs,
               RiskClass.READ, _INTERNAL),
)

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in (*_SPECS, *_WORKSPACE_SPECS)}

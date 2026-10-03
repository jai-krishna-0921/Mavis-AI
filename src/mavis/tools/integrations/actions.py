"""Mavis action catalog: provider-agnostic names, argument models, risk and previews.

Adding a provider means mapping these names; adding an action means one entry here plus a mapping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from mavis.domain.errors import NeedsUserDetail
from mavis.domain.policy import Capability, RiskClass

INTEGRATION_CAPABILITIES: tuple[Capability, ...] = (
    Capability.GMAIL, Capability.CALENDAR, Capability.SLACK, Capability.NOTION,
)
DISPLAY_NAMES: dict[Capability, str] = {
    Capability.GMAIL: "Gmail", Capability.CALENDAR: "Google Calendar",
    Capability.SLACK: "Slack", Capability.NOTION: "Notion",
}
BRANDS: dict[Capability, str] = {
    Capability.GMAIL: "Google", Capability.CALENDAR: "Google",
    Capability.SLACK: "Slack", Capability.NOTION: "Notion",
}
CAPABILITY_PURPOSE: dict[Capability, str] = {
    Capability.GMAIL: "check and handle your email",
    Capability.CALENDAR: "work with your calendar",
    Capability.SLACK: "work with your Slack",
    Capability.NOTION: "work with your Notion pages",
}


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

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in _SPECS}

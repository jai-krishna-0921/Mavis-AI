# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Google OAuth scope groups and the per-action scope table (data only)."""

from __future__ import annotations

_AUTH = "https://www.googleapis.com/auth/"
_MAIL_ALL = "https://mail.google.com/"


def _any(*names: str) -> frozenset[str]:
    return frozenset(n if n.startswith("https://") else _AUTH + n for n in names)


_MAIL_READ = _any("gmail.readonly", "gmail.modify", _MAIL_ALL)
_MAIL_SEND = _any("gmail.send", "gmail.compose", "gmail.modify", _MAIL_ALL)
_MAIL_DRAFT = _any("gmail.compose", "gmail.modify", _MAIL_ALL)
_MAIL_MODIFY = _any("gmail.modify", _MAIL_ALL)  # archive, mark read, label, trash
_CAL_READ = _any("calendar.readonly", "calendar.events.readonly", "calendar.events", "calendar")
_CAL_FREEBUSY = _any("calendar.freebusy", "calendar.readonly", "calendar.events.readonly",
                     "calendar.events", "calendar")
_CAL_WRITE = _any("calendar.events", "calendar")
_CAL_LIST = _any("calendar.readonly", "calendar", "calendar.calendarlist.readonly", "calendar.calendarlist")
_DRIVE_READ = _any("drive.readonly", "drive")
_DRIVE_META = _any("drive.readonly", "drive.metadata.readonly", "drive.metadata", "drive")
_DOCS_READ = _any("documents.readonly", "documents", "drive.readonly", "drive")
_SHEETS_READ = _any("spreadsheets.readonly", "spreadsheets", "drive.readonly", "drive")
_CONTACTS_READ = _any("contacts.readonly", "contacts")
_CONTACTS_WRITE = _any("contacts")
_SLIDES_READ = _any("presentations.readonly", "presentations", "drive.readonly", "drive")
_SLIDES_WRITE = _any("presentations", "drive")
_FORMS_BODY = _any("forms.body.readonly", "forms.body", "drive.readonly", "drive")
_FORMS_RESPONSES = _any("forms.responses.readonly", "drive.readonly", "drive")
# Writes. drive.file is deliberately absent: it only reaches files this app created, and these actions work
# on the user's existing files (comment, share, move, append, update cells), so the full drive scope counts.
_DRIVE_WRITE = _any("drive")
_DOCS_WRITE = _any("documents", "drive")
_SHEETS_WRITE = _any("spreadsheets", "drive")
_TASKS_READ = _any("tasks", "tasks.readonly")
_TASKS_WRITE = _any("tasks")
_MEET_CREATE = _any("meetings.space.created")
_MEET_READ = _any("meetings.space.readonly", "meetings.space.created")

# What each natively handled Google action needs: a tuple of any-of groups, and every group must be met
# (a reply reads the thread and then sends). An action with no entry is never run natively. Google lets a
# user untick scopes on the consent screen, so the grant's scopes, not what we asked for, decide.
ACTION_SCOPES: dict[str, tuple[frozenset[str], ...]] = {
    "mail.search": (_MAIL_READ,), "mail.read": (_MAIL_READ,), "mail.thread": (_MAIL_READ,),
    "mail.profile": (_MAIL_READ,),
    "mail.send": (_MAIL_SEND,), "mail.draft": (_MAIL_DRAFT,), "mail.reply": (_MAIL_READ, _MAIL_SEND),
    "calendar.list": (_CAL_READ,), "calendar.find": (_CAL_READ,), "calendar.free_slots": (_CAL_FREEBUSY,),
    "calendar.create_event": (_CAL_WRITE,), "calendar.update_event": (_CAL_WRITE,),
    "drive.search": (_DRIVE_META,), "drive.list_recent": (_DRIVE_META,), "drive.meta": (_DRIVE_META,),
    "drive.permissions": (_DRIVE_META,), "drive.read": (_DRIVE_READ,), "drive.download": (_DRIVE_READ,),
    "drive.export": (_DRIVE_READ,), "drive.export_file": (_DRIVE_READ,),
    "docs.read": (_DOCS_READ,), "sheets.find": (_DRIVE_META,), "sheets.read": (_SHEETS_READ,),
    "contacts.search": (_CONTACTS_READ,), "contacts.list": (_CONTACTS_READ,),
    "drive.create_folder": (_DRIVE_WRITE,), "drive.move": (_DRIVE_WRITE,), "drive.share": (_DRIVE_WRITE,),
    "drive.upload_file": (_DRIVE_WRITE,), "docs.comment": (_DRIVE_WRITE,),
    "docs.create": (_DOCS_WRITE,), "docs.insert_text": (_DOCS_WRITE,),
    "sheets.create": (_SHEETS_WRITE,), "sheets.append_row": (_SHEETS_WRITE,),
    "sheets.update_range": (_SHEETS_WRITE,),
    "tasks.list": (_TASKS_READ,), "tasks.get": (_TASKS_READ,), "tasks.add": (_TASKS_WRITE,),
    "tasks.patch": (_TASKS_WRITE,), "tasks.delete": (_TASKS_WRITE,),
    "meet.create": (_MEET_CREATE,), "meet.transcript": (_MEET_READ,), "meet.recent": (_MEET_READ,),
    "mail.archive": (_MAIL_MODIFY,), "mail.mark_read": (_MAIL_MODIFY,), "mail.mark_unread": (_MAIL_MODIFY,),
    "mail.label": (_MAIL_MODIFY,), "mail.trash": (_MAIL_MODIFY,), "mail.untrash": (_MAIL_MODIFY,),
    # The calendar list needs a calendar-list scope; an event delete or an RSVP is an events call, so a
    # grant made with calendar.events keeps working for them.
    "calendar.calendars": (_CAL_LIST,), "calendar.get": (_CAL_READ,), "calendar.delete_event": (_CAL_WRITE,),
    "calendar.respond": (_CAL_WRITE,),
    "contacts.create": (_CONTACTS_WRITE,), "contacts.update": (_CONTACTS_WRITE,),
    "slides.read": (_SLIDES_READ,), "slides.create": (_SLIDES_WRITE,),
    "forms.read": (_FORMS_BODY,), "forms.responses": (_FORMS_BODY, _FORMS_RESPONSES),
}


# What a person can still do, in the words used when they left some boxes unticked on Google's consent screen.
GOOGLE_FEATURES: tuple[tuple[str, str], ...] = (
    ("read your mail", "mail.search"), ("send mail", "mail.send"),
    ("see your calendar", "calendar.list"), ("add calendar events", "calendar.create_event"),
    ("browse Drive", "drive.search"), ("change Drive files", "drive.move"), ("read Docs", "docs.read"),
    ("read Sheets", "sheets.read"), ("see Tasks", "tasks.list"), ("see Contacts", "contacts.search"),
    ("make Meet links", "meet.create"),
    ("organise mail (archive, label, trash)", "mail.archive"),
    ("list all your calendars", "calendar.calendars"),
    ("delete calendar events", "calendar.delete_event"), ("edit Contacts", "contacts.create"),
    ("read and make Slides", "slides.create"), ("read Forms", "forms.responses"),
)

"""Google Workspace signals (spec 2026-10-03 section 5): normalization and the deterministic scoring table.

Pure: no I/O. Titles and previews are third-party text; titles keep file names (extensions included) but
lose links and markup, previews go through the attention sanitizer. The lure check reads the raw title.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from mavis.attention.sanitize import clean
from mavis.attention.schema import Verdict
from mavis.domain.loops import Loop, LoopKind
from mavis.tools.integrations.mail_render import strip_urls

PREVIEW_CHARS = 280
ASK_AFTER_DAYS = 2
_MARKUP = re.compile(r"[<>`]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LURE = re.compile(
    r"\.(exe|scr|bat|cmd|js|vbs|msi|apk|jar|iso|lnk|html?)\b"
    r"|\b(password|passcode|log-?in|sign-?in|verify|verification|invoice|payment|wire|bank|urgent|account)\b",
    re.I,
)
# Words that say nothing about which file: generic file nouns and short function words ("to", "be").
_STOP = frozenset(
    "the and for from with your you file doc docs deck sheet send sent share "
    "a an to of in on at by or is it be as my me we us our are was will can this that please".split()
)
_WORD = re.compile(r"[a-z0-9]+")


class SignalKind(StrEnum):
    FILE_SHARED = "file_shared"
    COMMENT = "comment"
    TASK_DUE = "task_due"
    TASK_OVERDUE = "task_overdue"


SOURCE_OF: dict[SignalKind, str] = {
    SignalKind.FILE_SHARED: "drive", SignalKind.COMMENT: "docs",
    SignalKind.TASK_DUE: "tasks", SignalKind.TASK_OVERDUE: "tasks",
}


class Signal(BaseModel):
    kind: SignalKind
    object_id: str
    event_id: str
    actor: str = ""  # email when known (shares), display name for comments
    actor_known: bool = False
    object_title: str = ""
    due: date | None = None
    mentions_you: bool = False
    owned_by_me: bool = False
    preview: str = ""  # sanitized, untrusted
    overdue_days: int = 0

    @property
    def source(self) -> str:
        return SOURCE_OF[self.kind]

    @property
    def message_id(self) -> str:
        return f"{self.source}:{self.object_id}:{self.event_id}"[:200]


def safe_title(text: object, limit: int = 120) -> str:
    flat = " ".join(_MARKUP.sub("", _CONTROL.sub(" ", strip_urls(str(text or "")))).split())
    return flat[:limit].rstrip()


def is_lure(raw_title: str) -> bool:
    return LURE.search(raw_title or "") is not None


def mentions(text: str, email: str) -> bool:
    """The comment names the user: their address, or an @handle of their local part as a whole word
    (@jai is not @jaiswal, and xjai@example.com is not jai@example.com)."""
    email = (email or "").strip().lower()
    if not email:
        return False
    low = (text or "").lower()
    address = rf"(?<![\w.-]){re.escape(email)}(?![\w-]|\.\w)"
    handle = rf"@{re.escape(email.split('@')[0])}(?![\w@-])"
    return re.search(address, low) is not None or re.search(handle, low) is not None


def _email(person: object) -> str:
    if not isinstance(person, dict):
        return ""
    return str(person.get("emailAddress") or person.get("displayName") or "").strip().lower()


def shared_file_signal(f: dict) -> Signal | None:
    """A Drive file resource from the shared-with-me listing (fields include owners, sharingUser)."""
    owners = [o for o in f.get("owners") or [] if isinstance(o, dict)]
    when = str(f.get("sharedWithMeTime") or "")
    if any(o.get("me") for o in owners) or not f.get("id") or not when:
        return None
    sharer = f.get("sharingUser") or (owners[0] if owners else {})
    return Signal(kind=SignalKind.FILE_SHARED, object_id=str(f["id"]), event_id=f"shared-{when[:19]}",
                  actor=_email(sharer), object_title=safe_title(f.get("name") or "(untitled)"),
                  preview="")


def comment_signal(p: dict, *, me: str = "") -> Signal | None:
    """GOOGLESUPER_COMMENT_ADDED_TRIGGER payload: comment_id, comment_text, commenter, file_id."""
    commenter = p.get("commenter") if isinstance(p.get("commenter"), dict) else {}
    if commenter.get("me") or not p.get("file_id") or not p.get("comment_id"):
        return None
    text = str(p.get("comment_text") or "")
    return Signal(kind=SignalKind.COMMENT, object_id=str(p["file_id"]), event_id=str(p["comment_id"]),
                  actor=safe_title(commenter.get("displayName") or "", 60), mentions_you=mentions(text, me),
                  preview=clean(text, PREVIEW_CHARS))


def _due_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def task_signal(t: dict, today: date) -> Signal | None:
    """A Google Task: due today or overdue becomes a signal; done, deleted, undated or future tasks don't."""
    if t.get("status") == "completed" or t.get("deleted") or not t.get("id"):
        return None
    due = _due_date(t.get("due"))
    if due is None or due > today:
        return None
    overdue = (today - due).days
    kind = SignalKind.TASK_OVERDUE if overdue > 0 else SignalKind.TASK_DUE
    return Signal(kind=kind, object_id=str(t["id"]), event_id=f"{kind.value}-{today.isoformat()}",
                  object_title=safe_title(t.get("title") or "(untitled)"), due=due, overdue_days=overdue,
                  preview=clean(t.get("notes") or "", 120))


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if len(w) >= 2 and w not in _STOP}


def match_loop(title: str, loops: list[Loop]) -> int | None:
    """The loop the user is waiting on (or committed to) that this file title most plausibly fulfils:
    the most shared words wins, then the closer spelling."""
    words = _tokens(title)
    best: tuple[int, float] | None = None
    found: int | None = None
    for loop in loops:
        if loop.kind not in (LoopKind.WAITING_ON, LoopKind.COMMITMENT):
            continue
        shared = len(words & _tokens(loop.title))
        ratio = difflib.SequenceMatcher(None, title.casefold(), loop.title.casefold()).ratio()
        if (shared >= 2 or ratio >= 0.75) and (best is None or (shared, ratio) > best):
            best, found = (shared, ratio), loop.id
    return found


@dataclass(frozen=True)
class WorkspaceDecision:
    verdict: Verdict
    urgency: int = 0
    reason: str = ""
    security: bool = False
    close_loop: int | None = None


_DOWN = {Verdict.NOTIFY: Verdict.BRIEF, Verdict.BRIEF: Verdict.LOG}


def decide(
    s: Signal, *, loop_id: int | None = None, muted: bool = False, asked: bool = False
) -> WorkspaceDecision:
    """Spec 5.2 scoring table. `muted`: the user said "not useful" for this kind and actor (one step down,
    never for a security line). `asked`: the keep-or-drop question already went out for this task."""
    if s.kind is SignalKind.FILE_SHARED:
        if s.actor_known and loop_id is not None:
            return WorkspaceDecision(Verdict.NOTIFY, 3, "matches something you were waiting for",
                                     close_loop=loop_id)
        if s.actor_known:
            d = WorkspaceDecision(Verdict.BRIEF, 0, "shared by someone you know")
        elif is_lure(s.object_title):
            return WorkspaceDecision(
                Verdict.BRIEF, 0, "shared by someone new, and the name looks like a lure", security=True
            )
        else:
            d = WorkspaceDecision(Verdict.LOG, 0, "shared by someone you have not emailed")
    elif s.kind is SignalKind.COMMENT:
        if s.owned_by_me or s.mentions_you:
            why = "a comment on your doc" if s.owned_by_me else "you were mentioned"
            d = WorkspaceDecision(Verdict.NOTIFY, 3, why)
        else:
            d = WorkspaceDecision(Verdict.BRIEF, 0, "a comment on a doc you follow")
    elif s.kind is SignalKind.TASK_OVERDUE and s.overdue_days >= ASK_AFTER_DAYS and not asked:
        return WorkspaceDecision(Verdict.ASK, 3, f"overdue for {s.overdue_days} days")
    else:
        d = WorkspaceDecision(Verdict.BRIEF, 0, "due today" if s.kind is SignalKind.TASK_DUE else "overdue")
    if muted:
        return WorkspaceDecision(_DOWN.get(d.verdict, d.verdict), 0, d.reason)
    return d

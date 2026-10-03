"""Per-task facts the Workspace write actions rely on (spec 2026-10-03 sections 4.1 and 4.3).

`record_created` / `created_by`: files and tasks a task made itself, the second source of the tainted-task
allowlist. Kept in process memory on purpose, like web.py's search URLs: a restart forgets them, which
fails closed (the task can no longer touch them without the user naming them).

Risk escalation (MavisTool.prepare, run before any write and before the approval check): a write to a file
someone else owns or anyone else can see is OUTWARD, overwriting OVERWRITE_LIMIT or more filled cells is
DESTRUCTIVE, and a lookup that fails or answers in a shape we do not recognise fails closed. Approval notes
carry only what these lookups verified. `my_email` and `file_meta` are the one place that resolves the
user's address and a file's ownership (WorkspaceIntake reuses them through the provider/cache arguments).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mavis.domain.errors import ActionFailed
from mavis.domain.policy import RiskClass
from mavis.store.repo import users
from mavis.tools.integrations.actions import FileArgs, NoArgs, SheetsReadArgs, SheetUpdateArgs, TaskRefArgs
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import extract_list, pick
from mavis.tools.integrations.tools import action_data
from mavis.tools.integrations.workspace_render import ID_KEYS, kind_of, one_line, sheet_rows
from mavis.tools.registry import Prepared, PrepareFn, ToolContext, current_run

MAX_TRACKED_TASKS = 200
_created: OrderedDict[int, set[str]] = OrderedDict()

STATE_KEY = "workspace"  # users.state["workspace"]: email, contacts, cursors, muted (shared with attention)
OVERWRITE_LIMIT = 20
UNKNOWN_NOTE = "I couldn't check who can see this file, so I'm asking first."
TASK_UNKNOWN = "I couldn't find that task in Google Tasks, so I haven't changed anything."
_CELL = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*)$")


def created_ids(data: Any) -> list[str]:
    """Ids a create action returned (Drive file, Doc, Sheet, task), wherever Composio nests them."""
    found: list[str] = []
    for key in ID_KEYS:
        value = pick(data, key, f"response_data.{key}", f"data.{key}")
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found


def record_created(task_id: int | None, ids: Iterable[str]) -> None:
    ids = [i for i in ids if i]
    if task_id is None or not ids:
        return
    _created.setdefault(task_id, set()).update(ids)
    _created.move_to_end(task_id)
    while len(_created) > MAX_TRACKED_TASKS:
        _created.popitem(last=False)


def created_by(task_id: int) -> set[str]:
    return set(_created.get(task_id, ()))


# --- users.state["workspace"] -------------------------------------------------------------------------


async def update_workspace_state(user_id: int, patch: dict) -> dict:
    """The only writer of users.state["workspace"]: merges `patch` inside the users row lock, so a webhook,
    a poll and a prepare step running at once never drop each other's keys."""
    return await users.update_nested(user_id, STATE_KEY, patch)


async def my_email(
    ctx: ToolContext, *, provider: IntegrationProvider | None = None, cache: ConnectionCache | None = None
) -> str:
    """The user's Google address (Gmail profile), cached in users.state; "" when unknown."""
    state = (await users.get_state(ctx.user_id)).get(STATE_KEY) or {}
    if isinstance(state, dict) and state.get("email"):
        return str(state["email"])
    try:
        data = await action_data(ctx, "mail.profile", NoArgs(), provider=provider, cache=cache)
    except ActionFailed:
        return ""
    email = str(pick(data, "emailAddress", "response_data.emailAddress", "data.response_data.emailAddress",
                     default="") or "").strip().lower()
    if email:
        await update_workspace_state(ctx.user_id, {"email": email})
    return email


# --- file ownership and sharing -------------------------------------------------------------------------


@dataclass(frozen=True)
class FileMeta:
    file_id: str
    name: str
    mime: str
    owned_by_me: bool
    shared_with_others: bool


def ownership(permissions: list[dict], me: str) -> tuple[bool, bool]:
    """(owned_by_me, shared_with_others) from a Drive permission list. Only a writer can list permissions,
    so a file whose one live permission is an owner is the caller's own, unshared file.

    Known limitation (live LIST_PERMISSIONS may omit emailAddress): without emails, a shared file never
    matches `me`, so it counts as someone else's. Safe for writes (more approvals), but WorkspaceIntake's
    "comment on your doc" notifies and mention checks also stop firing for shared docs.
    """
    me = (me or "").strip().lower()
    live = [p for p in permissions if isinstance(p, dict) and not p.get("deleted")]
    owners = [p for p in live if p.get("role") == "owner"]
    mine = bool(me) and any(str(p.get("emailAddress") or "").strip().lower() == me for p in owners)
    owned = (len(live) == 1 and len(owners) == 1) or mine
    shared = len(live) > 1 or any(p.get("type") in ("anyone", "domain") for p in live)
    return owned, shared


def _file_key(user_id: int, file_id: str) -> str:
    return f"file_meta:{user_id}:{file_id}"


async def file_meta(
    ctx: ToolContext,
    file_id: str,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> FileMeta | None:
    """Name, type, ownership and sharing of a Drive file; None when any lookup fails (callers fail closed).
    Cached for the tool loop in ToolRun.memo, so one turn looks a file up once."""
    run = current_run.get()
    key = _file_key(ctx.user_id, file_id)
    if run is not None and key in run.memo:
        return run.memo[key]
    ref = FileArgs(file_id=file_id)
    try:
        meta = await action_data(ctx, "drive.meta", ref, provider=provider, cache=cache)
        listed = await action_data(ctx, "drive.permissions", ref, provider=provider, cache=cache)
    except ActionFailed:
        result = None
    else:
        permissions = extract_list(listed, "permissions", "data.permissions", "response_data.permissions")
        owned, shared = ownership(permissions, await my_email(ctx, provider=provider, cache=cache))
        result = FileMeta(file_id, str(pick(meta, "name", "data.name", default="") or ""),
                          str(pick(meta, "mimeType", "data.mimeType", default="") or ""), owned, shared)
    if run is not None:
        run.memo[key] = result
    return result


def forget_file(ctx: ToolContext, file_id: str) -> None:
    """Drop a file's cached facts after a call that changes who can see it (drive.share)."""
    run = current_run.get()
    if run is not None:
        run.memo.pop(_file_key(ctx.user_id, file_id), None)


# --- overwrite count -----------------------------------------------------------------------------------


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def _col_letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def target_range(sheet: str, start_cell: str, rows: int, cols: int) -> str:
    m = _CELL.match(start_cell.strip())
    if m is None:
        raise ValueError(f"not a cell: {start_cell!r}")
    col, row = _col_index(m[1]), int(m[2])
    end = f"{_col_letters(col + max(cols, 1) - 1)}{row + max(rows, 1) - 1}"
    return f"'{sheet.replace(chr(39), chr(39) * 2)}'!{start_cell.strip().upper()}:{end}"


async def filled_cells(ctx: ToolContext, args: SheetUpdateArgs) -> int | None:
    """How many non-empty cells the write would overwrite; None when the range cannot be read."""
    cols = max(len(r) for r in args.values)
    try:
        where = target_range(args.sheet_name, args.start_cell, len(args.values), cols)
        data = await action_data(ctx, "sheets.read", SheetsReadArgs(spreadsheet_id=args.spreadsheet_id,
                                                                     range=where))
    except (ActionFailed, ValueError):
        return None
    found, rows = sheet_rows(data)
    if not found:
        return None  # no valueRanges in the reply: not proof that the range is empty
    return sum(1 for row in rows for cell in row if str(cell).strip())


# --- prepare steps -------------------------------------------------------------------------------------


def _note(meta: FileMeta) -> str:
    whose = "yours" if meta.owned_by_me else "owned by someone else"
    who = "shared with others" if meta.shared_with_others else "only you have access"
    return f"File: {one_line(meta.name or meta.file_id, 80)} ({kind_of(meta.mime)}), {whose}, {who}"


def escalation(meta: FileMeta | None) -> Prepared:
    """Spec 4.1: someone else's file, or a file anyone else can see, is OUTWARD; unknown is OUTWARD."""
    if meta is None:
        return Prepared(risk=RiskClass.OUTWARD, note=UNKNOWN_NOTE)
    if not meta.owned_by_me or meta.shared_with_others:
        return Prepared(risk=RiskClass.OUTWARD, note=_note(meta))
    return Prepared(note=_note(meta))


async def prepare_doc_write(ctx: ToolContext, args: Any) -> Prepared:
    return escalation(await file_meta(ctx, args.document_id))


async def prepare_row(ctx: ToolContext, args: Any) -> Prepared:
    return escalation(await file_meta(ctx, args.spreadsheet_id))


async def prepare_cells(ctx: ToolContext, args: Any) -> Prepared:
    base = escalation(await file_meta(ctx, args.spreadsheet_id))
    filled = await filled_cells(ctx, args)
    if filled is None or filled >= OVERWRITE_LIMIT:
        what = "cells that may already hold data" if filled is None else f"{filled} filled cells"
        return Prepared(risk=RiskClass.DESTRUCTIVE, note=f"{base.note}\nThis overwrites {what}.")
    return base


ESCALATIONS: dict[str, PrepareFn] = {
    "docs.append": prepare_doc_write,
    "sheets.append_row": prepare_row,
    "sheets.update_range": prepare_cells,
}


# --- Google Tasks: the real task, never the model's idea of it -------------------------------------------


@dataclass(frozen=True)
class TaskFacts:
    task_id: str
    title: str
    status: str  # "needsAction" or "completed"


async def task_facts(
    ctx: ToolContext,
    task_id: str,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> TaskFacts | None:
    """The task's title and status as Google has them (tasks.get); None when the lookup fails or the reply
    has no title. Cached for the tool loop, so prepare and the write share one lookup."""
    run = current_run.get()
    key = f"task:{ctx.user_id}:{task_id}"
    if run is not None and key in run.memo:
        return run.memo[key]
    try:
        data = await action_data(ctx, "tasks.get", TaskRefArgs(task_id=task_id), provider=provider,
                                 cache=cache)
    except ActionFailed:
        data = None
    title = pick(data, "title", "response_data.title", "data.title", "data.response_data.title")
    status = pick(data, "status", "response_data.status", "data.status", "data.response_data.status")
    result = None
    if isinstance(title, str) and title.strip():
        result = TaskFacts(task_id, title, "completed" if status == "completed" else "needsAction")
    if run is not None:
        run.memo[key] = result
    return result


async def prepare_task(ctx: ToolContext, args: Any) -> Prepared:
    """tasks.complete / tasks.update: the approval note names the real task; an unknown task is refused."""
    facts = await task_facts(ctx, args.task_id)
    if facts is None:
        return Prepared(refusal=TASK_UNKNOWN)
    return Prepared(note=f"Task: {one_line(facts.title)}")


VERIFIERS: dict[str, PrepareFn] = {"tasks.complete": prepare_task, "tasks.update": prepare_task}

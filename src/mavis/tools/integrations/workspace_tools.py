"""Google Workspace actions that need more than one provider call, and the registry hooks tools.py attaches.

CUSTOM_FNS replaces the default single-call `gated` for an action; PREPARES holds MavisTool.prepare
pre-steps (risk escalation and the tainted-task file allowlist). Everything a provider returns here is
third-party content: tools.py registers these tools with untrusted_output=True.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path

import httpcore
import httpx
from pydantic import BaseModel

from mavis.config import get_settings
from mavis.domain.errors import ActionFailed
from mavis.store.repo import tasks as tasks_repo
from mavis.tools import web
from mavis.tools.integrations.actions import (
    DocAppendArgs,
    DocArgs,
    DocInsertArgs,
    DriveDownloadArgs,
    DriveMoveArgs,
    DriveShareArgs,
    DriveUploadArgs,
    DriveUploadFileArgs,
    FileArgs,
    TaskCompleteArgs,
    TaskPatchArgs,
    TaskUpdateArgs,
)
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import pick
from mavis.tools.integrations.tools import action_data
from mavis.tools.integrations.workspace_guard import (
    DESTINATIONS,
    ESCALATIONS,
    FILE_TARGETS,
    TASK_UNKNOWN,
    VERIFIERS,
    TaskFacts,
    created_ids,
    forget_file,
    guarded,
    record_created,
    remember_task,
    task_facts,
)
from mavis.tools.integrations.workspace_render import (
    clip_body,
    doc_end_index,
    kind_of,
    one_line,
    render_created,
)
from mavis.tools.registry import PrepareFn, ToolContext

CustomFn = Callable[[ToolContext, BaseModel], Awaitable[str]]
Fetch = Callable[[str], Awaitable[str]]

# Google-native files have no bytes of their own: DOWNLOAD_FILE exports them to the mime type asked for.
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # the first sheet only
    "application/vnd.google-apps.presentation": "text/plain",
}
UPLOAD_LIMIT = 5 * 1024 * 1024  # googlesuper UPLOAD_FILE takes at most 5 MB
TEXT_MIMES = ("text/", "application/json", "application/xml")


def _expect[M: BaseModel](args: BaseModel, model: type[M]) -> M:
    """The registry validated `args` against the action's model; anything else is a wiring bug."""
    if not isinstance(args, model):
        raise TypeError(f"expected {model.__name__}, got {type(args).__name__}")
    return args


async def drive_read(
    ctx: ToolContext,
    args: FileArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
    fetch: Fetch | None = None,
) -> str:
    """Metadata (name, type) -> DOWNLOAD_FILE (exported for Docs/Sheets/Slides) -> fetch the file link."""
    ref = FileArgs(file_id=args.file_id)
    meta = await action_data(ctx, "drive.meta", ref, provider=provider, cache=cache)
    mime = str(pick(meta, "mimeType", "data.mimeType", default=""))
    name = one_line(pick(meta, "name", "data.name", default="(untitled)"))
    head = f"file_id={args.file_id} | {name} | {kind_of(mime)}"
    export = EXPORTS.get(mime, "")
    if not export and not mime.startswith(TEXT_MIMES):
        return (f"{head}\nThis is a {kind_of(mime)} file, so I can't read its text. "
                "Docs, Sheets, Slides and text files work.")
    data = await action_data(ctx, "drive.download", DriveDownloadArgs(file_id=args.file_id, mime_type=export),
                             provider=provider, cache=cache)
    url = pick(data, "downloaded_file_content.s3url", "data.downloaded_file_content.s3url")
    if not isinstance(url, str) or not url:
        raise ActionFailed("drive.read failed: the download returned no file",
                           reason="the download returned no file")
    try:
        text = await (fetch or web.fetch_file)(url)
    except (ValueError, httpx.HTTPError, httpcore.NetworkError, httpcore.TimeoutException,
            httpcore.ProtocolError, TimeoutError) as exc:
        raise ActionFailed(f"drive.read failed: could not fetch the file ({type(exc).__name__})",
                           reason="could not fetch the file") from None
    return f"{head}\n\n{clip_body(text) or '(empty)'}"


async def _drive_read(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, FileArgs)
    return await drive_read(ctx, args)


def _artifact_file(raw: str) -> tuple[Path, int | None]:
    """(resolved path, size) of an artifact inside ARTIFACTS_DIR; size None if outside it or missing."""
    path = Path(raw).resolve()
    if not path.is_relative_to(get_settings().artifacts_dir.resolve()) or not path.is_file():
        return path, None
    return path, path.stat().st_size


def _upload_name(title: str, path: Path) -> str:
    """The artifact title, with the file's own extension when the title has none."""
    if not title:
        return path.name
    return title if Path(title).suffix else title + path.suffix


async def drive_upload(
    ctx: ToolContext,
    args: DriveUploadArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    """Phase 6 hook: upload an artifact THIS task produced. Other tasks' files and paths outside the
    artifacts directory are never sent."""
    if ctx.task_id is None:
        raise ActionFailed("drive.upload failed: only a background task can upload its files",
                           reason="only a background task can upload its files")
    produced = await tasks_repo.artifacts_for(ctx.task_id)
    artifact = next((x for x in produced if x.id == args.artifact_id), None)
    if artifact is None or artifact.user_id != ctx.user_id:
        raise ActionFailed(f"drive.upload failed: this task has no file #{args.artifact_id}",
                           reason="that file is not from this task")
    path, size = await asyncio.to_thread(_artifact_file, artifact.path)
    if size is None:
        raise ActionFailed("drive.upload failed: the file is missing", reason="the file is missing")
    if size > UPLOAD_LIMIT:
        raise ActionFailed("drive.upload failed: Drive uploads are limited to 5 MB",
                           reason="the file is larger than 5 MB")
    staged = DriveUploadFileArgs(path=str(path), name=_upload_name(artifact.title, path), mime=artifact.mime,
                                 folder_id=args.folder_id)
    data = await action_data(ctx, "drive.upload_file", staged, provider=provider, cache=cache)
    record_created(ctx.task_id, created_ids(data))
    return render_created(data)


async def _drive_upload(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, DriveUploadArgs)
    return await drive_upload(ctx, args)


async def docs_append(
    ctx: ToolContext,
    args: DocAppendArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    """UPDATE_DOCUMENT_MARKDOWN would replace the whole doc, so: read it, then insert before the final
    newline (INSERT_TEXT_ACTION at the last endIndex - 1)."""
    doc = await action_data(ctx, "docs.read", DocArgs(document_id=args.document_id), provider=provider,
                            cache=cache)
    index = doc_end_index(doc)
    if index is None or index < 1:
        raise ActionFailed("docs.append failed: could not find the end of the document",
                           reason="could not find the end of the document")
    text = args.text if args.text.startswith("\n") else "\n" + args.text
    insert = DocInsertArgs(document_id=args.document_id, text=text, index=index)
    await action_data(ctx, "docs.insert_text", insert, provider=provider, cache=cache)
    return "Done. Added the text at the end of the document."


async def _docs_append(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, DocAppendArgs)
    return await docs_append(ctx, args)


async def drive_share(
    ctx: ToolContext,
    args: DriveShareArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    """Share, then forget the file's cached ownership facts: a later write in the same run must see that
    someone else can now read it."""
    try:
        data = await action_data(ctx, "drive.share", args, provider=provider, cache=cache)
    finally:
        forget_file(ctx, args.file_id)
    return render_created(data)


async def _drive_share(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, DriveShareArgs)
    return await drive_share(ctx, args)


async def drive_move(
    ctx: ToolContext,
    args: DriveMoveArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    """Move, then forget the file's cached facts: a file moved into a shared folder inherits its sharing."""
    try:
        data = await action_data(ctx, "drive.move", args, provider=provider, cache=cache)
    finally:
        forget_file(ctx, args.file_id)
    return render_created(data)


async def _drive_move(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, DriveMoveArgs)
    return await drive_move(ctx, args)


async def _patch_task(
    ctx: ToolContext,
    action: str,
    task_id: str,
    *,
    title: str | None = None,
    done: bool | None = None,
    notes: str | None = None,
    due: date | None = None,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    """PATCH_TASK needs a title and a status, so read the task first and keep what the call does not change.
    No verified task, no write."""
    facts = await task_facts(ctx, task_id, provider=provider, cache=cache)
    if facts is None:
        raise ActionFailed(f"{action} failed: {TASK_UNKNOWN}", reason="that task could not be found")
    status = facts.status if done is None else ("completed" if done else "needsAction")
    patch = TaskPatchArgs(task_id=task_id, title=title or facts.title, status=status, notes=notes, due=due)
    data = await action_data(ctx, "tasks.patch", patch, provider=provider, cache=cache)
    remember_task(ctx, TaskFacts(task_id, patch.title, patch.status))
    return render_created(data)


async def tasks_complete(
    ctx: ToolContext,
    args: TaskCompleteArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    return await _patch_task(ctx, "tasks.complete", args.task_id, done=True, provider=provider, cache=cache)


async def tasks_update(
    ctx: ToolContext,
    args: TaskUpdateArgs,
    *,
    provider: IntegrationProvider | None = None,
    cache: ConnectionCache | None = None,
) -> str:
    return await _patch_task(ctx, "tasks.update", args.task_id, title=args.title, done=args.done,
                             notes=args.notes, due=args.due, provider=provider, cache=cache)


async def _tasks_complete(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, TaskCompleteArgs)
    return await tasks_complete(ctx, args)


async def _tasks_update(ctx: ToolContext, args: BaseModel) -> str:
    args = _expect(args, TaskUpdateArgs)
    return await tasks_update(ctx, args)


def creating(
    action: str, *, provider: IntegrationProvider | None = None, cache: ConnectionCache | None = None
) -> CustomFn:
    """A create action: run it, remember what this task made (allowlist source), return only the ids."""

    async def fn(ctx: ToolContext, args: BaseModel) -> str:
        data = await action_data(ctx, action, args, provider=provider, cache=cache)
        record_created(ctx.task_id, created_ids(data))
        return render_created(data)

    return fn


CREATES = ("drive.create_folder", "docs.create", "sheets.create", "tasks.add")
CUSTOM_FNS: dict[str, CustomFn] = {
    "drive.read": _drive_read, "drive.upload": _drive_upload, "docs.append": _docs_append,
    "drive.share": _drive_share, "drive.move": _drive_move,
    "tasks.complete": _tasks_complete, "tasks.update": _tasks_update,
    **{name: creating(name) for name in CREATES},
}
# Every file-changing or sharing action, and every write into a folder, is allowlisted in tainted tasks
# (spec 4.3), then runs its own escalation or verification step, if it has one.
_STEPS: dict[str, PrepareFn] = {**ESCALATIONS, **VERIFIERS}
PREPARES: dict[str, PrepareFn] = {
    **_STEPS,
    **{name: guarded(name, _STEPS.get(name)) for name in (*FILE_TARGETS, *DESTINATIONS)},
}

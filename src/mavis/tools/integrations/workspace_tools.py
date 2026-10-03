"""Google Workspace actions that need more than one provider call, and the registry hooks tools.py attaches.

CUSTOM_FNS replaces the default single-call `gated` for an action; PREPARES holds MavisTool.prepare
pre-steps (risk escalation and the tainted-task file allowlist). Everything a provider returns here is
third-party content: tools.py registers these tools with untrusted_output=True.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpcore
import httpx
from pydantic import BaseModel

from mavis.config import get_settings
from mavis.domain.errors import ActionFailed
from mavis.store.repo import tasks as tasks_repo
from mavis.tools import web
from mavis.tools.integrations.actions import DriveDownloadArgs, DriveUploadArgs, DriveUploadFileArgs, FileArgs
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import pick
from mavis.tools.integrations.tools import action_data
from mavis.tools.integrations.workspace_guard import created_ids, record_created
from mavis.tools.integrations.workspace_render import clip_body, kind_of, one_line, render_created
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
    assert isinstance(args, FileArgs)
    return await drive_read(ctx, args)


def _artifact_file(raw: str) -> tuple[Path, int | None]:
    """(resolved path, size) of an artifact inside ARTIFACTS_DIR; size None if outside it or missing."""
    path = Path(raw).resolve()
    if not path.is_relative_to(get_settings().artifacts_dir.resolve()) or not path.is_file():
        return path, None
    return path, path.stat().st_size


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
    staged = DriveUploadFileArgs(path=str(path), name=artifact.title or path.name, mime=artifact.mime,
                                 folder_id=args.folder_id)
    data = await action_data(ctx, "drive.upload_file", staged, provider=provider, cache=cache)
    record_created(ctx.task_id, created_ids(data))
    return render_created(data)


async def _drive_upload(ctx: ToolContext, args: BaseModel) -> str:
    assert isinstance(args, DriveUploadArgs)
    return await drive_upload(ctx, args)


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
    "drive.read": _drive_read, "drive.upload": _drive_upload, **{name: creating(name) for name in CREATES},
}
PREPARES: dict[str, PrepareFn] = {}

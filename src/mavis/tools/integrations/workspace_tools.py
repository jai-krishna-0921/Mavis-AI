"""Google Workspace actions that need more than one provider call, and the registry hooks tools.py attaches.

CUSTOM_FNS replaces the default single-call `gated` for an action; PREPARES holds MavisTool.prepare
pre-steps (risk escalation and the tainted-task file allowlist). Everything a provider returns here is
third-party content: tools.py registers these tools with untrusted_output=True.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from mavis.domain.errors import ActionFailed
from mavis.tools import web
from mavis.tools.integrations.actions import DriveDownloadArgs, FileArgs
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.connections import ConnectionCache
from mavis.tools.integrations.normalize import pick
from mavis.tools.integrations.tools import action_data
from mavis.tools.integrations.workspace_render import clip_body, kind_of, one_line
from mavis.tools.registry import PrepareFn, ToolContext

CustomFn = Callable[[ToolContext, BaseModel], Awaitable[str]]
Fetch = Callable[[str], Awaitable[str]]

# Google-native files have no bytes of their own: DOWNLOAD_FILE exports them to the mime type asked for.
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # the first sheet only
    "application/vnd.google-apps.presentation": "text/plain",
}
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
    except (ValueError, httpx.HTTPError, TimeoutError) as exc:
        raise ActionFailed(f"drive.read failed: could not fetch the file ({type(exc).__name__})",
                           reason="could not fetch the file") from None
    return f"{head}\n\n{clip_body(text) or '(empty)'}"


async def _drive_read(ctx: ToolContext, args: BaseModel) -> str:
    assert isinstance(args, FileArgs)
    return await drive_read(ctx, args)


CUSTOM_FNS: dict[str, CustomFn] = {"drive.read": _drive_read}
PREPARES: dict[str, PrepareFn] = {}

# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""OneDrive over Microsoft Graph: search, recent and shared files, and reading a file as text.

Reading is the only part that leaves Graph. `/content` answers 302 to a pre-authenticated download URL on a
SharePoint or OneDrive host Graph does not name in advance. That one redirect is followed here, by hand:
the host is checked against an allowlist BEFORE any request (https, no userinfo, default port), the bearer
token is never sent to it, the body is size-capped while streaming, a second redirect is refused, and the
URL (it carries a temporary credential) is never put in a result, an error or a log line.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from mavis.domain.errors import FailureKind
from mavis.tools.integrations.native import gmail_mime
from mavis.tools.integrations.native.graph import GRAPH, REDIRECTS, GraphError, seg, strip_ms, with_ms
from mavis.tools.integrations.native.office_text import OFFICE_MIMES, UnreadableFile, office_text

DOWNLOAD_CAP_BYTES = 1_000_000  # text read from a file is cut here
EXPORT_CAP_BYTES = 10_000_000  # Office, PDF and converted files are read whole up to here
TEXT_MIMES = ("application/json", "application/xml", "application/csv")
CONVERT_EXTS = (".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp", ".csv")
CONVERT_MIMES = frozenset(
    {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/rtf",
        "text/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
    }
)
# Download hosts, matched as a dot-bounded suffix ("evilsharepoint.com" and "sharepoint.com.evil.test" fail).
TRUSTED_SUFFIXES = (
    ".sharepoint.com",
    ".sharepoint.cn",
    ".1drv.com",
    ".onedrive.com",
    ".live.com",
    ".microsoftpersonalcontent.com",
    ".graph.microsoft.com",
)
SELECT = "id,name,size,file,folder,lastModifiedDateTime,lastModifiedBy,webUrl,remoteItem,parentReference"
SHARED_GONE = (400, 404, 410, 501)
SHARED_UNAVAILABLE = "shared files are not available right now"
UNTRUSTED = "download location not trusted"
NOT_CONVERTED = "could not be converted to text"
TOO_BIG = "the file is over 10 MB, too large to read"
_STATUS = re.compile(r"microsoft (\d{3})")


class UntrustedDownload(GraphError):
    """The download redirect was not an allowed host, or a second redirect came. Never converted to a note."""

    def __init__(self) -> None:
        super().__init__(FailureKind.UNAVAILABLE, UNTRUSTED)


def trusted_download_url(location: str) -> httpx.URL | None:
    """The parsed URL when it is https on an allowlisted host (default port, no userinfo), else None."""
    try:
        url = httpx.URL(location)
    except (httpx.InvalidURL, ValueError):
        return None
    host = url.host.lower().rstrip(".") if url.host else ""
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.userinfo
        or not any(host.endswith(s) and len(host) > len(s) for s in TRUSTED_SUFFIXES)
    ):
        return None
    return url


def _person(node: Any) -> str:
    user = node.get("user") if isinstance(node, dict) else None
    return str(user.get("displayName") or "") if isinstance(user, dict) else ""


def item_view(raw: dict) -> dict:
    """One drive item. A shared item (`remoteItem`) is addressed by its own drive: `ms:<driveId>~<itemId>`.
    No download URL is ever copied out of the raw item."""
    remote = raw.get("remoteItem") if isinstance(raw.get("remoteItem"), dict) else None
    src = remote or raw
    ref = src.get("parentReference") if isinstance(src.get("parentReference"), dict) else {}
    drive_id, item_id = str(ref.get("driveId") or ""), str(src.get("id") or "")
    shared = remote is not None
    marker = src.get("shared") if isinstance(src.get("shared"), dict) else {}
    file_facet = src.get("file") if isinstance(src.get("file"), dict) else {}
    return {
        "id": with_ms(f"{drive_id}~{item_id}" if shared and drive_id else item_id),
        "name": str(src.get("name") or raw.get("name") or ""),
        "mimeType": str(file_facet.get("mimeType") or ""),
        "size": src.get("size") if isinstance(src.get("size"), int) else raw.get("size") or 0,
        "modifiedTime": str(raw.get("lastModifiedDateTime") or src.get("lastModifiedDateTime") or ""),
        "modifiedBy": _person(raw.get("lastModifiedBy")) or _person(src.get("lastModifiedBy")),
        "webViewLink": str(src.get("webUrl") or raw.get("webUrl") or ""),
        "shared": shared,
        "sharedBy": (_person(marker.get("sharedBy")) or _person(marker.get("owner"))) if shared else "",
    }


def _is_folder(raw: dict) -> bool:
    remote = raw.get("remoteItem")
    return "folder" in raw or (isinstance(remote, dict) and "folder" in remote)


def item_path(item_id: str) -> str:
    """`/me/drive/items/{id}` or, for the compound `<driveId>~<itemId>`, `/drives/{driveId}/items/{id}`."""
    try:
        value = strip_ms(item_id)
        if "~" not in value:
            return f"/me/drive/items/{seg(value)}"
        drive, _, item = value.partition("~")
        if "~" in item:
            raise GraphError(FailureKind.INVALID_ARGUMENT, "invalid id")
        return f"/drives/{seg(drive)}/items/{seg(item)}"
    except GraphError as exc:
        raise GraphError(exc.kind, exc.detail, field="item_id") from None


def _is_text(mime: str) -> bool:
    return mime.startswith("text/") or mime in TEXT_MIMES


def _charset(headers: httpx.Headers) -> str | None:
    match = re.search(r"charset=([\w-]+)", headers.get("content-type", ""))
    return match[1] if match else None


class FilesActions:
    def __init__(self, ex: Any) -> None:
        self.ex = ex

    async def search(self, uid: int, a: Any) -> dict:
        term = a.query.strip()
        if not term:
            return await self._listing(uid, f"{GRAPH}/me/drive/recent", None, a.max_results)
        path = quote(term.replace("'", "''"), safe="")
        return await self._listing(
            uid,
            f"{GRAPH}/me/drive/search(q='{path}')",
            {"$top": str(a.max_results), "$select": SELECT},
            a.max_results,
        )

    async def recent(self, uid: int, a: Any) -> dict:
        if not a.shared_with_me:
            return await self._listing(uid, f"{GRAPH}/me/drive/recent", None, a.max_results)
        try:
            return await self._listing(uid, f"{GRAPH}/me/drive/sharedWithMe", None, a.max_results)
        except GraphError as exc:
            status = _STATUS.match(exc.detail)
            if status and int(status[1]) in SHARED_GONE:
                raise GraphError(FailureKind.UNAVAILABLE, SHARED_UNAVAILABLE) from None
            raise

    async def _listing(self, uid: int, url: str, params: dict | None, limit: int) -> dict:
        data = await self.ex.graph.json(
            uid, "GET", url, params=params, on_forbidden=FailureKind.PERMISSION_MISSING
        )
        raw = [
            x for x in (data.get("value") if isinstance(data, dict) else None) or [] if isinstance(x, dict)
        ]
        return {"files": [item_view(x) for x in raw if not _is_folder(x)][:limit]}

    async def read(self, uid: int, a: Any) -> dict:
        path = item_path(a.item_id)
        raw = await self.ex.graph.json(uid, "GET", f"{GRAPH}{path}", params={"$select": SELECT})
        raw = raw if isinstance(raw, dict) else {}
        if _is_folder(raw):
            raise GraphError(FailureKind.INVALID_ARGUMENT, "that is a folder, not a file", "item_id")
        view = item_view(raw)
        mime, name = view["mimeType"].lower(), view["name"].lower()
        size = view["size"] if isinstance(view["size"], int) else 0
        if size > EXPORT_CAP_BYTES:
            raise GraphError(FailureKind.INVALID_ARGUMENT, TOO_BIG, "item_id")
        out = {
            "file_id": with_ms(strip_ms(a.item_id)),
            "id": with_ms(strip_ms(a.item_id)),
            "name": view["name"],
            "mimeType": view["mimeType"],
            "size": view["size"],
            "text": "",
            "truncated": False,
        }
        url = f"{GRAPH}{path}/content"
        if mime in OFFICE_MIMES:
            data, _, cut = await self._content(uid, url, None, EXPORT_CAP_BYTES)
            if cut:
                raise GraphError(FailureKind.INVALID_ARGUMENT, TOO_BIG, "item_id")
            try:
                out["text"] = await asyncio.to_thread(office_text, data, mime)
            except UnreadableFile as exc:
                raise GraphError(FailureKind.INVALID_ARGUMENT, str(exc), "item_id") from None
        elif _is_text(mime):
            data, headers, cut = await self._content(uid, url, None, DOWNLOAD_CAP_BYTES)
            out["text"], out["truncated"] = gmail_mime.decode_bytes(data, _charset(headers)), cut
        elif mime in CONVERT_MIMES or name.endswith(CONVERT_EXTS):
            out |= await self._converted(uid, url)
        else:
            raise GraphError(
                FailureKind.INVALID_ARGUMENT,
                f"{view['mimeType'] or 'this file'} is not a text file",
                "item_id",
            )
        return out

    async def _converted(self, uid: int, url: str) -> dict:
        """Graph's own conversion to PDF, then the PDF text. Any failure is metadata only, except a refused
        download host, which stays an error."""
        try:
            data, _, cut = await self._content(uid, url, {"format": "pdf"}, EXPORT_CAP_BYTES)
            if cut or not data.startswith(b"%PDF"):
                return {"note": NOT_CONVERTED}
            return {"text": await asyncio.to_thread(office_text, data, "application/pdf")}
        except UntrustedDownload:
            raise
        except UnreadableFile:
            return {"note": NOT_CONVERTED}
        except GraphError as exc:
            if exc.kind in (FailureKind.AUTH, FailureKind.PERMISSION_MISSING):
                raise
            return {"note": NOT_CONVERTED}

    async def _content(
        self, uid: int, url: str, params: dict | None, cap: int
    ) -> tuple[bytes, httpx.Headers, bool]:
        """The file bytes: direct from Graph, or through the single allowlisted redirect."""
        status, headers, data, cut = await self.ex.graph.raw_status(
            uid,
            "GET",
            url,
            params=params,
            cap=cap,
            accept_redirect=True,
            on_forbidden=FailureKind.PERMISSION_MISSING,
        )
        if status not in REDIRECTS:
            return data, headers, cut
        target = trusted_download_url(headers.get("location", ""))
        if target is None:
            raise UntrustedDownload()
        return await self._download(target, cap)

    async def _download(self, url: httpx.URL, cap: int) -> tuple[bytes, httpx.Headers, bool]:
        """GET the pre-authenticated URL: no Authorization header, no redirects, capped while streaming.
        Errors carry no URL and are raised `from None`, so the URL cannot reach a log through a traceback."""
        try:
            async with self.ex.graph.http.stream("GET", url, follow_redirects=False) as resp:
                if resp.status_code in REDIRECTS:
                    raise UntrustedDownload()
                if resp.status_code != 200:
                    kind = (
                        FailureKind.NOT_FOUND
                        if resp.status_code in (403, 404, 410)
                        else FailureKind.UNAVAILABLE
                    )
                    raise GraphError(kind, f"download failed ({resp.status_code})")
                chunks, size, cut = [], 0, False
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > cap:
                        cut = True
                        break
                return b"".join(chunks)[:cap], resp.headers, cut
        except httpx.HTTPError:
            raise GraphError(FailureKind.UNAVAILABLE, "download failed") from None


def files_handlers(ex: Any) -> dict[str, Callable[[int, Any], Awaitable[Any]]]:
    f = FilesActions(ex)
    return {"onedrive.search": f.search, "onedrive.read": f.read, "onedrive.list_recent": f.recent}

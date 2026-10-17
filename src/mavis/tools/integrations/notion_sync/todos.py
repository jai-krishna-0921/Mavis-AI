# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The two-way Todos pass: loops become rows in the Todos database, and the owner's edits flow back.

Order inside the pass: pull (the owner's edits), trash check, push (loops to rows), daily archive.
Echo suppression: a row last edited by the Mavis bot, or whose synced properties hash like the last push, is
never read back. Only edits by the connecting Notion user (`owner_id`) change loops; anyone else's edit is
mentioned once and applied to nothing. Cursors (all `notion_todos_*`): `seen`, `pushed`, `archived`, and
`foreign` (recent foreign edits already mentioned).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import structlog

from mavis.bus import get_bus
from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.errors import FailureKind
from mavis.domain.events import Trust
from mavis.domain.loops import Loop, LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.loops.service import LoopService
from mavis.memory.names import normalize_name
from mavis.store.repo import loops as loop_repo
from mavis.store.repo import notion_links, users
from mavis.tools.integrations.native.notion_api import NotionError
from mavis.tools.integrations.notion_sync.blocks import TEXT_LIMIT, content_hash
from mavis.tools.integrations.notion_sync.home import HOME_LINK
from mavis.tools.integrations.notion_sync.runner import SyncCtx, _cursors, _put, _when, register_pass

log = structlog.get_logger(__name__)

LINK = "todo"
PERSON_LINK = "person"
PROJECT_LINK = "project"
KINDS = frozenset({LoopKind.COMMITMENT, LoopKind.WAITING_ON, LoopKind.GOAL})
KIND_LABEL = {LoopKind.COMMITMENT: "I owe", LoopKind.WAITING_ON: "Waiting on them", LoopKind.GOAL: "Goal"}
LABEL_KIND = {v: k for k, v in KIND_LABEL.items()}
STATUS_LABEL = {
    LoopStatus.OPEN: "Open", LoopStatus.AWAITING_REPLY: "Open", LoopStatus.BLOCKED: "Blocked",
    LoopStatus.DONE: "Done", LoopStatus.DROPPED: "Dropped", LoopStatus.EXPIRED: "Expired",
}
CLOSED = (LoopStatus.DONE, LoopStatus.DROPPED, LoopStatus.EXPIRED)
OVERLAP = timedelta(minutes=2)
MINUTE = timedelta(minutes=1)  # Notion truncates last_edited_time to the minute: ties go to the owner
ARCHIVE_AFTER = timedelta(days=30)
ARCHIVE_EVERY = timedelta(days=1)
FIRST_PUSH_BACK = timedelta(days=3650)
DEFAULT_DUE_HOUR = 9
FOREIGN_KEPT = 100
LIST_LIMIT = 5000


def _key(loop_id: int) -> str:
    return f"loop:{loop_id}"


# ----- reading and writing row properties

def _plain(prop: dict | None, kind: str) -> str:
    items = (prop or {}).get(kind) or []
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "") for r in items)


def _select(prop: dict | None) -> str | None:
    return ((prop or {}).get("select") or {}).get("name")


def _relation(prop: dict | None) -> list[str]:
    return sorted(r["id"] for r in (prop or {}).get("relation") or [] if r.get("id"))


def _row_due(prop: dict | None, tz: str) -> tuple[str | None, datetime | None]:
    """(local date string, an exact due_at when the row carries a time)."""
    start = ((prop or {}).get("date") or {}).get("start")
    if not start:
        return None, None
    if "T" not in start:
        return start[:10], None
    at = datetime.fromisoformat(start.replace("Z", "+00:00"))
    at = timeutil.to_utc(at, tz)
    return timeutil.to_local(at, tz).date().isoformat(), at


def _canon(name: str, key: str, kind: str | None, status: str | None, due_date: str | None,
           people: list[str], project: list[str], source: str) -> dict:
    return {"name": name, "key": key, "kind": kind, "status": status, "due": due_date,
            "people": sorted(people), "project": sorted(project), "source": source}


def _canon_page(page: dict, tz: str) -> dict:
    p = page.get("properties") or {}
    return _canon(_plain(p.get("Name"), "title"), _plain(p.get("Mavis key"), "rich_text"),
                  _select(p.get("Kind")), _select(p.get("Status")), _row_due(p.get("Due"), tz)[0],
                  _relation(p.get("People")), _relation(p.get("Project")),
                  _plain(p.get("Source"), "rich_text"))


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}] if text else []


class Names:
    """Local person and project links by normalised name, and page id back to name."""

    def __init__(self, people, projects) -> None:
        self.by_name = {PERSON_LINK: self._index(people), PROJECT_LINK: self._index(projects)}
        self.by_page = {r.page_id: str(r.meta.get("name") or "") for r in [*people, *projects]}

    @staticmethod
    def _index(rows) -> dict[str, str]:
        return {normalize_name(str(r.meta.get("name") or "")): r.page_id for r in rows
                if r.meta.get("name")}

    def ids_for(self, kind: str, entities: list[str]) -> list[str]:
        found = (self.by_name[kind].get(normalize_name(e)) for e in entities)
        return sorted({f for f in found if f})

    def names_of(self, page_ids: list[str]) -> list[str]:
        return [self.by_page[i] for i in page_ids if self.by_page.get(i)]


def _render(loop: Loop, tz: str, names: Names) -> tuple[dict, dict]:
    """(Notion properties, canonical dict) for a loop; the hash of the canonical dict is the push hash."""
    due_date = timeutil.to_local(loop.due_at, tz).date().isoformat() if loop.due_at else None
    people = names.ids_for(PERSON_LINK, loop.entities)
    project = names.ids_for(PROJECT_LINK, loop.entities)
    kind, status = KIND_LABEL[loop.kind], STATUS_LABEL[loop.status]
    canon = _canon(loop.title, _key(loop.id), kind, status, due_date, people, project, loop.source)
    props = {
        "Name": {"title": _rich(loop.title)},
        "Mavis key": {"rich_text": _rich(_key(loop.id))},
        "Kind": {"select": {"name": kind}},
        "Status": {"select": {"name": status}},
        "Due": {"date": {"start": due_date} if due_date else None},
        "People": {"relation": [{"id": i} for i in people]},
        "Project": {"relation": [{"id": i} for i in project]},
        "Source": {"rich_text": _rich(loop.source)},
    }
    canon["name"], canon["source"] = canon["name"][:TEXT_LIMIT], canon["source"][:TEXT_LIMIT]
    return props, canon


def _edited(page: dict) -> datetime:
    return timeutil.ensure_utc(datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")))


def _editor(page: dict) -> str:
    return (page.get("last_edited_by") or {}).get("id") or ""


# ----- notices

async def _mention_foreign(ctx: SyncCtx, page: dict, title: str) -> None:
    from mavis.initiative import wiring

    seen = list((await _cursors(ctx.user_id)).get("notion_todos_foreign") or [])
    tag = f"{page['id']}:{page['last_edited_time']}"
    if tag in seen:
        return
    shown = " ".join(title.split())[:80] or "a todo"
    user = await users.get(ctx.user_id)
    await wiring.current().executor.notify(user, NotifyIntent(
        urgency=2, dedupe_key=f"notion_foreign:{tag}",
        intent=f'Tell the user someone other than them changed the Notion todo "{shown}", and Mavis AI '
               f"left it as it was."), untrusted=True)
    await _put(ctx.user_id, notion_todos_foreign=(seen + [tag])[-FOREIGN_KEPT:])


# ----- pull

async def _pull(ctx: SyncCtx, service: LoopService, names: Names, tz: str, cursors: dict) -> bool:
    """Apply the owner's edits. False when cut short."""
    uid = ctx.user_id
    stored = _when(cursors.get("notion_todos_seen"))
    if stored is None:
        home = await notion_links.get(uid, *HOME_LINK)
        floor = (home.created_at if home and home.created_at else ctx.now)
    else:
        floor = stored - OVERLAP
    rows = await ctx.api.query(
        uid, ctx.home.ds["todos"], limit=LIST_LIMIT,
        filter={"timestamp": "last_edited_time",
                "last_edited_time": {"on_or_after": floor.strftime("%Y-%m-%dT%H:%M:%S.000Z")}},
        sorts=[{"timestamp": "last_edited_time", "direction": "ascending"}])
    newest = stored
    for page in rows:
        if not ctx.left():
            return False
        edited = _edited(page)
        await _pull_row(ctx, service, names, tz, page)
        newest = max(newest, edited) if newest else edited
    if newest is not None:
        await _put(uid, notion_todos_seen=newest.isoformat())
    return True


async def _pull_row(ctx: SyncCtx, service: LoopService, names: Names, tz: str, page: dict) -> None:
    uid = ctx.user_id
    editor = _editor(page)
    if editor == ctx.account.get("bot_user_id"):
        return
    link = await notion_links.by_page(uid, page["id"])
    link = link if link is not None and link.kind == LINK else None
    canon = _canon_page(page, tz)
    if link is not None and link.meta.get("adopting"):
        loop = await service.get(int(link.local_key))
        if loop is not None:
            await _write_key(ctx, loop, page, canon, tz)  # a retry: the loop exists, finish the writeback
        return
    if link is not None and content_hash(canon) == link.pushed_hash:
        return  # not a change to anything Mavis syncs
    if editor != ctx.account.get("owner_id"):
        await _mention_foreign(ctx, page, canon["name"])
        return
    if page.get("in_trash"):
        await _row_trashed(service, uid, link)
        return
    if link is None:
        await _adopt_row(ctx, service, names, tz, page, canon)
    else:
        await _apply_edit(ctx, service, names, tz, page, canon, link)


async def _adopt_row(ctx, service, names, tz, page, canon) -> None:
    """A row the owner added: a new commitment (never merged into a look-alike), keyed back onto the row."""
    title = canon["name"].strip()
    if not title:
        return  # an empty new row: wait until it has a title
    kind = LABEL_KIND.get(canon["kind"] or "", LoopKind.COMMITMENT)
    due_date, due_at = _row_due(page["properties"].get("Due"), tz)
    loop = await service.create_exact(ctx.user_id, LoopUpsert(
        kind=kind, title=title, due_at=due_at or _day(due_date, tz),
        entities=names.names_of(canon["people"] + canon["project"]), trust=Trust.USER,
        origin=LoopOrigin.NOTION, source=canon["source"]))
    if canon["status"] in ("Done", "Dropped"):
        loop = await service.close(
            loop.id, LoopStatus.DONE if canon["status"] == "Done" else LoopStatus.DROPPED) or loop
    # link first: if the writeback below fails, the retry finds this link and creates no second loop
    await notion_links.upsert(ctx.user_id, LINK, str(loop.id), page["id"], pushed_hash="",
                              meta={"adopting": True})
    await _write_key(ctx, loop, page, canon, tz)


async def _write_key(ctx, loop: Loop, page: dict, canon: dict, tz: str) -> None:
    patch = {"Mavis key": {"rich_text": _rich(_key(loop.id))}}
    if not canon["kind"]:
        patch["Kind"] = {"select": {"name": KIND_LABEL[loop.kind]}}
    if not canon["status"]:
        patch["Status"] = {"select": {"name": STATUS_LABEL[loop.status]}}
    fresh = await ctx.api.update_page(ctx.user_id, page["id"], properties=patch)
    await notion_links.upsert(
        ctx.user_id, LINK, str(loop.id), page["id"], pushed_hash=content_hash(_canon_page(fresh, tz)),
        pushed_at=timeutil.now(), edited_at=_edited(fresh), meta={})


def _day(day: str | None, tz: str) -> datetime | None:
    if not day:
        return None
    return timeutil.to_utc(datetime.combine(date.fromisoformat(day), time(DEFAULT_DUE_HOUR)), tz)


async def _apply_edit(ctx, service, names, tz, page, canon, link) -> None:
    uid = ctx.user_id
    loop = await service.get(int(link.local_key))
    if loop is None or loop.kind not in KINDS:
        return
    touched = await loop_repo.touched_at(loop.id)
    if touched and touched >= _edited(page) + MINUTE and link.pushed_at and touched > link.pushed_at:
        # Mavis changed the loop after the owner's edit: Mavis wins, and the push overwrites the row
        await notion_links.upsert(uid, LINK, link.local_key, link.page_id, pushed_hash="-")
        return
    _, current = _render(loop, tz, names)
    changes: dict = {}
    wanted_kind = LABEL_KIND.get(canon["kind"] or "")
    if wanted_kind and wanted_kind != loop.kind:
        changes["kind"] = wanted_kind
    if canon["name"].strip() and canon["name"] != loop.title:
        changes["title"] = canon["name"].strip()
    if canon["due"] and canon["due"] != current["due"]:
        changes["due_at"] = _row_due(page["properties"].get("Due"), tz)[1] or _day(canon["due"], tz)
    people = [n for n in names.names_of(canon["people"] + canon["project"])
              if normalize_name(n) not in {normalize_name(e) for e in loop.entities}]
    if people:
        changes["entities"] = people
    if changes:
        await service.upsert(uid, LoopUpsert(id=loop.id, trust=Trust.USER, **changes))
    status = canon["status"]
    if status in ("Done", "Dropped"):
        want = LoopStatus.DONE if status == "Done" else LoopStatus.DROPPED
        if loop.status != want:
            await service.close(loop.id, want)
    elif status == "Open" and loop.status in CLOSED:
        await service.upsert(uid, LoopUpsert(id=loop.id, status=LoopStatus.OPEN, trust=Trust.USER))
    await notion_links.upsert(uid, LINK, link.local_key, link.page_id, pushed_hash=content_hash(canon),
                              pushed_at=timeutil.now(), edited_at=_edited(page))


async def _row_trashed(service: LoopService, uid: int, link) -> None:
    if link is None:
        return
    loop = await service.get(int(link.local_key))
    if loop is not None and loop.status not in CLOSED:
        await service.close(loop.id, LoopStatus.DROPPED)
    # keep a tombstone so the push does not rebuild the row of a loop that was just dropped
    await notion_links.upsert(uid, LINK, link.local_key, link.page_id, meta={**link.meta, "trashed": True})


async def _trash_check(ctx: SyncCtx, service: LoopService) -> bool:
    """The data source query never returns trashed rows, so linked rows missing from a full listing are
    fetched: one the owner trashed drops its loop."""
    uid = ctx.user_id
    links = await notion_links.all_of(uid, LINK)
    if not links:
        return True
    live = {p["id"] for p in await ctx.api.query(uid, ctx.home.ds["todos"], limit=LIST_LIMIT)}
    for link in links:
        if link.page_id in live or link.meta.get("trashed"):
            continue
        if not ctx.left():
            return False
        try:
            page = await ctx.api.page(uid, link.page_id)
        except NotionError as exc:
            if exc.kind is FailureKind.NOT_FOUND:
                continue
            raise
        if not (page.get("in_trash") or page.get("is_archived")):
            continue
        if _editor(page) == ctx.account.get("owner_id"):
            await _row_trashed(service, uid, link)
        else:
            # trashed by someone else: say so once and change nothing; the tombstone stops re-checking
            props = page.get("properties") or {}
            await _mention_foreign(ctx, page, _plain(props.get("Name"), "title"))
            await notion_links.upsert(uid, LINK, link.local_key, link.page_id,
                                      meta={**link.meta, "trashed": True})
    return True


# ----- push

async def _push(ctx: SyncCtx, names: Names, tz: str, cursors: dict) -> bool:
    uid = ctx.user_id
    stored = _when(cursors.get("notion_todos_pushed"))
    started = ctx.now
    if stored is None:
        home = await notion_links.get(uid, *HOME_LINK)
        since = (home.created_at if home and home.created_at else ctx.now) - FIRST_PUSH_BACK
    else:
        since = stored
    stale = {lp.id for lp in await loop_repo.list_closed_before(uid, ctx.now - ARCHIVE_AFTER, KINDS)}
    if stored is None:
        for loop in await loop_repo.list_open(uid):
            if loop.kind in KINDS:
                if not ctx.left():
                    return False
                await _push_one(ctx, loop, names, tz, skip_new=loop.id in stale)
    while True:
        # oldest change first and capped: continue from the last loop done until a batch comes back short
        batch = await loop_repo.list_changed_since(uid, since, KINDS)
        for loop in batch:
            if not ctx.left():
                return False
            await _push_one(ctx, loop, names, tz, skip_new=loop.id in stale)
        last = await loop_repo.touched_at(batch[-1].id) if batch else None
        if len(batch) < loop_repo.CHANGED_LIMIT or last is None or last <= since:
            break
        since = last
    await _put(uid, notion_todos_pushed=started.isoformat())
    return True


async def _push_one(ctx: SyncCtx, loop: Loop, names: Names, tz: str, *, skip_new: bool) -> None:
    uid = ctx.user_id
    props, canon = _render(loop, tz, names)
    digest = content_hash(canon)
    link = await notion_links.get(uid, LINK, str(loop.id))
    if link is not None and link.meta.get("trashed"):
        if loop.status in CLOSED:
            return
        await notion_links.delete(uid, LINK, str(loop.id))  # reopened by Mavis: a new row
        link = None
    if link is not None and link.pushed_hash == digest:
        return
    if link is None and skip_new:
        return  # closed long ago: it would only be archived again
    page = None
    if link is not None:
        try:
            page = await ctx.api.update_page(uid, link.page_id, properties=props)
        except NotionError as exc:
            if exc.kind not in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT):
                raise
            await notion_links.delete(uid, LINK, str(loop.id))  # the row is gone (trashed): make a new one
    if page is None:
        page = await ctx.api.create_page(
            uid, {"type": "data_source_id", "data_source_id": ctx.home.ds["todos"]}, props)
    await notion_links.upsert(uid, LINK, str(loop.id), page["id"], pushed_hash=digest,
                              pushed_at=timeutil.now(), edited_at=_edited(page), meta={})


# ----- archive

async def _archive(ctx: SyncCtx, cursors: dict) -> bool:
    uid = ctx.user_id
    last = _when(cursors.get("notion_todos_archived"))
    if last is not None and ctx.now - last < ARCHIVE_EVERY:
        return True
    for loop in await loop_repo.list_closed_before(uid, ctx.now - ARCHIVE_AFTER, KINDS):
        link = await notion_links.get(uid, LINK, str(loop.id))
        if link is None:
            continue
        if not ctx.left():
            return False
        try:
            if not link.meta.get("trashed"):
                await ctx.api.update_page(uid, link.page_id, in_trash=True)
        except NotionError as exc:
            if exc.kind is not FailureKind.NOT_FOUND:
                raise
        await notion_links.delete(uid, LINK, str(loop.id))
    await _put(uid, notion_todos_archived=ctx.now.isoformat())
    return True


async def todos_pass(ctx: SyncCtx) -> None:
    uid = ctx.user_id
    service = LoopService(get_bus())
    tz = (await users.get(uid)).timezone
    cursors = await _cursors(uid)
    names = Names(await notion_links.all_of(uid, PERSON_LINK), await notion_links.all_of(uid, PROJECT_LINK))
    if not await _pull(ctx, service, names, tz, cursors):
        return
    if not await _trash_check(ctx, service):
        return
    if not await _push(ctx, names, tz, cursors):
        return
    await _archive(ctx, await _cursors(uid))


register_pass("todos", timedelta(minutes=5), todos_pass, order=40)

# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The "Mavis AI" home page in the user's Notion and its five databases: build, adopt, repair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog

from mavis.domain.errors import FailureKind
from mavis.store.repo import notion_links
from mavis.tools.integrations.native.notion_api import NotionApi, NotionError, notion_id
from mavis.tools.integrations.notion_sync import blocks

log = structlog.get_logger(__name__)

DBS = ("people", "projects", "meetings", "briefings", "todos")
TITLES = {"people": "People", "projects": "Projects", "meetings": "Meetings", "briefings": "Briefings",
          "todos": "Todos"}
HOME_TITLE = "Mavis AI"
DESCRIPTION = "Kept up to date by Mavis AI. Add your own columns freely."
HOME_LINK = ("home", "home")
HomeProblem = Literal["no_page", "gone"]


def _select(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


def _rel(target: str) -> dict:
    return {"relation": {"to": target}}  # resolved to the target's data source id once it exists


_BASE = {"Name": {"title": {}}, "Mavis key": {"rich_text": {}}}
SCHEMAS: dict[str, dict] = {
    "people": {**_BASE, "Email": {"email": {}}, "Last contact": {"date": {}}},
    "projects": {**_BASE, "People": _rel("people")},
    "meetings": {**_BASE, "When": {"date": {}}, "People": _rel("people"), "Recording": {"url": {}}},
    "briefings": {**_BASE, "Date": {"date": {}}},
    "todos": {
        **_BASE,
        "Kind": _select("I owe", "Waiting on them", "Goal"),
        "Status": _select("Open", "Blocked", "Done", "Dropped", "Expired"),
        "Due": {"date": {}},
        "People": _rel("people"), "Project": _rel("projects"), "Meeting": _rel("meetings"),
        "Source": {"rich_text": {}},
    },
}


@dataclass(frozen=True)
class Home:
    page_id: str
    ds: dict[str, str]
    props: dict[str, dict[str, str]]


def _is_relation(spec: dict) -> bool:
    return "relation" in spec


def _resolved(spec: dict, ds: dict[str, str]) -> dict:
    if not _is_relation(spec):
        return spec
    return {"relation": {"data_source_id": ds[spec["relation"]["to"]], "single_property": {}}}


def _gone(ds: dict[str, Any]) -> bool:
    return bool(ds.get("in_trash") or ds.get("is_archived"))


def _page_gone(page: dict[str, Any]) -> bool:
    return bool(page.get("in_trash") or page.get("is_archived"))


async def _reachable(api: NotionApi, user_id: int, page_id: str) -> bool:
    try:
        return not _page_gone(await api.page(user_id, page_id))
    except NotionError as exc:
        if exc.kind is FailureKind.NOT_FOUND:
            return False
        raise


def _props_map(name: str, stored: dict[str, str], live: dict[str, dict]) -> dict[str, str]:
    """Canonical property name -> id. A stored id wins (the user may have renamed the column); a property
    never seen before is matched by its name."""
    present = {p.get("id") for p in live.values()}
    out: dict[str, str] = {}
    for prop in SCHEMAS[name]:
        if stored.get(prop) in present:
            out[prop] = stored[prop]
        elif prop in live and live[prop].get("id") and live[prop]["id"] not in stored.values():
            out[prop] = live[prop]["id"]
    return out


async def _adopt(api: NotionApi, user_id: int, page_id: str) -> Home | HomeProblem:
    links = {name: await notion_links.get(user_id, "db", name) for name in DBS}
    if not all(links.values()):
        return "gone"
    ds_ids = {name: str(links[name].meta.get("ds_id") or "") for name in DBS}
    live: dict[str, dict] = {}
    for name in DBS:
        try:
            ds = await api.data_source(user_id, ds_ids[name])
        except NotionError as exc:
            if exc.kind is FailureKind.NOT_FOUND:
                return "gone"
            raise
        if _gone(ds):
            return "gone"
        live[name] = ds
    props: dict[str, dict[str, str]] = {}
    for name in DBS:
        link = links[name]
        stored = dict(link.meta.get("props") or {})
        found = _props_map(name, stored, live[name].get("properties") or {})
        missing = {p: _resolved(spec, ds_ids) for p, spec in SCHEMAS[name].items() if p not in found}
        if missing:
            ds = await api.update_data_source(user_id, ds_ids[name], missing)
            found = _props_map(name, stored, ds.get("properties") or {})
        props[name] = found
        if found != stored:
            await notion_links.upsert(user_id, "db", name, link.page_id, meta={**link.meta, "props": found})
    return Home(page_id, ds_ids, props)


async def _pick_parent(api: NotionApi, user_id: int, account: dict) -> str | None:
    template = notion_id(str(account.get("template_page_id") or ""))
    if template and await _reachable(api, user_id, template):  # a deleted template copy is not a parent (I2)
        return template
    pages = [p for p in await api.search(user_id, "", "page", 50) if not _page_gone(p) and p.get("id")]
    if not pages:
        return None
    top = [p for p in pages if (p.get("parent") or {}).get("type") == "workspace"]
    return str((top or pages)[0]["id"])  # search is newest edit first


async def _earlier_child(api: NotionApi, user_id: int, parent_id: str, kind: str, title: str,
                         bot_id: str) -> str | None:
    """The id of a child page or database the bot already made under `parent_id` with this title: the
    result of an earlier create whose outcome was never seen (I1)."""
    for block in await api.children(user_id, parent_id, limit=500):
        inner = block.get(kind) if block.get("type") == kind else None
        if not isinstance(inner, dict) or str(inner.get("title") or "").strip() != title:
            continue
        if block.get("in_trash") or (bot_id and (block.get("created_by") or {}).get("id") != bot_id):
            continue
        return str(block.get("id") or "") or None
    return None


async def _create_missing(api: NotionApi, user_id: int, page_id: str, bot_id: str = "") -> None:
    """Create the databases that have no link yet, saving each link the moment its database exists, so a
    retry after a failure continues here instead of starting a second home. An `attempt` link is saved
    before each create: if the create's result was lost, the retry adopts the database it made."""
    for name in DBS:
        if await notion_links.get(user_id, "db", name) is not None:
            continue
        attempt = f"db:{name}"
        found = None
        if await notion_links.get(user_id, "attempt", attempt) is not None:
            found = await _earlier_child(api, user_id, page_id, "child_database", TITLES[name], bot_id)
        if found:
            sources = (await api.database(user_id, found)).get("data_sources") or []
            ds_id = str(sources[0].get("id") or "") if sources and isinstance(sources[0], dict) else ""
            if not ds_id:
                raise NotionError(FailureKind.UNKNOWN, "no_data_source")
            db_id = found
        else:
            await notion_links.upsert(user_id, "attempt", attempt, f"attempt:{attempt}", meta={})
            plain = {p: s for p, s in SCHEMAS[name].items() if not _is_relation(s)}
            db_id, ds_id = await api.create_database(user_id, page_id, TITLES[name], plain, DESCRIPTION)
        await notion_links.upsert(user_id, "db", name, db_id,
                                  meta={"db_id": db_id, "ds_id": ds_id, "props": {}})
        await notion_links.delete(user_id, "attempt", attempt)


async def _finish(api: NotionApi, user_id: int, page_id: str, bot_id: str = "") -> Home | HomeProblem:
    """Build or resume: create what is missing, add the relations (every target exists now) through the
    repair path, then mark the home built."""
    await _create_missing(api, user_id, page_id, bot_id)
    result = await _adopt(api, user_id, page_id)
    if isinstance(result, Home):
        link = await notion_links.get(user_id, *HOME_LINK)
        meta = {**(link.meta if link else {}), "built": True}
        await notion_links.upsert(user_id, *HOME_LINK, page_id, meta=meta)
    return result


async def _start(api: NotionApi, user_id: int, parent_id: str, bot_id: str = "") -> Home | HomeProblem:
    """Create the home page. An `attempt` link is saved first; if the create's result was lost (a 5xx after
    the request left), the next try adopts the page the bot already made instead of making a second (I1)."""
    page_id = None
    if await notion_links.get(user_id, "attempt", "home") is not None:
        page_id = await _earlier_child(api, user_id, parent_id, "child_page", HOME_TITLE, bot_id)
    if page_id is None:
        await notion_links.upsert(user_id, "attempt", "home", "attempt:home", meta={"parent": parent_id})
        page = await api.create_page(
            user_id, {"type": "page_id", "page_id": parent_id},
            {"title": {"title": [{"type": "text", "text": {"content": HOME_TITLE}}]}},
            blocks.para("Mavis AI keeps these databases up to date. Add your own columns and notes freely."))
        page_id = page["id"]
    await notion_links.upsert(user_id, *HOME_LINK, page_id, meta={})
    await notion_links.delete(user_id, "attempt", "home")
    return await _finish(api, user_id, page_id, bot_id)


async def ensure_home(api: NotionApi, user_id: int, account: dict) -> Home | HomeProblem:
    """Adopt the stored home (repairing missing columns, finishing a build that was cut short), else build
    one. "gone": the home page or a database was deleted, left for the user to rebuild. "no_page": the user
    has shared no page to build under."""
    bot_id = str(account.get("bot_user_id") or "")
    link = await notion_links.get(user_id, *HOME_LINK)
    if link is not None:
        if not await _reachable(api, user_id, link.page_id):
            return "gone"
        if link.meta.get("built"):
            return await _adopt(api, user_id, link.page_id)
        return await _finish(api, user_id, link.page_id, bot_id)
    attempt = await notion_links.get(user_id, "attempt", "home")
    # a retry looks where the lost create went, even if another page is the newest shared one now
    parent = str(attempt.meta.get("parent") or "") if attempt else ""
    if parent and not await _reachable(api, user_id, parent):
        parent = ""  # that page was deleted or unshared: choose again, once
        await notion_links.delete(user_id, "attempt", "home")
    parent = parent or await _pick_parent(api, user_id, account)
    if parent is None:
        return "no_page"
    return await _start(api, user_id, parent, bot_id)

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
    if template:
        return template
    pages = [p for p in await api.search(user_id, "", "page", 50) if not _page_gone(p) and p.get("id")]
    if not pages:
        return None
    top = [p for p in pages if (p.get("parent") or {}).get("type") == "workspace"]
    return str((top or pages)[0]["id"])  # search is newest edit first


async def _build(api: NotionApi, user_id: int, parent_id: str) -> Home:
    page = await api.create_page(
        user_id, {"type": "page_id", "page_id": parent_id},
        {"title": {"title": [{"type": "text", "text": {"content": HOME_TITLE}}]}},
        blocks.para("Mavis AI keeps these databases up to date. Add your own columns and notes freely."))
    page_id = page["id"]
    ids: dict[str, tuple[str, str]] = {}
    for name in DBS:
        plain = {p: s for p, s in SCHEMAS[name].items() if not _is_relation(s)}
        ids[name] = await api.create_database(user_id, page_id, TITLES[name], plain, DESCRIPTION)
    ds_ids = {name: ids[name][1] for name in DBS}
    for name in DBS:  # relations need every target to exist first
        rels = {p: _resolved(s, ds_ids) for p, s in SCHEMAS[name].items() if _is_relation(s)}
        if rels:
            await api.update_data_source(user_id, ds_ids[name], rels)
    props: dict[str, dict[str, str]] = {}
    for name in DBS:
        live = (await api.data_source(user_id, ds_ids[name])).get("properties") or {}
        props[name] = _props_map(name, {}, live)
    await notion_links.upsert(user_id, *HOME_LINK, page_id, meta={})
    for name in DBS:
        await notion_links.upsert(user_id, "db", name, ids[name][0],
                                  meta={"db_id": ids[name][0], "ds_id": ds_ids[name], "props": props[name]})
    return Home(page_id, ds_ids, props)


async def ensure_home(api: NotionApi, user_id: int, account: dict) -> Home | HomeProblem:
    """Adopt the stored home (repairing missing columns), else build one. "gone": a database was deleted,
    left for the user to rebuild. "no_page": the user has shared no page to build under."""
    link = await notion_links.get(user_id, *HOME_LINK)
    if link is not None and await _reachable(api, user_id, link.page_id):
        return await _adopt(api, user_id, link.page_id)
    parent = await _pick_parent(api, user_id, account)
    if parent is None:
        return "no_page"
    return await _build(api, user_id, parent)

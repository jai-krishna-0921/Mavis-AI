"""An in-memory Notion (API version 2026-03-11) over httpx.MockTransport.

Covers search, pages, blocks, databases and data sources well enough for the sync and the executor."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

BOT_ID = "bot-0000"
OWNER_ID = "user-owner"
HEADERS = {"Notion-Version": "2026-03-11", "Authorization": "Bearer tok"}
EMPTY_VALUE: dict[str, Any] = {
    "title": [], "rich_text": [], "people": [], "relation": [], "multi_select": [], "files": [],
    "checkbox": False,
}
MAX_CHILDREN = 100
MAX_TEXT = 2000


def _err(status: int, code: str, message: str = "error", headers: dict | None = None) -> httpx.Response:
    body = {"object": "error", "status": status, "code": code, "message": message}
    return httpx.Response(status, json=body, headers=headers)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _too_long(node: Any) -> bool:
    """True when any rich text `content` string exceeds the 2000 char limit."""
    if isinstance(node, dict):
        text = node.get("content")
        if isinstance(text, str) and len(text) > MAX_TEXT:
            return True
        return any(_too_long(v) for v in node.values())
    if isinstance(node, list):
        return any(_too_long(v) for v in node)
    return False


def _plain(rich: list[dict]) -> str:
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "") for r in rich)


class FakeNotion:
    """Routes the Notion endpoints the connector uses; every request is recorded in `requests`.

    Timestamps come from `clock` and are truncated to the minute like the real API. Every write made
    through the API is attributed to the bot (`BOT_ID`); `user_edit` simulates a human edit. A request
    no route answers is recorded in `unrouted` and answered 404 `unrouted_in_fake_notion`."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.block_index: dict[str, dict] = {}
        self.block_parent: dict[str, str] = {}
        self.databases: dict[str, dict] = {}
        self.data_sources: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.unrouted: list[httpx.Request] = []
        self.clock: Callable[[], datetime] = lambda: datetime(2026, 10, 17, 12, 0, 30, tzinfo=UTC)
        self._scripted: list[httpx.Response] = []
        self._n = 0

    # ----- scripting and plumbing

    def script_429(self, n: int = 1, retry_after: int = 1) -> None:
        self._scripted += [
            _err(429, "rate_limited", "slow down", {"Retry-After": str(retry_after)}) for _ in range(n)
        ]

    def script_error(self, status: int, code: str, n: int = 1) -> None:
        self._scripted += [_err(status, code) for _ in range(n)]

    def assert_all_routed(self) -> None:
        assert not self.unrouted, "unrouted requests: " + ", ".join(
            f"{r.method} {r.url.path}" for r in self.unrouted
        )

    def client(self, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        """No default headers: the code under test must send Notion-Version and Authorization itself.
        Self-tests pass `HEADERS`."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self), headers=headers)

    def executor(self, tokens: Any = None):
        """(NotionExecutor, recorded sleeps). Imported lazily: the executor lands after this fake."""
        from mavis.tools.integrations.native.notion import NotionExecutor

        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return NotionExecutor(tokens or _FakeTokens(), self.client(), sleep=sleep), sleeps

    def _id(self, kind: str) -> str:
        self._n += 1
        return f"{kind}-{self._n}"

    def _now(self) -> str:
        return self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00.000Z")

    # ----- direct seeding and simulated human edits

    def user_edit(self, page_id: str, props: dict, editor_id: str = OWNER_ID) -> dict:
        """A human edit: merge property values, bump last_edited_time, set last_edited_by."""
        page = self.pages[page_id]
        problem = self._check_props(page, props)
        assert problem is None, problem
        self._apply_props(page, props)
        page["last_edited_time"] = self._now()
        page["last_edited_by"] = {"object": "user", "id": editor_id}
        return page

    def _check_props(self, page: dict, props: dict) -> str | None:
        """Reason a write is invalid against the data source schema, else None (pages outside a
        data source are not checked)."""
        schema = self._schema_of(page)
        if not schema:
            return None
        for name, value in props.items():
            if name not in schema:
                return f"{name} is not a property that exists."
            ptype = schema[name]["type"]
            if value is not None and ptype not in value:
                return f"{name} is expected to be {ptype}."
        return None

    def _blank(self, definition: dict) -> dict:
        ptype = definition["type"]
        empty = EMPTY_VALUE.get(ptype)
        return {"id": definition["id"], "type": ptype, ptype: [] if isinstance(empty, list) else empty}

    def _apply_props(self, page: dict, props: dict) -> None:
        schema = self._schema_of(page)
        for name, value in props.items():
            if value is None:
                if name in schema:
                    page["properties"][name] = self._blank(schema[name])
                else:
                    page["properties"].pop(name, None)
                continue
            old = page["properties"].get(name, {})
            ptype = schema.get(name, {}).get("type") or old.get("type") or next(iter(value), "rich_text")
            if ptype == "title" or ptype == "rich_text":
                for r in value.get(ptype, []):
                    r.setdefault("type", "text")
                    r.setdefault("plain_text", r.get("text", {}).get("content", ""))
            page["properties"][name] = {
                "id": schema.get(name, {}).get("id") or old.get("id") or name,
                "type": ptype,
                **value,
            }

    def _schema_of(self, page: dict) -> dict:
        ds = self.data_sources.get(page["parent"].get("data_source_id", ""))
        return ds["properties"] if ds else {}

    # ----- transport

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        on_api = request.url.host == "api.notion.com" and request.url.path.startswith("/v1/")
        handler = self._route(request) if on_api else None
        if handler is None:
            self.unrouted.append(request)
            where = f"{request.method} {request.url.path}"
            return _err(404, "unrouted_in_fake_notion", f"no fake route for {where}")
        if "Notion-Version" not in request.headers:
            return _err(400, "missing_version", "Notion-Version header is required.")
        if request.headers["Notion-Version"] != "2026-03-11":
            return _err(400, "invalid_request", "Unsupported Notion-Version.")
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _err(401, "unauthorized", "API token is invalid.")
        if self._scripted:
            return self._scripted.pop(0)
        body = json.loads(request.content) if request.content else {}
        return handler(request, body)

    def _route(self, request: httpx.Request):
        m, path = request.method, request.url.path.removeprefix("/v1")
        table: list[tuple[str, str, Callable]] = [
            ("POST", r"/search", self._search),
            ("GET", r"/users/me", self._me),
            ("POST", r"/pages", self._create_page),
            ("GET", r"/pages/([^/]+)", self._get_page),
            ("PATCH", r"/pages/([^/]+)", self._patch_page),
            ("POST", r"/databases", self._create_database),
            ("GET", r"/data_sources/([^/]+)", self._get_ds),
            ("PATCH", r"/data_sources/([^/]+)", self._patch_ds),
            ("POST", r"/data_sources/([^/]+)/query", self._query),
            ("GET", r"/blocks/([^/]+)/children", self._list_children),
            ("PATCH", r"/blocks/([^/]+)/children", self._append_children),
            ("DELETE", r"/blocks/([^/]+)", self._delete_block),
        ]
        for method, pattern, fn in table:
            hit = re.fullmatch(pattern, path)
            if method == m and hit:
                return lambda req, body, fn=fn, args=hit.groups(): fn(req, body, *args)
        return None

    # ----- handlers

    def _list(self, items: list[dict], req: httpx.Request, body: dict) -> httpx.Response:
        size = int(body.get("page_size") or req.url.params.get("page_size") or 100)
        start = int(body.get("start_cursor") or req.url.params.get("start_cursor") or 0)
        more = start + size < len(items)
        return httpx.Response(200, json={
            "object": "list",
            "results": items[start : start + size],
            "next_cursor": str(start + size) if more else None,
            "has_more": more,
            "type": "page_or_data_source",
            "request_status": {"type": "complete"},
        })

    def _me(self, req, body):
        return httpx.Response(200, json={
            "object": "user", "id": BOT_ID, "type": "bot", "name": "Mavis AI", "avatar_url": None,
            "bot": {
                "owner": {"type": "user", "user": {
                    "object": "user", "id": OWNER_ID, "type": "person", "name": "Owner",
                    "person": {"email": "owner@example.test"}}},
                "workspace_id": "ws-1", "workspace_name": "Workspace",
            },
        })

    def _search(self, req, body):
        want = (body.get("filter") or {}).get("value")
        results: list[dict] = []
        if want in (None, "page"):
            results += [p for p in self.pages.values() if not p["in_trash"]]
        if want in (None, "data_source"):
            results += [d for d in self.data_sources.values() if not d["in_trash"]]
        if query := (body.get("query") or "").lower():
            hay = lambda r: json.dumps(r.get("properties") or r.get("title")).lower()  # noqa: E731
            results = [r for r in results if query in hay(r)]
        sort = body.get("sort") or {}
        if sort.get("timestamp") == "last_edited_time":
            results.sort(key=lambda r: r["last_edited_time"], reverse=sort.get("direction") == "descending")
        return self._list(results, req, body)

    def _page_obj(self, parent: dict, props: dict) -> dict:
        now, who = self._now(), {"object": "user", "id": BOT_ID}
        pid = self._id("page")
        page = {
            "object": "page", "id": pid, "created_time": now, "last_edited_time": now,
            "created_by": who, "last_edited_by": who, "in_trash": False, "is_archived": False,
            "is_locked": False, "parent": parent, "properties": {},
            "url": f"https://www.notion.so/{pid}", "public_url": None, "icon": None, "cover": None,
        }
        self.pages[pid] = page
        page["properties"] = {n: self._blank(d) for n, d in self._schema_of(page).items()}
        self._apply_props(page, props)
        return page

    def _create_page(self, req, body):
        parent = body.get("parent") or {}
        if parent.get("type") == "data_source_id" and parent.get("data_source_id") not in self.data_sources:
            return _err(404, "object_not_found", "data source not found")
        children = body.get("children") or []
        if len(children) > MAX_CHILDREN or _too_long(body):
            return _err(400, "validation_error", "body failed validation")
        schema = self.data_sources.get(parent.get("data_source_id", ""), {}).get("properties", {})
        for name, value in (body.get("properties") or {}).items():
            if schema and (name not in schema or (value is not None and schema[name]["type"] not in value)):
                return _err(400, "validation_error", f"{name} is not a valid property.")
        page = self._page_obj(parent, body.get("properties") or {})
        self.blocks[page["id"]] = []
        self._insert(page["id"], children, {"type": "end"})
        return httpx.Response(200, json=page)

    def _get_page(self, req, body, pid):
        page = self.pages.get(pid)
        return httpx.Response(200, json=page) if page else _err(404, "object_not_found")

    def _patch_page(self, req, body, pid):
        page = self.pages.get(pid)
        if page is None:
            return _err(404, "object_not_found")
        if _too_long(body):
            return _err(400, "validation_error", "body failed validation")
        props = body.get("properties") or {}
        if props and page["in_trash"] and body.get("in_trash") is not False:
            return _err(400, "validation_error", "Can't edit block that is in trash.")
        if problem := self._check_props(page, props):
            return _err(400, "validation_error", problem)
        if "in_trash" in body:
            page["in_trash"] = page["is_archived"] = bool(body["in_trash"])
        self._apply_props(page, props)
        page["last_edited_time"] = self._now()
        page["last_edited_by"] = {"object": "user", "id": BOT_ID}
        return httpx.Response(200, json=page)

    def _prop_def(self, name: str, spec: dict, old: dict | None = None) -> dict:
        out = dict(old or {})
        out.update({k: v for k, v in spec.items()})
        out["name"] = spec.get("name", name)
        out.setdefault("id", self._id("prop"))
        if "type" not in out:
            out["type"] = next((k for k in spec if k not in ("name", "id")), "rich_text")
        if out["type"] == "relation":
            rel = out.setdefault("relation", {})
            rel.setdefault("type", "single_property")
            rel.setdefault("single_property", {})
            rel.setdefault("database_id", self.data_sources.get(rel.get("data_source_id"), {})
                           .get("parent", {}).get("database_id"))
        return out

    def _create_database(self, req, body):
        now = self._now()
        db_id, ds_id = self._id("db"), self._id("ds")
        title = body.get("title") or []
        props = ((body.get("initial_data_source") or {}).get("properties")) or {}
        if not any(p.get("type") == "title" or "title" in p for p in props.values()):
            props = {"Name": {"type": "title", "title": {}}, **props}
        who = {"object": "user", "id": BOT_ID}
        common = {"created_time": now, "last_edited_time": now, "created_by": who, "last_edited_by": who,
                  "in_trash": False, "is_archived": False}
        ds = {"object": "data_source", "id": ds_id, "parent": {"type": "database_id", "database_id": db_id},
              "database_parent": body.get("parent"), "title": title, "properties": {}, **common}
        for name, spec in props.items():
            ds["properties"][name] = self._prop_def(name, spec)
        db = {"object": "database", "id": db_id, "title": title, "parent": body.get("parent"),
              "is_inline": bool(body.get("is_inline")), "url": f"https://www.notion.so/{db_id}",
              "data_sources": [{"id": ds_id, "name": _plain(title)}], **common}
        self.databases[db_id], self.data_sources[ds_id] = db, ds
        return httpx.Response(200, json=db)

    def _get_ds(self, req, body, ds_id):
        ds = self.data_sources.get(ds_id)
        return httpx.Response(200, json=ds) if ds else _err(404, "object_not_found")

    def _patch_ds(self, req, body, ds_id):
        ds = self.data_sources.get(ds_id)
        if ds is None:
            return _err(404, "object_not_found")
        for name, spec in (body.get("properties") or {}).items():
            if spec is None:
                ds["properties"].pop(name, None)
                continue
            old = ds["properties"].get(name)
            new = self._prop_def(name, spec, old)
            if old and new["name"] != name:
                del ds["properties"][name]
            ds["properties"][new["name"]] = new
        for page in self.pages.values():
            if page["parent"].get("data_source_id") != ds_id:
                continue
            old_props, page["properties"] = page["properties"], {}
            by_id = {v["id"]: v for v in old_props.values()}
            for name, definition in ds["properties"].items():
                page["properties"][name] = by_id.get(definition["id"]) or self._blank(definition)
        if "title" in body:
            ds["title"] = body["title"]
        ds["last_edited_time"] = self._now()
        return httpx.Response(200, json=ds)

    def _query(self, req, body, ds_id):
        if ds_id not in self.data_sources:
            return _err(404, "object_not_found")
        pages = [p for p in self.pages.values()
                 if p["parent"].get("data_source_id") == ds_id and not p["in_trash"]]
        flt = body.get("filter")
        if flt:
            rule = flt.get("last_edited_time", {})
            if flt.get("timestamp") != "last_edited_time" or "on_or_after" not in rule:
                return _err(400, "validation_error", "FakeNotion only supports last_edited_time on_or_after")
            floor = _parse(flt["last_edited_time"]["on_or_after"])
            pages = [p for p in pages if _parse(p["last_edited_time"]) >= floor]
        for sort in body.get("sorts") or []:
            if sort.get("timestamp") == "last_edited_time":
                pages.sort(key=lambda p: p["last_edited_time"], reverse=sort.get("direction") == "descending")
        return self._list(pages, req, body)

    def _block(self, spec: dict) -> dict:
        now, who = self._now(), {"object": "user", "id": BOT_ID}
        kind = spec.get("type", "paragraph")
        inner = dict(spec.get(kind, {}))
        nested = inner.pop("children", None)
        block = {"object": "block", "id": self._id("blk"), "type": kind, kind: inner, "created_time": now,
                 "last_edited_time": now, "created_by": who, "last_edited_by": who, "in_trash": False,
                 "has_children": bool(nested)}
        self.block_index[block["id"]] = block
        if nested:
            kids = self.blocks[block["id"]] = [self._block(c) for c in nested]
            for kid in kids:
                self.block_parent[kid["id"]] = block["id"]
        return block

    def _owning_page(self, node_id: str) -> dict | None:
        while node_id not in self.pages:
            node_id = self.block_parent.get(node_id, "")
            if not node_id:
                return None
        return self.pages[node_id]

    def _stamp(self, node_id: str) -> None:
        if page := self._owning_page(node_id):
            page["last_edited_time"] = self._now()
            page["last_edited_by"] = {"object": "user", "id": BOT_ID}

    def _insert(self, parent: str, children: list[dict], position: dict) -> list[dict] | None:
        siblings = self.blocks.setdefault(parent, [])
        new = [self._block(c) for c in children]
        at = len(siblings)
        if position.get("type") == "start":
            at = 0
        elif position.get("type") == "after_block":
            anchor = (position.get("after_block") or {}).get("id")
            ids = [b["id"] for b in siblings]
            if anchor not in ids:
                return None
            at = ids.index(anchor) + 1
        siblings[at:at] = new
        for block in new:
            self.block_parent[block["id"]] = parent
        return new

    def _append_children(self, req, body, parent):
        children = body.get("children") or []
        if len(children) > MAX_CHILDREN or _too_long(children):
            return _err(400, "validation_error", "body failed validation")
        if not self._known_parent(parent):
            return _err(404, "object_not_found", "block not found")
        new = self._insert(parent, children, body.get("position") or {"type": "end"})
        if new is None:
            return _err(404, "object_not_found", "after_block not found")
        self._stamp(parent)
        return httpx.Response(200, json={"object": "list", "results": new, "next_cursor": None,
                                         "has_more": False, "type": "block"})

    def _known_parent(self, node_id: str) -> bool:
        block = self.block_index.get(node_id)
        return node_id in self.pages or (block is not None and not block["in_trash"])

    def _list_children(self, req, body, parent):
        if not self._known_parent(parent):
            return _err(404, "object_not_found", "block not found")
        items = [b for b in self.blocks.get(parent, []) if not b["in_trash"]]
        return self._list(items, req, {})

    def _delete_block(self, req, body, bid):
        block = self.block_index.get(bid)
        if block is None:
            return _err(404, "object_not_found")
        block["in_trash"] = True
        self._stamp(bid)
        for siblings in self.blocks.values():
            if block in siblings:
                siblings.remove(block)
        return httpx.Response(200, json=block)


class _FakeTokens:
    async def access_token(self, user_id: int, provider: Any, *, force: bool = False) -> str:
        return "tok-forced" if force else "tok"

    async def account(self, user_id: int, provider: Any) -> dict | None:
        return {"owner_id": OWNER_ID, "bot_id": BOT_ID}


__all__ = ["BOT_ID", "OWNER_ID", "FakeNotion"]

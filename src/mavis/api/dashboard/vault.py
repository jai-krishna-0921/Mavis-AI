"""The Vault page: what Mavis knows about the session user, with edit and forget. A thin mapping over
mavis.memory.vault.Vault (which resolves every item id inside the caller's own data) and the preferences
setters for the profile card."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from mavis.access import preferences
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.memory.vault import VaultError, VaultItem, get_vault
from mavis.store.repo import personal as personal_repo
from mavis.web import emails
from mavis.web.sessions import Active

router = APIRouter(prefix="/vault")
log = structlog.get_logger(__name__)

PAGE = 25
MAX_ITEMS = 500
UI_KINDS = ("person", "organisation", "project", "fact", "preference")
SOURCES = ("gmail", "slack", "calendar")  # connectors whose learning can be forgotten as a whole
_ORG_RELS = frozenset({"WORKS_AT", "STUDIES_AT"})
_PREF_RELS = frozenset({"PREFERS", "DISLIKES", "STRUGGLES_WITH"})
_PREF_PROFILE = frozenset({"tone", "brevity", "dislikes", "other"})
_PROJECT_PROFILE = frozenset({"goals"})
_TRUST = {"user": "user", "self": "high", "third_party": "low"}


def ui_kind(item: VaultItem) -> str | None:
    """Which Vault tab an item belongs to; None when the page does not show it."""
    if item.kind == "person":
        return "person"
    if item.kind in ("fact", "signal"):
        rel = str(item.meta.get("relation", ""))
        if rel in _ORG_RELS:
            return "organisation"
        return "preference" if rel in _PREF_RELS or item.section == "preferences" else "fact"
    if item.kind == "profile":
        field = str(item.meta.get("field", ""))
        return "preference" if field in _PREF_PROFILE else "project" if field in _PROJECT_PROFILE else "fact"
    if item.kind == "layer":
        return "project" if item.section == "projects" else None
    if item.kind == "routine":
        return "fact"
    return None


def source_of(item: VaultItem) -> str:
    if item.connector:
        return item.connector
    return "dashboard" if any(s.startswith("vault:") for s in item.sources) else "you"


def to_ui(item: VaultItem, kind: str | None = None) -> dict[str, Any]:
    return {"id": item.id, "kind": kind or ui_kind(item) or "fact", "title": item.text, "detail": None,
            "source": source_of(item), "trust": _TRUST.get(item.trust, "low"), "updated_at": None}


async def all_items(user_id: int) -> list[tuple[str, VaultItem]]:
    """Every item of the user's Vault with its tab, built from the user's own data only."""
    vault = get_vault()
    out: list[tuple[str, VaultItem]] = []
    seen: set[str] = set()
    for vk in ("person", "fact", "signal", "profile", "layer", "routine"):
        for item in await vault.list_items(user_id, vk, limit=MAX_ITEMS):
            kind = ui_kind(item)
            if kind is not None and item.id not in seen:
                seen.add(item.id)
                out.append((kind, item))
    return out


@router.get("/summary")
async def summary(active: Active = Depends(common.authed)) -> dict:
    user = active.user
    items = await all_items(user.id)
    n = lambda k: sum(1 for kind, _ in items if kind == k)  # noqa: E731
    sources = {source_of(i) for _, i in items if source_of(i) in SOURCES}
    profile = {"name": user.name or "", "timezone": user.timezone, "currency": user.currency or ""}
    if email := await emails.primary(user.id):
        profile["email"] = email
    return {"profile": profile, "counts": {
        "people": n("person"), "organisations": n("organisation"), "projects": n("project"),
        "facts": n("fact") + n("preference"), "sources": len(sources)}}


@router.get("/items")
async def list_items(kind: str = Query(max_length=16), q: str = Query(default="", max_length=100),
                     cursor: str | None = Query(default=None, max_length=12),
                     active: Active = Depends(common.authed)) -> dict:
    if kind not in UI_KINDS:
        raise DashError(400, "bad_kind", "Unknown kind of item.")
    if cursor is not None and not (cursor.isascii() and cursor.isdigit()):
        raise DashError(400, "bad_cursor", "Unknown page.")
    start = int(cursor or 0)
    needle = q.strip().casefold()
    rows = [to_ui(i, k) for k, i in reversed(await all_items(active.user.id))
            if k == kind and (not needle or needle in i.text.casefold())]
    page = rows[start:start + PAGE]
    return {"items": page, "next_cursor": str(start + PAGE) if start + PAGE < len(rows) else None}


class PatchBody(BaseModel):
    title: str | None = Field(default=None, max_length=400)
    detail: str | None = Field(default=None, max_length=400)


def _text(body: PatchBody) -> str:
    title, detail = (body.title or "").strip(), (body.detail or "").strip()
    if title and detail:
        return f"{title}{'' if title[-1] in '.!?' else '.'} {detail}"
    return title or detail


async def _patch_profile(user_id: int, field: str, value: str) -> dict:
    try:
        if field == "name":
            value = await preferences.set_name(user_id, value)
        elif field == "timezone":
            value = (await preferences.set_timezone(user_id, value)).new
        elif field == "currency":
            value = await preferences.set_currency(user_id, value)
        else:
            raise common.not_found("That profile field")
    except ValueError:
        raise DashError(400, "invalid_value", f"That is not a valid {field}.") from None
    return {"id": f"profile.{field}", "kind": "fact", "title": value, "detail": None, "source": "dashboard",
            "trust": "user", "updated_at": None}


@router.patch("/items/{item_id:path}")
async def patch_item(item_id: str, body: PatchBody, active: Active = Depends(common.authed)) -> dict:
    text = _text(body)
    if not text:
        raise DashError(400, "empty", "Write something first.")
    if item_id.startswith("profile."):
        return await _patch_profile(active.user.id, item_id[len("profile."):], text)
    try:
        item = await get_vault().correct_item(active.user.id, item_id, text)
    except VaultError as exc:
        raise DashError(404 if "not found" in str(exc).lower() else 400, "invalid_item", str(exc)) from None
    return to_ui(item)


class ForgetBody(BaseModel):
    also_suppress: bool = False


@router.delete("/items/{item_id:path}", status_code=204)
async def forget_item(item_id: str, body: ForgetBody | None = None,
                      active: Active = Depends(common.authed)) -> Response:
    if item_id not in {i.id for _, i in await all_items(active.user.id)}:
        raise common.not_found("That item")  # also what another user's id looks like
    try:
        await get_vault().forget_item(active.user.id, item_id, suppress=bool(body and body.also_suppress))
    except VaultError as exc:
        raise DashError(400, "invalid_item", str(exc)) from None
    return Response(status_code=204)


@router.get("/sources")
async def sources(active: Active = Depends(common.authed)) -> list[dict]:
    items = await all_items(active.user.id)
    counts: dict[str, int] = {}
    for _, i in items:
        counts[source_of(i)] = counts.get(source_of(i), 0) + 1
    signals = [*await personal_repo.signals(active.user.id, "interaction"),
               *await personal_repo.signals(active.user.id, "meeting")]
    out = []
    for source, count in counts.items():
        stamps = [s.at for s in signals if s.source_ref.startswith(f"{source}:")]
        out.append({"source": source, "count": count,
                    "last_sync": max(stamps).isoformat() if stamps else None})
    return out


@router.post("/sources/{source}/forget", status_code=204)
async def forget_source(source: str, active: Active = Depends(common.authed)) -> Response:
    if source not in SOURCES:
        raise common.not_found("That source")
    await get_vault().forget_source(active.user.id, source)
    return Response(status_code=204)

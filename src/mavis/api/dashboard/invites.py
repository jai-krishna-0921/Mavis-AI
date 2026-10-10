"""Invite links a person makes from the dashboard. Reuses the invite code store; the owner has no cap."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from mavis.access import UserTier
from mavis.access.codes import InviteError, deep_link_param
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.config import get_settings
from mavis.store.models import InviteCode, User
from mavis.store.repo import invites
from mavis.web.sessions import Active

router = APIRouter(prefix="/invites")


def cap_for(user: User) -> int | None:
    """Open links this user may have at once; None for the owner (unlimited)."""
    s = get_settings()
    if user.tier == UserTier.OWNER.value:
        return None
    return s.invite_cap_trusted if user.tier == UserTier.TRUSTED.value else s.invite_cap_standard


async def left_for(user: User) -> int | None:
    cap = cap_for(user)
    return None if cap is None else max(cap - len(await invites.list_by_creator(user.id)), 0)


def _row(row: InviteCode, link: str | None = None, code: str | None = None) -> dict:
    return {"code": code or str(row.id), "link": link, "name": row.label or None, "uses": row.uses,
            "max_uses": row.max_uses, "hint": row.code_hint}


class CreateBody(BaseModel):
    name: str | None = Field(default=None, max_length=60)


@router.get("")
async def list_invites(active: Active = Depends(common.authed)) -> list[dict]:
    return [_row(r) for r in await invites.list_by_creator(active.user.id)]


@router.post("", status_code=201)
async def create_invite(body: CreateBody, active: Active = Depends(common.authed)) -> dict:
    user = active.user
    s = get_settings()
    try:
        label = " ".join((body.name or "").split())[:60]
        row, plain = await invites.mint(created_by=user.id, uses=s.invite_web_uses,
                                        tier=UserTier.STANDARD.value, label=label, creator_cap=cap_for(user))
    except InviteError as exc:
        if exc.reason == "cap":
            raise DashError(409, "invite_cap",
                            "You have used all your invite links. Revoke one to make another.") from None
        raise DashError(409, "invite_cap", "Mavis has too many open invites right now. "
                                           "Try again later.") from None
    link = f"{s.public_base_url.rstrip('/')}/login?invite={deep_link_param(plain)}"
    return _row(row, link=link, code=str(row.id))


@router.delete("/{invite_id}", status_code=204)
async def revoke_invite(invite_id: str, active: Active = Depends(common.authed)) -> Response:
    if not invite_id.isascii() or not invite_id.isdigit() or await invites.revoke_own(
            active.user.id, int(invite_id)) is None:
        raise common.not_found("That invite link")
    return Response(status_code=204)

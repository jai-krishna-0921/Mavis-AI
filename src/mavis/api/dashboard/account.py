"""Account deletion: the existing DELETE_USER job, started after a typed confirmation."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mavis.access import deletion
from mavis.api.dashboard import common
from mavis.api.dashboard.common import DashError
from mavis.web import sessions
from mavis.web.sessions import Active

router = APIRouter(prefix="/account")


class DeleteBody(BaseModel):
    confirm: str = ""


@router.post("/delete", status_code=202)
async def delete_account(body: DeleteBody, active: Active = Depends(common.authed)) -> JSONResponse:
    if body.confirm != "DELETE":
        raise DashError(400, "confirm", "Type DELETE to confirm.")
    await deletion.request_deletion(active.user.id)  # marks the user deleting and ends every session
    out = JSONResponse({"status": "deleting"}, status_code=202)
    common.clear_cookie(out, sessions.COOKIE)
    return out

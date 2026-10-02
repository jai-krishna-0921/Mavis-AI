"""Where the provider's consent screen sends the browser back. Writes no state; it only nudges a check."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from mavis.bus import get_bus
from mavis.bus.base import EventBus
from mavis.domain import timeutil
from mavis.domain.events import Job, JobKind
from mavis.store.repo import connections

router = APIRouter()

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connected</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
background:#0f1115;color:#e8e8e8}main{text-align:center;padding:24px}h1{font-size:22px}</style></head>
<body><main><h1>Connected ✓</h1>
<p>All set, you can close this tab and head back to Telegram.</p></main></body></html>"""


@router.get("/connect/callback", response_class=HTMLResponse)
async def connect_callback(p: str | None = None, bus: EventBus = Depends(get_bus)) -> HTMLResponse:
    pending_id = int(p) if p is not None and p.isascii() and p.isdigit() else None
    if pending_id is not None:
        pending = await connections.get_pending(pending_id)
        if pending is not None:
            await bus.enqueue(Job(
                id=f"conncheck:{pending_id}:{int(timeutil.now().timestamp()) // 10}",
                user_id=pending.user_id, kind=JobKind.CONNECTION_CHECK, payload={"pending_id": pending_id},
            ))
    return HTMLResponse(_PAGE)

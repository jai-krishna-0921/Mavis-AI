"""The chat agent's handles on the studio: create_document starts the studio sub-agent as a background job
(the async subagent pattern: start, reply at once, deliver when done) and brand reads or sets the look
every artifact is drawn in."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from mavis.bus import get_bus
from mavis.domain.args import ToolArgs
from mavis.domain.events import Job, JobKind
from mavis.domain.policy import RiskClass
from mavis.studio import brand as brand_store
from mavis.studio.themes import SAFE_FONTS
from mavis.tools.registry import MavisTool

_CONV = frozenset({"conversation"})


class CreateDocumentArgs(ToolArgs):
    kind: Literal["deck", "doc", "sheet"] = Field(
        description="deck (slides), doc (report, proposal, notes) or sheet (table, tracker, budget)"
    )
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(
        min_length=10,
        max_length=8000,
        description=(
            "Everything the studio needs, since it does not see this chat: purpose, audience, the points, "
            "names, numbers and dates to use (copy them from the chat and from what you read), the length"
        ),
    )
    format: Literal["pptx", "docx", "xlsx", "pdf", "google"] | None = Field(
        default=None,
        description=(
            "pdf to get a PDF; google for a Google Slides, Doc or Sheet in Drive only; default is the Office "
            "file (pptx, docx, xlsx)"
        ),
    )
    source_file_ids: list[str] = Field(
        default_factory=list, max_length=5, description="Drive file ids whose content the studio should use"
    )


class BrandArgs(ToolArgs):
    style: Literal["minimal", "corporate", "bold", "warm", "editorial", ""] | None = None
    company: str | None = Field(default=None, max_length=80)
    primary: str | None = Field(default=None, description="#RRGGBB")
    secondary: str | None = Field(default=None, description="#RRGGBB")
    accent: str | None = Field(default=None, description="#RRGGBB")
    heading_font: str | None = Field(default=None, description=f"One of: {', '.join(SAFE_FONTS)}")
    body_font: str | None = Field(default=None, description=f"One of: {', '.join(SAFE_FONTS)}")
    tone: str | None = Field(default=None, max_length=80, description="e.g. formal, friendly, crisp")


async def create_document(user_id: int, args: CreateDocumentArgs) -> str:
    job_id = f"studio:{user_id}:{uuid.uuid4().hex[:12]}"
    await get_bus().enqueue(
        Job(
            id=job_id,
            user_id=user_id,
            kind=JobKind.STUDIO,
            payload={
                "kind": args.kind,
                "title": args.title,
                "brief": args.brief,
                "format": args.format or "",
                "source_file_ids": list(args.source_file_ids),
            },
        )
    )
    return (
        f"STARTED: the studio is designing '{args.title}'. It arrives in this chat by itself in a minute "
        "or two, with its Drive link. Tell them briefly; don't describe it as done or list its contents."
    )


async def brand(user_id: int, args: BrandArgs) -> str:
    fields = {k: v for k, v in args.model_dump().items() if v is not None}
    current = (
        await brand_store.set_brand(user_id, **fields) if fields else await brand_store.get_brand(user_id)
    )
    if not current:
        return (
            "No brand saved yet: the first designed file will pick a style that fits, and later ones match."
        )
    return "Brand: " + ", ".join(f"{k}={v}" for k, v in current.items())


TOOLS = [
    MavisTool(
        "create_document",
        (
            "Design and make a polished presentation, document or spreadsheet in the user's brand (the "
            "studio "
            "sub-agent writes it with design skills and renders it), saved to their Drive and sent here as "
            "PPTX, DOCX, XLSX or PDF. Use for anything they will present, share or print: decks, reports, "
            "proposals, plans, trackers, budgets. Use docs_create only for a quick plain note. For several "
        "files at once, or research first and then a file, use start_task: its planner runs a sub-agent "
        "per piece in parallel and hands the findings to studio sub-agents."
        ),
        CreateDocumentArgs,
        RiskClass.WRITE_SELF,
        create_document,
        _CONV,
        priority=75,
    ),
    MavisTool(
        "brand",
        (
            "Show or change the look of every file the studio makes for them: style (minimal, corporate, "
            "bold, warm, editorial), company name, colours, fonts and tone. No arguments shows it."
        ),
        BrandArgs,
        RiskClass.WRITE_SELF,
        brand,
        _CONV,
        priority=40,
    ),
]

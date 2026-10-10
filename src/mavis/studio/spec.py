"""What the studio agent writes and the renderers draw: a structured spec per artifact kind.

The model never writes layout code or file bytes. It writes one of these specs (validated by pydantic), guided
by the design skills (mavis/studio/skills/*/SKILL.md), and a deterministic renderer turns it into a PPTX, DOCX
or XLSX in the user's theme (mavis/studio/themes.py). Limits keep a spec renderable and a deck readable.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Text = Annotated[str, Field(max_length=400)]
Short = Annotated[str, Field(max_length=120)]


# --- slides ------------------------------------------------------------------------------------------


class Stat(BaseModel):
    value: Short = Field(description="The number or short figure, e.g. '18 lakh', '3x', '92%'")
    label: Short = Field(description="What it measures, a few words")


class Step(BaseModel):
    when: Short = Field(description="A date, week or phase name")
    what: Text


class Series(BaseModel):
    name: Short
    values: list[float] = Field(min_length=1, max_length=24)


class Chart(BaseModel):
    kind: Literal["bar", "column", "line", "pie", "doughnut"] = "column"
    categories: list[Short] = Field(min_length=1, max_length=24)
    series: list[Series] = Field(min_length=1, max_length=4)
    number_format: Short = Field(default="", description="e.g. '#,##0', '0%', '₹#,##0'")


class Slide(BaseModel):
    """One slide. `layout` picks the design; fill only the fields that layout uses."""

    layout: Literal[
        "title",        # opening: title + subtitle
        "section",      # chapter divider: title (+ subtitle)
        "bullets",      # title + 2 to 5 short bullets
        "two_column",   # title + left/right columns (heading + bullets each): compare, before/after
        "stats",        # title + 2 to 4 big numbers with labels
        "quote",        # one strong sentence + attribution
        "timeline",     # title + 3 to 6 steps
        "chart",        # title + one native chart + optional takeaway line
        "table",        # title + a small table (max 6 rows x 5 cols)
        "closing",      # thank you / next steps / contact
    ]
    title: Short = ""
    subtitle: Text = ""
    bullets: list[Text] = Field(default_factory=list, max_length=6)
    left_heading: Short = ""
    left: list[Text] = Field(default_factory=list, max_length=6)
    right_heading: Short = ""
    right: list[Text] = Field(default_factory=list, max_length=6)
    stats: list[Stat] = Field(default_factory=list, max_length=4)
    quote: Text = ""
    attribution: Short = ""
    steps: list[Step] = Field(default_factory=list, max_length=6)
    chart: Chart | None = None
    takeaway: Text = Field(default="", description="One line under a chart or table: what it means")
    table_header: list[Short] = Field(default_factory=list, max_length=5)
    table_rows: list[list[Short]] = Field(default_factory=list, max_length=6)
    notes: Annotated[str, Field(max_length=2000)] = Field(default="", description="Speaker notes")


class DeckSpec(BaseModel):
    title: Short
    subtitle: Text = ""
    slides: list[Slide] = Field(min_length=1, max_length=20)


# --- documents ---------------------------------------------------------------------------------------


class Block(BaseModel):
    """One block of a document section."""

    kind: Literal["paragraph", "bullets", "numbered", "table", "callout", "quote"]
    text: Annotated[str, Field(max_length=4000)] = ""
    items: list[Text] = Field(default_factory=list, max_length=20)
    table_header: list[Short] = Field(default_factory=list, max_length=8)
    table_rows: list[list[Short]] = Field(default_factory=list, max_length=40)


class Section(BaseModel):
    heading: Short
    level: Literal[1, 2] = 1
    blocks: list[Block] = Field(default_factory=list, max_length=20)


class DocSpec(BaseModel):
    title: Short
    subtitle: Text = ""
    cover: bool = Field(default=True, description="A cover block with title, subtitle, author and date")
    summary: Annotated[str, Field(max_length=1500)] = Field(default="", description="Key takeaways first")
    sections: list[Section] = Field(min_length=1, max_length=30)


# --- spreadsheets ------------------------------------------------------------------------------------


class Column(BaseModel):
    name: Short
    kind: Literal["text", "number", "currency", "percent", "date"] = "text"
    currency: Annotated[str, Field(max_length=4)] = Field(default="", description="e.g. '₹', '$'")
    total: bool = Field(default=False, description="Sum this column in a total row")


class Tab(BaseModel):
    name: Annotated[str, Field(max_length=31)]
    columns: list[Column] = Field(min_length=1, max_length=20)
    rows: list[list[str | float | int | None]] = Field(default_factory=list, max_length=500)
    chart: Chart | None = Field(default=None, description="Optional chart drawn beside the table")


class SheetSpec(BaseModel):
    title: Short
    tabs: list[Tab] = Field(min_length=1, max_length=6)


Kind = Literal["deck", "doc", "sheet"]
SPECS: dict[str, type[BaseModel]] = {"deck": DeckSpec, "doc": DocSpec, "sheet": SheetSpec}

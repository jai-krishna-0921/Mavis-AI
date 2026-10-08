from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class PlanStep(BaseModel):
    id: str = Field(description="Short id like 's1'")
    agent: str = Field(description="Specialist name or 'spawn'")
    instruction: str
    depends_on: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="Only for agent='spawn': tool names allowed")
    title: str = Field(default="", description="Short plain title for the progress card, at most 60 chars")

    @field_validator("title", mode="before")
    @classmethod
    def _short_title(cls, v: object) -> str:
        return " ".join(str(v or "").split())[:60]


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep]
    deliverable: str = Field(default="message", description="message | pptx | pdf | docx | xlsx | chart")


class CriticVerdict(BaseModel):
    accept: bool
    revise_steps: list[str] = Field(default_factory=list)
    feedback: str = ""


class SlideSpec(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    notes: str = ""
    visual_hint: str | None = None


class DeckOutline(BaseModel):
    title: str
    subtitle: str = ""
    slides: list[SlideSpec]


class DocSection(BaseModel):
    heading: str
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    table: list[list[str]] | None = None


class DocOutline(BaseModel):
    title: str
    sections: list[DocSection]

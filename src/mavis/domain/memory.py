from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

NODE_LABELS = ("User", "Person", "Organization", "Project", "Goal", "Event", "Topic", "Place", "Preference")
REL_TYPES = (
    "KNOWS", "FAMILY_OF", "FRIEND_OF", "COLLEAGUE_OF", "WORKS_AT", "STUDIES_AT", "PURSUING", "SUPPORTS",
    "WITH", "ABOUT", "PREFERS", "DISLIKES", "STRUGGLES_WITH", "SKILLED_AT", "LOCATED_IN", "ATTENDED",
    "INTERVIEWING_AT", "RELATED_TO",
)
SINGLE_VALUED_RELS = frozenset({"WORKS_AT", "STUDIES_AT", "LOCATED_IN"})


class Entity(BaseModel):
    name: str
    label: str = Field(description=f"One of {NODE_LABELS}")
    aliases: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    subject: str = Field(description="Entity name; use 'User' for the user themself")
    rel: str = Field(description=f"One of {REL_TYPES}")
    object: str
    statement: str = Field(description="The fact as one natural sentence")
    confidence: float = Field(ge=0, le=1, default=0.8)


class ExtractedEvent(BaseModel):
    title: str
    starts_at: datetime | None = Field(description="ISO-8601 with offset; null if time unknown/ambiguous")
    ambiguous: bool = Field(default=False, description="True if the time could mean two different days")
    with_people: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5, default=3)


class LoopDraft(BaseModel):
    kind: str = Field(description="COMMITMENT | WAITING_ON | GOAL | CONCERN | ROUTINE | WATCH")
    title: str
    due_at: datetime | None = None
    entities: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5, default=3)


class ProfileUpdate(BaseModel):
    field: str = Field(description="name | timezone | tone | goals | key_people | routines | dislikes | other")  # noqa: E501
    value: str


class Extraction(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    loops: list[LoopDraft] = Field(default_factory=list)
    profile_updates: list[ProfileUpdate] = Field(default_factory=list)
    mood: str | None = None


class RecallContext(BaseModel):
    profile: str = ""
    loops: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    episodes: list[str] = Field(default_factory=list)
    untrusted: bool = False  # some included item is third-party derived (a signal, an untrusted loop)

    def render(self) -> str:
        parts: list[str] = []
        if self.profile:
            parts.append(f"## About the user\n{self.profile}")
        if self.loops:
            parts.append("## Open loops\n" + "\n".join(f"- {x}" for x in self.loops))
        if self.facts:
            parts.append("## Known facts\n" + "\n".join(f"- {x}" for x in self.facts))
        if self.episodes:
            parts.append("## Related past moments\n" + "\n".join(f"- {x}" for x in self.episodes))
        return "\n\n".join(parts)

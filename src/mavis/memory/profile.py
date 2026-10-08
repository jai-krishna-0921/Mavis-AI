"""The profile card: a short, always-in-prompt description of who the user is."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from mavis.domain import timeutil
from mavis.domain.memory import ProfileUpdate
from mavis.memory.tokens import estimate_tokens

LIST_FIELDS = ("goals", "key_people", "routines", "dislikes", "other")
SCALAR_FIELDS = ("name", "timezone", "tone")
MAX_ITEMS = 8
MAX_TOKENS = 400
_TITLES = {
    "goals": "Goals",
    "key_people": "Key people",
    "routines": "Routines",
    "dislikes": "Dislikes",
    "other": "Other",
}


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


class ProfileCard(BaseModel):
    version: int = 0
    name: str | None = None
    timezone: str | None = None
    tone: str | None = None
    goals: list[str] = Field(default_factory=list)
    key_people: list[str] = Field(default_factory=list)
    routines: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)
    flags: dict[str, bool] = Field(default_factory=dict)
    # when each scalar field was last set, as said (ISO UTC): a LEARN deferred by a busy model must not
    # overwrite a newer value with an older statement
    stamps: dict[str, str] = Field(default_factory=dict)

    @property
    def tracks_mood(self) -> bool:
        return self.flags.get("track_mood", True)

    def apply(self, updates: list[ProfileUpdate], at: datetime | None = None) -> ProfileCard:
        """`at`: when the updates were said (default now). A scalar field already set from a later
        statement keeps its value."""
        data = self.model_dump()
        when = (at or timeutil.now())
        when_s = timeutil.ensure_utc(when).isoformat()
        for u in updates:
            value = " ".join(u.value.split())
            if not value:
                continue
            field = u.field.strip().casefold()
            if field in SCALAR_FIELDS and data["stamps"].get(field, "") > when_s:
                continue  # said before the value we hold: history, not an update
            if field == "timezone":
                try:
                    ZoneInfo(value)
                except (ZoneInfoNotFoundError, ValueError):
                    continue
                data["timezone"] = value
                data["stamps"][field] = when_s
            elif field in SCALAR_FIELDS:
                data[field] = value
                data["stamps"][field] = when_s
            else:
                target = field if field in LIST_FIELDS else "other"
                existing = next((x for x in data[target] if _norm(x) == _norm(value)), None)
                items = [x for x in data[target] if x != existing]
                items.append(existing or value)  # a repeat keeps its original spelling, refreshed as newest
                data[target] = items[-MAX_ITEMS:]
        return ProfileCard.model_validate(data)

    def remove_matching(self, needle: str) -> tuple[ProfileCard, bool]:
        n = needle.casefold().strip()
        if not n:
            return self, False
        data, changed = self.model_dump(), False
        for f in LIST_FIELDS:
            kept = [x for x in data[f] if n not in x.casefold()]
            changed |= len(kept) != len(data[f])
            data[f] = kept
        return ProfileCard.model_validate(data), changed

    def _lines(self, lists: dict[str, list[str]]) -> list[str]:
        lines = []
        if self.name:
            lines.append(f"Name: {self.name}")
        if self.timezone:
            lines.append(f"Timezone: {self.timezone}")
        if self.tone:
            lines.append(f"Prefers tone: {self.tone}")
        for f in LIST_FIELDS:
            if lists[f]:
                lines.append(f"{_TITLES[f]}: " + "; ".join(lists[f]))
        return lines

    def render(self, max_tokens: int = MAX_TOKENS) -> str:
        lists = {f: list(getattr(self, f)) for f in LIST_FIELDS}
        text = "\n".join(self._lines(lists))
        while estimate_tokens(text) > max_tokens and any(lists.values()):
            longest = max(LIST_FIELDS, key=lambda f: sum(len(x) for x in lists[f]))
            lists[longest].pop(0)  # oldest first
            text = "\n".join(self._lines(lists))
        return text[: max_tokens * 4]

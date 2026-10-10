"""Each user's brand: the theme base and overrides every artifact they get is drawn in.

Stored on the user row (state["studio_brand"]). Set by the user ("use navy and gold, Montserrat") through
the set_brand tool, or, when they never set one, fixed by their first artifact's style so later ones match.
"""

from __future__ import annotations

from typing import Any

from mavis.store.repo import users
from mavis.studio.themes import THEMES, Theme, theme_for

KEY = "studio_brand"
FIELDS = ("style", "company", "primary", "secondary", "accent", "dark", "heading_font", "body_font", "tone")


async def get_brand(user_id: int) -> dict[str, str]:
    state = await users.get_state(user_id)
    raw = state.get(KEY) or {}
    return {k: str(v) for k, v in raw.items() if k in FIELDS and v}


async def set_brand(user_id: int, **fields: Any) -> dict[str, str]:
    """Merge the given fields (empty values clear a field) and return the brand."""

    def change(cur: dict) -> dict:
        out = dict(cur)
        for key, value in fields.items():
            if key not in FIELDS or value is None:
                continue
            if value == "":
                out.pop(key, None)
            else:
                out[key] = str(value)[:80]
        return out

    await users.modify_nested(user_id, KEY, change)
    return await get_brand(user_id)


def theme_of(brand: dict[str, str], chosen_style: str = "") -> Theme:
    base = brand.get("style") or chosen_style or "minimal"
    if base not in THEMES:
        base = "minimal"
    overrides = {k: v for k, v in brand.items() if k not in ("style", "tone")}
    return theme_for(base, **overrides)

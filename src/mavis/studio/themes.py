"""Design themes: the colours, fonts and style a renderer applies. A user's brand (mavis/studio/brand.py)
overrides a built-in theme field by field, so every artifact a user gets looks like theirs.

Fonts are limited to ones Google Slides and Docs render after import (and Office has or substitutes well):
anything else falls back silently and breaks the layout.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Google Fonts (Google Slides and Docs render them after import; PowerPoint substitutes when missing).
# Inter, Roboto and Open Sans are left out on purpose: they are the generic default look.
SAFE_FONTS = ("Manrope", "DM Sans", "Plus Jakarta Sans", "Space Grotesk", "Outfit", "Fraunces", "Lora",
              "Playfair Display", "Source Sans Pro", "Merriweather", "Lato", "Montserrat", "Poppins",
              "Georgia", "Arial", "Calibri")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
Style = Literal["minimal", "bold", "corporate", "warm", "editorial"]


class Theme(BaseModel):
    name: str
    style: Style = "minimal"
    background: str = "#FFFFFF"   # slide / page background
    surface: str = "#F7F6F3"      # cards, table bands, callouts (warm, not grey-blue)
    text: str = "#2F3437"         # body text: off-black, never pure black
    muted: str = "#787774"        # captions, labels, footers
    primary: str = "#1F1F1F"      # titles, chart series 1, key shapes
    secondary: str = "#5B7A6A"    # chart series 2, highlights
    accent: str = "#B4542D"       # one emphasis colour, used sparingly
    dark: str = "#191919"         # title / section / closing slide background
    heading_font: str = "Manrope"
    body_font: str = "DM Sans"
    company: str = Field(default="", description="Shown in footers when set")

    @field_validator("background", "surface", "text", "muted", "primary", "secondary", "accent", "dark")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX.match(v):
            raise ValueError("colours are #RRGGBB")
        return v.upper()

    @field_validator("heading_font", "body_font")
    @classmethod
    def _font(cls, v: str) -> str:
        return v if v in SAFE_FONTS else "DM Sans"

    def palette(self) -> list[str]:
        """Chart series colours, in order."""
        return [self.primary, self.secondary, self.accent, self.muted]


THEMES: dict[str, Theme] = {
    # warm monochrome with one terracotta accent: internal updates, notes, plans
    "minimal": Theme(name="minimal"),
    # deep navy and steel with a saffron accent: clients, finance, leadership
    "corporate": Theme(name="corporate", style="corporate", primary="#1D3557", secondary="#457B9D",
                       accent="#E9A23B", dark="#14213D", surface="#F1F4F8", text="#1E2A3A",
                       heading_font="Plus Jakarta Sans", body_font="DM Sans"),
    # charcoal with vermilion and deep green: pitches, launches
    "bold": Theme(name="bold", style="bold", primary="#FF5A36", secondary="#0E7C66", accent="#F2C14E",
                  dark="#121212", surface="#F4F1EC", text="#1C1C1C", heading_font="Space Grotesk",
                  body_font="Manrope"),
    # clay, teal and sand on cream: people, culture, education
    "warm": Theme(name="warm", style="warm", primary="#C0603E", secondary="#2A7F75", accent="#E3B448",
                  dark="#2B3A3A", surface="#FBF4EA", background="#FFFCF7", text="#33302B",
                  heading_font="Fraunces", body_font="DM Sans"),
    # ink and bronze: long-form reports, strategy
    "editorial": Theme(name="editorial", style="editorial", primary="#151515", secondary="#8C6D46",
                       accent="#A3361F", dark="#151515", surface="#F6F3EE", text="#222222",
                       heading_font="Playfair Display", body_font="Source Sans Pro"),
}


def theme_for(base: str = "minimal", **overrides: str) -> Theme:
    """A built-in theme with brand overrides applied (unknown or invalid fields are ignored)."""
    theme = THEMES.get(base, THEMES["minimal"])
    for key, value in overrides.items():
        if not value or key not in Theme.model_fields or key == "name":
            continue
        try:  # one field at a time: a bad colour never discards the user's other choices
            theme = Theme.model_validate({**theme.model_dump(), key: value})
        except ValueError:
            continue
    return theme

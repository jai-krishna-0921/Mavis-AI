"""Timezone from a shared location (tzfpy, offline), a city name (bundled GeoNames), or the fast model.
Telegram's language_code never sets a timezone (spec 5)."""

from __future__ import annotations

import csv
import gzip
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

DATA = Path(__file__).parent / "data"


@dataclass(frozen=True)
class CityMatch:
    name: str
    country: str
    zone: str
    population: int


def valid_zone(name: str | None) -> bool:
    if not name or "/" not in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def from_location(lat: float, lon: float) -> str | None:
    from tzfpy import get_tz

    zone = get_tz(lon, lat)
    return zone if valid_zone(zone) else None


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


@lru_cache(maxsize=4)
def _cities(path: str) -> dict[str, list[CityMatch]]:
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    index: dict[str, list[CityMatch]] = {}
    with opener(p, "rt", encoding="utf-8") as fh:
        for name, ascii_name, country, zone, pop in csv.reader(fh, delimiter="\t"):
            m = CityMatch(name, country, zone, int(pop or 0))
            for key in {_fold(name), _fold(ascii_name)}:
                index.setdefault(key, []).append(m)
    return index


def from_city(text: str, *, table: Path | None = None) -> list[CityMatch]:
    rows = _cities(str(table or DATA / "cities.tsv.gz")).get(_fold(text), [])
    best: dict[str, CityMatch] = {}
    for m in sorted(rows, key=lambda m: -m.population):
        if valid_zone(m.zone) and m.zone not in best:
            best[m.zone] = m
    return list(best.values())[:3]


class _Zone(BaseModel):
    zone: str


async def from_text_llm(text: str) -> str | None:
    from mavis.llm import models as llm

    try:
        out = await llm.structured(_Zone, "Return the IANA time zone name for the place the user describes. "
                                          "Only the zone name, like Europe/Lisbon.", text[:200],
                                   tier=llm.Tier.FAST, priority="interactive")
    except Exception:  # noqa: BLE001
        return None
    return out.zone if valid_zone(out.zone) else None

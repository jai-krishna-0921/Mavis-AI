from __future__ import annotations

from pathlib import Path

import pytest

from mavis.access import currency, tz_resolve

SMALL = Path(__file__).parent / "fixtures" / "cities_small.tsv"


@pytest.mark.parametrize("lat,lon,zone", [(41.15, -8.61, "Europe/Lisbon"), (4.71, -74.07, "America/Bogota"),
                                          (34.69, 135.50, "Asia/Tokyo")])
def test_location_resolves_offline(lat, lon, zone):
    assert tz_resolve.from_location(lat, lon) == zone


@pytest.mark.parametrize("text,zones", [("porto", ["Europe/Lisbon"]), ("Bogotá", ["America/Bogota"]),
                                        ("Portland", ["America/Los_Angeles", "America/New_York"])])
def test_city_lookup_ranks_by_population_and_dedupes_zones(text, zones):
    assert [m.zone for m in tz_resolve.from_city(text, table=SMALL)] == zones


def test_city_lookup_caps_at_three():
    got = tz_resolve.from_city("springfield", table=SMALL)
    assert len(got) == 3 and len({m.zone for m in got}) == 3


async def test_llm_fallback_must_pass_zoneinfo(fake_llm):
    fake_llm.push_structured(tz_resolve._Zone(zone="Asia/Kathmandu"))
    assert await tz_resolve.from_text_llm("near the big lake in the Himalayas") == "Asia/Kathmandu"
    fake_llm.push_structured(tz_resolve._Zone(zone="Mars/Olympus"))
    assert await tz_resolve.from_text_llm("olympus") is None


@pytest.mark.parametrize("zone,code", [("Europe/Lisbon", "EUR"), ("America/Bogota", "COP"),
                                       ("Asia/Tokyo", "JPY"), ("Asia/Kolkata", "INR"), ("Etc/UTC", None)])
def test_currency_from_zone(zone, code):
    assert currency.for_zone(zone) == code

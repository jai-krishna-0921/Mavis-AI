"""Currency from a zone's country (zone1970.tab first country) through a country -> ISO 4217 map."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"
# Countries whose currency is the euro, plus the rest of the common set. Extend by data, never by user.
_EURO = {"AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE", "IT", "LT", "LU", "LV", "MT", "NL",
         "PT", "SI", "SK"}
COUNTRY_CURRENCY = {**{c: "EUR" for c in _EURO}, "IN": "INR", "US": "USD", "GB": "GBP", "JP": "JPY",
                    "SG": "SGD", "AE": "AED", "CO": "COP", "AU": "AUD", "CA": "CAD", "BR": "BRL", "MX": "MXN",
                    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN", "ZA": "ZAR", "KR": "KRW",
                    "CN": "CNY", "HK": "HKD", "ID": "IDR", "MY": "MYR", "TH": "THB", "PH": "PHP", "VN": "VND",
                    "NZ": "NZD", "SA": "SAR", "TR": "TRY", "IL": "ILS", "EG": "EGP", "NG": "NGN", "KE": "KES",
                    "PK": "PKR", "BD": "BDT", "LK": "LKR", "NP": "NPR", "AR": "ARS", "CL": "CLP", "PE": "PEN"}
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


@lru_cache(maxsize=1)
def _zone_country() -> dict[str, str]:
    out = {}
    for line in (DATA / "zone_country.tsv").read_text(encoding="utf-8").splitlines():
        zone, _, cc = line.partition("\t")
        out[zone] = cc
    return out


def country_for_zone(zone: str) -> str | None:
    return _zone_country().get(zone)


def for_zone(zone: str) -> str | None:
    cc = country_for_zone(zone)
    return COUNTRY_CURRENCY.get(cc) if cc else None


def symbol(code: str) -> str:
    return SYMBOLS.get(code, code)

"""Build access/data tables (run once, commit the output): zone -> country from the system tzdata
zone1970.tab, and cities (population >= 15000) from a GeoNames cities15000.txt the owner downloads.

    uv run python -m scripts.build_geo_tables /usr/share/zoneinfo/zone1970.tab ~/Downloads/cities15000.txt
"""

from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src/mavis/access/data"


def main(zone_tab: str, cities: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    zones_out = (OUT / "zone_country.tsv").open("w", encoding="utf-8")
    with open(zone_tab, encoding="utf-8") as fh, zones_out as out:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            codes, _, zone = line.split("\t")[:3]
            out.write(f"{zone.strip()}\t{codes.split(',')[0]}\n")
    cities_out = gzip.open(OUT / "cities.tsv.gz", "wt", encoding="utf-8")
    with open(cities, encoding="utf-8") as fh, cities_out as out:
        w = csv.writer(out, delimiter="\t")
        for row in csv.reader(fh, delimiter="\t"):
            # geonameid, name, asciiname, alternates, lat, lon, ..., country(8), ..., population(14), tz(17)
            w.writerow([row[1], row[2], row[8], row[17], row[14]])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

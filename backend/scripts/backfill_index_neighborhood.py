"""
Populate building_search_index.neighborhood from NYC NTA 2020 polygons.

Search had NO neighborhood concept anywhere -- not in the intent router, not in
the index -- so people's most natural framing was silently dropped:
"art deco in tribeca" returned Radio City and the Chrysler Building (Midtown),
and "cast iron soho" returned 2016-2018 glass towers. historic_district does
not substitute: it is populated on 217 of 35,382 Supabase rows.

NTA names are compound ("SoHo-Little Italy-Hudson Square"), so neighborhood
carries the official name for display and neighborhood_text carries the parts
split out, which is what "soho" actually has to match.

Source: NYC Open Data NTA 2020 (9nt8-h7nd), 262 polygons, downloaded on first
run and cached next to this script.

Run: python -m scripts.backfill_index_neighborhood [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.request

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("nta")

NTA_URL = ("https://data.cityofnewyork.us/api/geospatial/9nt8-h7nd"
           "?method=export&format=GeoJSON")
CACHE = os.path.join(os.path.dirname(__file__), ".nta_2020.geojson")


def load_polygons():
    if not os.path.exists(CACHE):
        log.info("downloading NTA 2020 polygons...")
        urllib.request.urlretrieve(NTA_URL, CACHE)
    with open(CACHE) as fh:
        gj = json.load(fh)
    from shapely.geometry import shape
    polys, names = [], []
    for f in gj["features"]:
        props = f.get("properties") or {}
        name = (props.get("ntaname") or "").strip()
        # ntatype != '0' is a park, airport or cemetery tract, not a
        # neighborhood anyone searches by.
        if not name or str(props.get("ntatype", "0")) != "0":
            continue
        polys.append(shape(f["geometry"]))
        names.append(name)
    log.info("%d neighborhood polygons", len(polys))
    return polys, names


def searchable(name: str) -> str:
    """'SoHo-Little Italy-Hudson Square' -> 'soho little italy hudson square'.

    Without this the compound official name is one opaque token and "soho"
    matches nothing.
    """
    return " ".join(re.sub(r"[-/(),]+", " ", name.lower()).split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from shapely.geometry import Point
    from shapely.strtree import STRtree

    polys, names = load_polygons()
    tree = STRtree(polys)

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SELECT bin, lat, lng FROM building_search_index "
                    " WHERE lat IS NOT NULL AND lng IS NOT NULL")
        rows = cur.fetchall()
    log.info("%d rows with coordinates", len(rows))

    pairs, misses = [], 0
    for b, lat, lng in rows:
        pt = Point(float(lng), float(lat))
        hit = None
        for idx in tree.query(pt):
            if polys[idx].contains(pt):
                hit = names[idx]
                break
        if hit:
            pairs.append((str(b), hit, searchable(hit)))
        else:
            misses += 1

    log.info("matched %d, unmatched %d", len(pairs), misses)
    if args.dry_run:
        from collections import Counter
        for n, c in Counter(p[1] for p in pairs).most_common(8):
            log.info("  %-42s %5d -> %s", n, c, searchable(n))
        return 0

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE building_search_index "
                    " ADD COLUMN IF NOT EXISTS neighborhood text,"
                    " ADD COLUMN IF NOT EXISTS neighborhood_text text")
        psycopg2.extras.execute_values(
            cur,
            "UPDATE building_search_index b"
            "   SET neighborhood = v.n, neighborhood_text = v.nt"
            "  FROM (VALUES %s) AS v(bin, n, nt) WHERE b.bin = v.bin",
            pairs, page_size=1000,
        )
        conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bsi_neighborhood_trgm"
                    " ON building_search_index USING gin (lower(neighborhood_text) gin_trgm_ops)")
        conn.commit()
        cur.execute("SELECT count(*) FILTER (WHERE neighborhood IS NOT NULL), count(*)"
                    "  FROM building_search_index")
        filled, total = cur.fetchone()
        log.info("neighborhood populated on %d/%d rows", filled, total)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

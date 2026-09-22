"""
Flag lore/plaque/contribution rows that are not in New York City.

263 of 1,922 layer rows sat outside the city's NTA polygons on 2026-09-22:
AMC multiplexes in Wayne and Clifton, the Hoboken Historical Museum, the
Hall-Mills murder in New Brunswick, memorials across Nassau. "spooky spots"
surfaced a house in Sea Cliff, Long Island.

A strict polygon test is wrong here, unlike for venues: a real share of NYC
lore happens on the water (the General Slocum, the Brooklyn Bridge panic of
1883, the airport once proposed in the Hudson). So a row counts as NYC within
~800m of any NTA polygon, which keeps mid-river stories and drops Jersey City,
whose shore is 1.2km+ from Manhattan's.

Nothing is deleted; the search legs filter `in_nyc IS NOT FALSE`, so a row
this script has not seen yet (NULL) stays visible.

Run: python -m scripts.flag_layers_in_nyc
"""
from __future__ import annotations

import os
import sys

import psycopg2
import psycopg2.extras
import shapely
from shapely.strtree import STRtree

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.enrich_venues import load_ntas  # noqa: E402

WITHIN_DEG = 0.009  # ~800m at NYC's latitude


def main() -> int:
    geoms, _ = load_ntas()
    tree = STRtree(geoms)
    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("ALTER TABLE layer_search_index ADD COLUMN IF NOT EXISTS in_nyc boolean")
        conn.commit()
        cur.execute("SELECT id, lat, lng, in_nyc FROM layer_search_index WHERE lat IS NOT NULL AND lng IS NOT NULL")
        rows = cur.fetchall()
        pts = shapely.points([r[2] for r in rows], [r[1] for r in rows])
        near = set(tree.query(pts, predicate="dwithin", distance=WITHIN_DEG)[0].tolist())
        changed = [(r[0], i in near) for i, r in enumerate(rows) if r[3] != (i in near)]
        psycopg2.extras.execute_values(
            cur,
            "UPDATE layer_search_index l SET in_nyc = d.n FROM (VALUES %s) AS d(id, n) WHERE l.id = d.id",
            changed, page_size=500,
        )
        conn.commit()
    print(f"{len(rows)} rows, {len(rows) - len(near)} outside NYC, {len(changed)} flags written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

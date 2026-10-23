#!/usr/bin/env python3
"""
Re-join every venue to the building it is actually in.

The original join took the nearest building CENTROID, which hands shopfronts
to the big building next door (Brooklyn Diner, 212 W 57th, was joined to the
Rodin Studios on the corner). join_buildings_via_footprints now measures to
the footprint polygon; this applies it to the venues already loaded.

Writes bin, bbl, building_year and building_style only where they change.
The text, lex_text and embedding still describe the old building afterwards,
so run enrich_venues next: it rebuilds exactly the rows whose text changed.

Env:
  SEARCH_DB_URL      pgvector DB holding `venues` (owner login: the API's user
                     is read-only, see docs/SEARCH_INDEX_JOBS.md)
  FOOTPRINTS_DB_URL  building_footprints + pluto_buildings

Run:
  python -m scripts.rejoin_venue_buildings --dry-run     # counts + samples
  python -m scripts.rejoin_venue_buildings --dry-run --show "brooklyn diner"
      # expect the W 57th one (49ecebbaf964a520ae671fe3) to leave 1024896
  python -m scripts.rejoin_venue_buildings               # write
  python -m scripts.enrich_venues                        # then re-embed
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from scripts.seed_venues import join_buildings_via_footprints  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rejoin_venues")

PAGE = 20000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--show", default=None,
                    help='print every move for venues whose name contains this, e.g. --show "brooklyn diner"')
    args = ap.parse_args()

    search_url = os.environ.get("SEARCH_DB_URL")
    fp_url = os.environ.get("FOOTPRINTS_DB_URL")
    if not search_url or not fp_url:
        logger.error("SEARCH_DB_URL and FOOTPRINTS_DB_URL must both be set")
        sys.exit(1)

    with psycopg.connect(search_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fsq_id, name, lat, lng, bin, bbl, building_year, building_style "
            "FROM venues WHERE lat IS NOT NULL AND lng IS NOT NULL ORDER BY fsq_id"
            + (f" LIMIT {int(args.limit)}" if args.limit else "")
        )
        venues = cur.fetchall()
    logger.info(f"{len(venues)} venues with a coordinate")

    changed = []
    for start in range(0, len(venues), PAGE):
        page = venues[start : start + PAGE]
        # join_buildings_via_footprints reads lat/lng at [2]/[3].
        joins = join_buildings_via_footprints(fp_url, search_url, page)
        for i, v in enumerate(page):
            fsq_id, name, _, _, old_bin, old_bbl, old_year, old_style = v
            new_bin, new_bbl, new_year, new_style = joins.get(i, [None, None, None, ""])
            if str(new_bin or "") != str(old_bin or ""):
                changed.append((fsq_id, name, old_bin, new_bin, new_bbl, new_year, new_style or None))

    logger.info(f"{len(changed)} venues move to a different building ({100 * len(changed) // max(1, len(venues))}%)")
    for c in changed[:15]:
        logger.info(f"  {c[1]}: bin {c[2]} -> {c[3]}")

    if args.show:
        want = args.show.lower()
        for c in changed:
            if want in (c[1] or "").lower():
                logger.info(f"  [show] {c[1]} ({c[0]}): bin {c[2]} -> {c[3]}")

    if args.dry_run:
        logger.info("dry-run: no writes")
        return

    with psycopg.connect(search_url) as conn, conn.cursor() as cur:
        cur.executemany(
            "UPDATE venues SET bin = %s, bbl = %s, building_year = %s, building_style = %s, "
            "updated_at = now() WHERE fsq_id = %s",
            [(c[3], c[4], c[5], c[6], c[0]) for c in changed],
        )
        conn.commit()
    logger.info(f"updated {len(changed)} rows. Now run: python -m scripts.enrich_venues")


if __name__ == "__main__":
    main()

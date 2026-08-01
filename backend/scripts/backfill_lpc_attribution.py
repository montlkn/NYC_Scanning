#!/usr/bin/env python3
"""
Backfill architect / primary+secondary style / historic district onto
`building_search_index` from NYC Open Data `gpmc-yuvp` (LPC Individual Landmark
and Historic District Building Database).

This is the SAME dataset building_search_index was originally derived from --
it does not add buildings. It adds three fields that were dropped on the way in:

  architect         27,638 rows. There was no architect column at all, so the
                    `architect` search intent had nothing structured to match.
  style_primary /   LPC records primary and secondary styles separately. Flattened
  style_secondary   into one string, LPC's own hedges ("Simplified Colonial
                    Revival or Art Deco", 598 rows) read as confident Art Deco
                    attributions to a trigram matcher.
  hist_dist         161 named districts, usable as a facet.

Sentinels: LPC writes "Not determined" / "0" for unknown, in every one of these
fields. Stored verbatim they become real-looking values ("architect: 0"), so
they are normalized to NULL here rather than downstream.

Env:
  SEARCH_DB_URL   pgvector DB holding building_search_index  [required]

Usage:
  python scripts/backfill_lpc_attribution.py --dry-run
  python scripts/backfill_lpc_attribution.py
"""

import argparse
import json
import logging
import os
import sys
import urllib.request

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_lpc")

SODA_URL = (
    "https://data.cityofnewyork.us/resource/gpmc-yuvp.json"
    "?$select=bin,bbl,des_addres,style_prim,style_sec,arch_build,date_combo,hist_dist"
    "&$limit=50000"
)

# LPC's placeholders for "we don't know". These appear in EVERY attribution
# field, so a naive load produces rows claiming an architect literally named
# "Not determined".
SENTINELS = {"not determined", "0", "", "none", "undetermined", "n/a", "unknown"}


def clean(v) -> str:
    v = (v or "").strip()
    return "" if v.lower() in SENTINELS else v


def fetch() -> list:
    logger.info("fetching LPC building database (gpmc-yuvp)...")
    with urllib.request.urlopen(SODA_URL, timeout=300) as r:
        rows = json.loads(r.read())
    logger.info(f"{len(rows)} rows fetched")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("SEARCH_DB_URL")
    if not url:
        logger.error("SEARCH_DB_URL must be set")
        sys.exit(1)

    rows = fetch()

    prepared = []
    for r in rows:
        bin_ = (r.get("bin") or "").strip().replace(".0", "")
        if not bin_:
            continue
        prepared.append(
            (
                clean(r.get("arch_build")) or None,
                clean(r.get("style_prim")) or None,
                clean(r.get("style_sec")) or None,
                clean(r.get("hist_dist")) or None,
                bin_,
            )
        )

    n = len(prepared) or 1
    logger.info(
        f"{len(prepared)} rows with a BIN — "
        f"architect {sum(1 for p in prepared if p[0])} ({100*sum(1 for p in prepared if p[0])//n}%), "
        f"style_primary {sum(1 for p in prepared if p[1])}, "
        f"style_secondary {sum(1 for p in prepared if p[2])}, "
        f"district {sum(1 for p in prepared if p[3])}"
    )

    if args.dry_run:
        for p in prepared[:8]:
            logger.info(f"  bin {p[4]}: architect={p[0]!r} style={p[1]!r}/{p[2]!r} dist={p[3]!r}")
        logger.info("dry-run: no writes")
        return

    # UPDATE, not upsert: this dataset must never INSERT buildings. A row absent
    # from building_search_index is absent because it has no embedding, and a
    # row without an embedding breaks the vector leg.
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE building_search_index
               SET architect       = COALESCE(%s, architect),
                   style_primary   = COALESCE(%s, style_primary),
                   style_secondary = COALESCE(%s, style_secondary),
                   hist_dist       = COALESCE(%s, hist_dist)
             WHERE bin = %s
            """,
            prepared,
        )
        conn.commit()
        matched = cur.rowcount

    logger.info(f"✅ done — {matched} index rows updated (of {len(prepared)} LPC rows)")

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(architect), count(style_primary), "
            "count(style_secondary), count(hist_dist) FROM building_search_index"
        )
        total, arch, sp, ss, hd = cur.fetchone()
        logger.info(
            f"   index coverage: {total} rows — architect {arch}, "
            f"style_primary {sp}, style_secondary {ss}, district {hd}"
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Sync iOS-generated building lore (MAIN `grok_narratives`) into the buildings DB
`buildings_full_merge_scanning.storytelling`, so the next `embed_buildings.py`
run folds that prose into the search index.

WHY THIS IS A SEPARATE SCRIPT (and why YOU run it, not the backend):
  `grok_narratives` lives on the MAIN Supabase project; `storytelling` lives on
  the BUILDINGS project. No single SQL can join across two Supabase projects, and
  Claude has no access to MAIN — so this copies bin+narrative MAIN → BUILDINGS
  from a machine that holds both connection strings (yours).

The backend already re-indexes a building the moment IT generates lore
(lore_generator._reindex_building). This script covers the larger pool of lore
the iOS app wrote directly to MAIN, which the backend never sees.

Env (both required — put them in backend/.env or export):
  MAIN_DB_URL          MAIN Supabase Postgres   (source: grok_narratives)
  DATABASE_URL         BUILDINGS Supabase        (target: storytelling)

Usage:
  python scripts/sync_narratives_to_storytelling.py --dry-run   # counts only
  python scripts/sync_narratives_to_storytelling.py             # write storytelling
  python scripts/sync_narratives_to_storytelling.py --overwrite # also replace non-empty
  # then fold into the index (incremental — only changed rows re-embed is not
  # automatic, so use --rebuild OR clear those BINs; simplest:):
  python scripts/embed_buildings.py --rebuild
"""

import argparse
import logging
import os
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sync_narratives")


def _clean_bin(b: str) -> str:
    return str(b or "").strip().removesuffix(".0")


def _prose_only(narrative: str) -> str:
    """Drop the machine tail. iOS narratives carry a `SOURCES:` / `FACTS:`
    section meant for parsing, not reading — strip it so only human prose is
    embedded. Case-insensitive, keeps everything before the first marker."""
    if not narrative:
        return ""
    lo = narrative.lower()
    cut = len(narrative)
    for marker in ("\nsources:", "\nfacts:", "sources:", "facts:"):
        i = lo.find(marker)
        if i != -1:
            cut = min(cut, i)
    return narrative[:cut].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace storytelling even when already populated")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    main_url = os.environ.get("MAIN_DB_URL")
    bld_url = os.environ.get("DATABASE_URL")
    if not main_url or not bld_url:
        logger.error("MAIN_DB_URL and DATABASE_URL must both be set")
        sys.exit(1)

    # 1. Pull all narratives from MAIN.
    with psycopg.connect(main_url) as conn, conn.cursor() as cur:
        sql = "SELECT bin, narrative FROM grok_narratives WHERE narrative IS NOT NULL AND narrative <> ''"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
    logger.info(f"{len(rows)} narratives in MAIN.grok_narratives")

    pairs = []
    for b, narrative in rows:
        bc = _clean_bin(b)
        prose = _prose_only(narrative)
        if bc and prose:
            pairs.append((bc, prose))
    logger.info(f"{len(pairs)} have usable prose after stripping SOURCES/FACTS")

    if args.dry_run:
        for bc, prose in pairs[:5]:
            logger.info(f"  [{bc}] {prose[:160]}")
        logger.info("dry-run: no writes")
        return

    # 2. Write into BUILDINGS.storytelling. Only fill empties unless --overwrite.
    guard = "" if args.overwrite else \
        "AND (storytelling IS NULL OR storytelling = '')"
    written = 0
    with psycopg.connect(bld_url) as conn, conn.cursor() as cur:
        for bc, prose in pairs:
            cur.execute(
                f"""
                UPDATE buildings_full_merge_scanning
                SET storytelling = %s
                WHERE REPLACE(bin, '.0', '') = %s {guard}
                """,
                (prose, bc),
            )
            written += cur.rowcount
        conn.commit()
    logger.info(f"✅ updated storytelling on {written} building rows")
    logger.info("Now run: python scripts/embed_buildings.py --rebuild   (folds prose into the index)")


if __name__ == "__main__":
    main()

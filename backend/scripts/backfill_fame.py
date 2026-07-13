"""
Backfill building_search_index.fame from buildings_full_merge_scanning.final_score.

fame = final_score / max(final_score) — normalized against the corpus's own
maximum, never a hardcoded scale. Rows with no final_score get NULL (no fame
signal, treated as 0 by ranking). Idempotent; safe to re-run any time the
curated scores change. Requires DATABASE_URL (buildings Postgres) and
SEARCH_DB_URL (Railway search index), same env contract as embed_buildings.py.

Run: python3 scripts/backfill_fame.py
"""

import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_fame")


def main():
    supa_url = os.environ.get("DATABASE_URL")
    rail_url = os.environ.get("SEARCH_DB_URL")
    if not supa_url or not rail_url:
        logger.error("DATABASE_URL and SEARCH_DB_URL must both be set")
        sys.exit(1)

    with psycopg.connect(supa_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT bin::text, final_score::float / NULLIF(mx, 0)
            FROM buildings_full_merge_scanning,
                 (SELECT max(final_score::float) AS mx FROM buildings_full_merge_scanning) m
            WHERE bin IS NOT NULL AND final_score IS NOT NULL
            """
        )
        rows = [(r[0].removesuffix(".0"), r[1]) for r in cur.fetchall()]
    logger.info(f"{len(rows)} fame scores fetched (normalized to corpus max)")

    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.executemany(
            "UPDATE building_search_index SET fame = %s WHERE bin = %s",
            [(fame, b) for b, fame in rows],
        )
        conn.commit()
        cur.execute("SELECT count(fame), max(fame), percentile_cont(0.5) WITHIN GROUP (ORDER BY fame) FROM building_search_index")
        n, mx, med = cur.fetchone()
    logger.info(f"✅ fame populated on {n} rows (max={mx:.3f}, median={med:.3f})")


if __name__ == "__main__":
    main()

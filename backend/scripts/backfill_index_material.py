"""
Backfill building_search_index.material from Supabase mat_prim.

20260710_index_enrich.sql added the column but nothing ever populated it for
the curated corpus: 35,142 of 35,334 rows are NULL, so the `material` facet
filter matches almost nothing and "terracotta" queries have no column to hit
(Supabase carries 1,502 'Brick and Terra Cotta' + 679 'Limestone and Terra
Cotta' rows).

Also writes material_text: the same value normalized for retrieval, with the
spelling variants folded in. The source spells it "Terra Cotta" (two words)
while people type "terracotta" (one), and neither trigram nor a 384-dim
embedding bridges that on its own.

Run: python -m scripts.backfill_index_material [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("material")

JUNK = {"", "0", "unknown", "not determined", "n/a", "na", "none", "undetermined"}

# Retrieval spellings for what the source column actually contains. This is a
# normalization table for observed DB values, not a guessed synonym list: every
# key below is a real distinct mat_prim value.
VARIANTS = {
    "terra cotta": "terra cotta terracotta terra-cotta",
    "cast iron": "cast iron cast-iron ironwork",
    "wood frame": "wood frame timber clapboard",
    "brownstone": "brownstone sandstone rowhouse stoop",
}


def expand(raw: str) -> str:
    low = raw.lower()
    out = [low]
    for key, extra in VARIANTS.items():
        if key in low:
            out.append(extra)
    return " ".join(dict.fromkeys(" ".join(out).split()))


def fetch_source() -> list[tuple[str, str]]:
    """Read mat_prim straight off the BUILDINGS Postgres.

    $DATABASE_URL is the direct connection to that project; $SUPABASE_URL in
    this .env points at MAIN, which has no buildings table (a PostgREST fetch
    against it 404s).
    """
    out: list[tuple[str, str]] = []
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT bin, mat_prim
              FROM buildings_full_merge_scanning
             WHERE mat_prim IS NOT NULL AND btrim(mat_prim) <> ''
        """)
        for b, m in cur.fetchall():
            m = (m or "").strip()
            if not b or m.lower() in JUNK:
                continue
            b = str(b)
            if b.endswith(".0"):
                b = b[:-2]
            out.append((b, m))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pairs = fetch_source()
    log.info("%d rows with a usable mat_prim", len(pairs))
    if args.dry_run:
        from collections import Counter
        for v, n in Counter(m for _, m in pairs).most_common(8):
            log.info("  %-28s %6d  -> %s", v, n, expand(v))
        return 0

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE building_search_index "
                    "ADD COLUMN IF NOT EXISTS material_text text")
        psycopg2.extras.execute_values(
            cur,
            """
            UPDATE building_search_index b
               SET material = v.mat, material_text = v.mtext
              FROM (VALUES %s) AS v(bin, mat, mtext)
             WHERE b.bin = v.bin
            """,
            [(b, m, expand(m)) for b, m in pairs],
            page_size=1000,
        )
        conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bsi_material_text_trgm "
                    "ON building_search_index USING gin (lower(material_text) gin_trgm_ops)")
        conn.commit()
        cur.execute("SELECT count(*) FILTER (WHERE material IS NOT NULL), count(*) "
                    "FROM building_search_index")
        filled, total = cur.fetchone()
        log.info("material populated on %d/%d rows", filled, total)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

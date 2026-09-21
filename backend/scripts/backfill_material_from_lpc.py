"""
Fill building_search_index.material for rows Supabase mat_prim does not cover,
by reading the LPC designation-report prose.

backfill_index_material.py takes mat_prim to 31,438 of 35,334 rows. Of the
3,896 left, 3,644 DO have LPC text -- the material is simply in prose ("the
brick and terra-cotta facade") rather than in a labelled field.

Parsing only a "Material(s):" label recovers 190 of them and drags in the
WINDOW material field as noise ("One-over-one double-hung/Wood"). Scanning the
whole building-specific chunk for material words recovers 1,824. So this reads
the prose and takes the most-mentioned material.

A prose mention is weaker evidence than PLUTO's mat_prim, so every row written
here is stamped material_source='lpc_prose' and never overwrites an existing
value. That keeps the two provenances separable and this pass reversible.

Run: python -m scripts.backfill_material_from_lpc [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from collections import Counter, defaultdict

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("lpc-material")

# Display value -> retrieval spellings. Ordered longest-first at match time so
# "cast iron" wins over "iron" and "terra cotta" over a bare "stone".
CANON = {
    "terra cotta":  "terra cotta terracotta terra-cotta",
    "terra-cotta":  "terra cotta terracotta terra-cotta",
    "terracotta":   "terra cotta terracotta terra-cotta",
    "cast iron":    "cast iron cast-iron ironwork",
    "cast-iron":    "cast iron cast-iron ironwork",
    "brownstone":   "brownstone sandstone",
    "wood frame":   "wood frame timber",
    "clapboard":    "wood frame timber clapboard",
    "limestone":    "limestone",
    "sandstone":    "sandstone",
    "granite":      "granite",
    "marble":       "marble",
    "concrete":     "concrete",
    "stucco":       "stucco",
    "brick":        "brick",
    "slate":        "slate",
    "copper":       "copper",
    "steel":        "steel",
    "glass":        "glass",
}
DISPLAY = {
    "terra-cotta": "Terra Cotta", "terracotta": "Terra Cotta", "terra cotta": "Terra Cotta",
    "cast-iron": "Cast Iron", "cast iron": "Cast Iron", "wood frame": "Wood Frame",
    "clapboard": "Wood Frame",
}
MIN_MENTIONS = 2  # one passing mention may be about a neighbouring building


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    search = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with search.cursor() as cur:
        cur.execute("SELECT bin FROM building_search_index WHERE material IS NULL")
        need = {r[0] for r in cur.fetchall()}
    log.info("%d index rows have no material", len(need))

    with psycopg2.connect(os.environ["FOOTPRINTS_DB_URL"]) as f, f.cursor() as cur:
        cur.execute("SELECT bin, chunk_text FROM landmark_chunks "
                    " WHERE specificity = 'building'")
        rows = cur.fetchall()

    counts: dict[str, Counter] = defaultdict(Counter)
    for b, txt in rows:
        if not b:
            continue
        b = str(b)
        if b.endswith(".0"):
            b = b[:-2]
        if b not in need:
            continue
        low = (txt or "").lower()
        for key in sorted(CANON, key=len, reverse=True):
            n = len(re.findall(r"\b" + re.escape(key) + r"\b", low))
            if n:
                counts[b][key] += n

    picked: dict[str, tuple[str, str]] = {}
    for b, c in counts.items():
        key, n = c.most_common(1)[0]
        if n < MIN_MENTIONS:
            continue
        picked[b] = (DISPLAY.get(key, key.title()), CANON[key])

    log.info("recovered a material for %d of %d (>= %d mentions)",
             len(picked), len(need), MIN_MENTIONS)
    if args.dry_run:
        for v, n in Counter(v[0] for v in picked.values()).most_common(10):
            log.info("  %-16s %5d", v, n)
        return 0

    with search.cursor() as cur:
        cur.execute("ALTER TABLE building_search_index "
                    " ADD COLUMN IF NOT EXISTS material_source text")
        # Stamp the rows that came from PLUTO before adding a second source, so
        # the two never become indistinguishable.
        cur.execute("UPDATE building_search_index SET material_source = 'pluto'"
                    " WHERE material IS NOT NULL AND material_source IS NULL")
        psycopg2.extras.execute_values(
            cur,
            "UPDATE building_search_index b"
            "   SET material = v.m, material_text = v.mt, material_source = 'lpc_prose'"
            "  FROM (VALUES %s) AS v(bin, m, mt)"
            " WHERE b.bin = v.bin AND b.material IS NULL",
            [(b, m, mt) for b, (m, mt) in picked.items()], page_size=1000,
        )
        search.commit()
        cur.execute("SELECT coalesce(material_source,'(none)'), count(*)"
                    "  FROM building_search_index GROUP BY 1 ORDER BY 2 DESC")
        for src, n in cur.fetchall():
            log.info("  %-12s %6d", src, n)
    search.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

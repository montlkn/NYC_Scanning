"""
Populate building_search_index.aesthetic from the 9 archetypes.

build_text already folds "{primary_aesthetic} character" into the embedding,
so the archetype is reachable semantically -- 25,540 indexed texts contain
"romantic". But there is no STRUCTURED column, which means no exact match, no
facet, and no way to say "show me the visionary ones" and get precisely those.

The archetypes are a closed set of nine (romantic 16,506, classicist 12,394,
vernacularist 3,249, stylist 1,418, industrialist 772, modernist 577,
austerist 169, visionary 128, pop_culturalist 111), so this is an exact
lookup, never a similarity guess.

aesthetic_text carries the spellings people type: pop_culturalist is written
"pop culturalist" and "pop culture" in a query, never with the underscore.

Run: python -m scripts.backfill_index_aesthetic [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("aesthetic")

# Query spellings per archetype. Every key is a real primary_aesthetic value.
SPELLINGS = {
    "romantic":        "romantic romanticist romance",
    "classicist":      "classicist classical classicism",
    "vernacularist":   "vernacularist vernacular",
    "stylist":         "stylist stylistic stylised stylized",
    "industrialist":   "industrialist industrial",
    "modernist":       "modernist modern modernism",
    "austerist":       "austerist austere austerity minimal",
    "visionary":       "visionary visionist futurist",
    "pop_culturalist": "pop culturalist popculturalist pop culture popular",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg2.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("""
            SELECT bin, primary_aesthetic, secondary_aesthetic
              FROM buildings_full_merge_scanning
             WHERE primary_aesthetic IS NOT NULL AND primary_aesthetic <> ''
        """)
        rows = cur.fetchall()

    pairs = []
    for b, prim, sec in rows:
        if not b:
            continue
        b = str(b)
        if b.endswith(".0"):
            b = b[:-2]
        prim = (prim or "").strip().lower()
        sec = (sec or "").strip().lower()
        words = [SPELLINGS.get(prim, prim)]
        if sec and sec != prim:
            words.append(SPELLINGS.get(sec, sec))
        txt = " ".join(dict.fromkeys(" ".join(words).split()))
        pairs.append((b, prim, txt))

    log.info("%d rows carry a primary archetype", len(pairs))
    if args.dry_run:
        from collections import Counter
        for a, n in Counter(p[1] for p in pairs).most_common():
            log.info("  %-16s %6d -> %s", a, n, SPELLINGS.get(a, a))
        return 0

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("ALTER TABLE building_search_index "
                    " ADD COLUMN IF NOT EXISTS aesthetic text,"
                    " ADD COLUMN IF NOT EXISTS aesthetic_text text")
        conn.commit()
        for i in range(0, len(pairs), 2000):
            psycopg2.extras.execute_values(
                cur,
                "UPDATE building_search_index b SET aesthetic=v.a, aesthetic_text=v.t"
                "  FROM (VALUES %s) AS v(bin,a,t) WHERE b.bin = v.bin",
                pairs[i:i + 2000], page_size=1000)
            conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bsi_aesthetic ON building_search_index (aesthetic)")
        conn.commit()
        cur.execute("SELECT aesthetic, count(*) FROM building_search_index "
                    " WHERE aesthetic IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
        for a, n in cur.fetchall():
            log.info("  %-16s %6d", a, n)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

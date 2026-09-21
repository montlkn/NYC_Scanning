"""
Populate building_search_index.name_norm: names and aliases ONLY.

The fuzzy (typo-tolerant) retrieval leg runs similarity(query, text) -- a
WHOLE-STRING trigram comparison against the ~200-character embedded document.
That score is structurally near zero no matter how good the match, so the leg
is dead weight and typo tolerance is a coin flip: "woolwoth" finds the
Woolworth Building, "chrystler" does not surface the Chrysler Building at all.

similarity() only works against a string of comparable length, so the fuzzy leg
needs a short name-only column to compare against.

Run: python -m scripts.backfill_index_name_norm [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("name_norm")

JUNK = {"", "0", "unknown", "not determined", "n/a", "none"}


def clean(v) -> str:
    s = (str(v) if v is not None else "").strip()
    return "" if s.lower() in JUNK else s


def build(name, wiki, colloquial) -> str:
    parts: list[str] = []
    for raw in (name, wiki, colloquial):
        c = clean(raw)
        if not c:
            continue
        # colloquial_names_text is pipe-delimited ("Seagram Building | The
        # Seagram | 375 Park").
        parts += [p.strip() for p in c.split("|") if p.strip()]
    seen, out = set(), []
    for p in parts:
        k = re.sub(r"[^a-z0-9]+", "", p.lower())
        if k and k not in seen:
            seen.add(k)
            out.append(p)
    return " | ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT bin, building_name, wiki_name, colloquial_names_text
              FROM buildings_full_merge_scanning
        """)
        rows = cur.fetchall()

    pairs = []
    for b, name, wiki, coll in rows:
        if not b:
            continue
        b = str(b)
        if b.endswith(".0"):
            b = b[:-2]
        nn = build(name, wiki, coll)
        if nn:
            pairs.append((b, nn))

    log.info("%d/%d rows have a usable name_norm", len(pairs), len(rows))
    if args.dry_run:
        for b, nn in pairs[:8]:
            log.info("  %s -> %s", b, nn)
        return 0

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE building_search_index "
                    "ADD COLUMN IF NOT EXISTS name_norm text")
        psycopg2.extras.execute_values(
            cur,
            "UPDATE building_search_index b SET name_norm = v.nn"
            "  FROM (VALUES %s) AS v(bin, nn) WHERE b.bin = v.bin",
            pairs, page_size=1000,
        )
        conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bsi_name_norm_trgm "
                    "ON building_search_index USING gin (lower(name_norm) gin_trgm_ops)")
        conn.commit()
        cur.execute("SELECT count(*) FILTER (WHERE name_norm IS NOT NULL), count(*)"
                    "  FROM building_search_index")
        filled, total = cur.fetchone()
        log.info("name_norm populated on %d/%d rows", filled, total)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

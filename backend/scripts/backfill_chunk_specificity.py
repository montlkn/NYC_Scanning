#!/usr/bin/env python3
"""
Backfill `specificity` on landmark_chunks rows that predate the column.

The problem
───────────
`specificity='building'` marks text that is about ONE building. It was added
with the district-report ingest, so every chunk written before that carries
NULL — including all ~1,748 buildings whose lore comes from an INDIVIDUAL
landmark report (Empire State, Flatiron, and the rest of the best material in
the corpus).

The iOS client accepts only `specificity == "building"` — deliberately, so a
generic district blurb is never shown as if it described this building. With
those rows left NULL the client rejects the corpus's strongest content and
falls through to paid agentic web search for exactly the buildings that needed
it least.

How the two are told apart
──────────────────────────
Individual-report text is unique to one building. District blurbs are copied to
every member BIN — measured at 200 to 2,100 bins sharing one text. A threshold
of 10 sits in the empty gap between those populations, nowhere near either.

Safe to re-run: the predicate only matches rows still NULL.
Reversible:  UPDATE landmark_chunks SET specificity = NULL
             WHERE specificity = 'building' AND source_file NOT IN (...);
             -- or restore from the count printed below.

Usage:
  python scripts/backfill_chunk_specificity.py            # dry run, counts only
  python scripts/backfill_chunk_specificity.py --commit
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SHARED_BIN_THRESHOLD = 10

COUNT_SQL = """
WITH t AS (
  SELECT md5(chunk_text) h, count(DISTINCT bin) nb FROM landmark_chunks GROUP BY 1
)
SELECT count(*) AS rows, count(DISTINCT lc.bin) AS bins
FROM landmark_chunks lc JOIN t ON t.h = md5(lc.chunk_text)
WHERE lc.specificity IS NULL AND t.nb <= %s
"""

UPDATE_SQL = """
WITH t AS (
  SELECT md5(chunk_text) h, count(DISTINCT bin) nb FROM landmark_chunks GROUP BY 1
)
UPDATE landmark_chunks lc SET specificity = 'building'
FROM t WHERE t.h = md5(lc.chunk_text)
  AND lc.specificity IS NULL AND t.nb <= %s
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually write")
    args = ap.parse_args()

    url = os.environ.get("FOOTPRINTS_DB_URL")
    if not url:
        print("FOOTPRINTS_DB_URL not set (expected in backend/.env)", file=sys.stderr)
        return 2

    conn = psycopg2.connect(url)
    cur = conn.cursor()

    cur.execute(COUNT_SQL, (SHARED_BIN_THRESHOLD,))
    rows, bins = cur.fetchone()
    print(f"rows to update : {rows}")
    print(f"bins affected  : {bins}")

    if not args.commit:
        print("\nDRY RUN — nothing written. Re-run with --commit")
        cur.close()
        conn.close()
        return 0

    cur.execute(UPDATE_SQL, (SHARED_BIN_THRESHOLD,))
    print(f"updated: {cur.rowcount}")
    conn.commit()

    cur.execute("SELECT specificity, count(*), count(DISTINCT bin) "
                "FROM landmark_chunks GROUP BY 1 ORDER BY 1")
    print("\nfinal distribution:")
    for spec, n, b in cur.fetchall():
        print(f"  {spec or 'NULL':<10} rows={n:<8} bins={b}")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

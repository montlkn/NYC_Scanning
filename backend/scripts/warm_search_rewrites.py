"""
Pre-compute LLM rewrites for the queries people actually type.

The first search of a vague query ("spooky spots") waits up to 4.5s for the
model; every later one is served from search_interpretation_cache and runs
the rewrite in parallel. This runs the most frequent logged queries through
the live endpoint once, so real users land on the cached path.

Queries come from search_query_log, not from a list in this file.

Run: python -m scripts.warm_search_rewrites [--base URL] [--top N]
Cron: daily is plenty; a warm query costs one cache lookup.
"""
from __future__ import annotations

import argparse
import os
import time

import httpx
import psycopg2

DEFAULT_BASE = "https://nycscanning-production.up.railway.app"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SEARCH_WARM_BASE", DEFAULT_BASE))
    ap.add_argument("--top", type=int, default=200)
    args = ap.parse_args()

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lower(trim(query)) q, count(*) n
              FROM search_query_log
             WHERE length(trim(query)) >= 3
               AND lower(trim(query)) NOT IN (SELECT query FROM search_interpretation_cache)
             GROUP BY 1 ORDER BY n DESC LIMIT %s
            """,
            (args.top,),
        )
        queries = [r[0] for r in cur.fetchall()]
    conn.close()
    print(f"{len(queries)} uncached queries to warm")

    with httpx.Client(timeout=20.0) as client:
        for q in queries:
            t = time.time()
            try:
                r = client.get(f"{args.base}/api/search/unified",
                               params={"q": q, "limit": 5, "debug": "true"})
                timing = r.json().get("_timing", {})
                print(f"  {time.time() - t:5.1f}s  {q!r}  expanded={timing.get('expansions')}")
            except Exception as e:
                print(f"  failed {q!r}: {e}")
            time.sleep(0.5)  # stay well under the rate limit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

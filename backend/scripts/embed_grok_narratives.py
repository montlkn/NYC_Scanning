"""
Index the cached Kit narratives into building_lore_index.

grok_narratives on MAIN holds LLM-written prose about individual buildings --
309 of them, ~1,692 characters each -- generated and paid for by the app and
never once searched. Its density of the language the LPC reports never use is
an order of magnitude better:

    fire 12%   death 7%   scandal 6%   died 6%   murder 2%   ghost 1%

against LPC designation reports, which describe cornices and fenestration.
That is the content "spooky spots" and "buildings with murder lore" need.

It goes into building_lore_index beside the LPC chunks rather than a new
table: same shape (prose keyed by BIN), so the existing lore leg picks it up
with no query change. `source` distinguishes them, so either can be dropped
or re-weighted later.

This grows on its own -- every Kit narrative the app generates is another row
-- so it is written to be re-run on a cron, embedding only what is new.

Run: python -m scripts.embed_grok_narratives [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time

import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("grok-lore")

PAGE = 500
BATCH = 256
MAX_CHARS = 1800
EMBED_THREADS = int(os.environ.get("EMBED_THREADS", "4"))


def fetch_narratives() -> list[tuple[str, str]]:
    """Page grok_narratives off MAIN via PostgREST (no direct DSN for it)."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/grok_narratives"
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    out, offset = [], 0
    while True:
        r = requests.get(url, params={"select": "bin,narrative",
                                      "limit": PAGE, "offset": offset},
                         headers=headers, timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for row in batch:
            b, n = row.get("bin"), (row.get("narrative") or "").strip()
            if not b or len(n) < 80:
                continue
            b = str(b)
            if b.endswith(".0"):
                b = b[:-2]
            if not (b.isdigit() and len(b) >= 6):
                continue
            out.append((b, n))
        offset += PAGE
        if len(batch) < PAGE:
            break
    return out


def clean(text: str) -> str:
    """Strip the markdown Kit writes. The embedding should see prose, not
    asterisks, and the text doubles as the citation shown to the user."""
    t = re.sub(r"\*{1,2}", "", text)
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)   # [label](url) -> label
    return " ".join(t.split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    rows = fetch_narratives()
    log.info("%d usable narratives on MAIN", len(rows))

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("ALTER TABLE building_lore_index "
                    " ADD COLUMN IF NOT EXISTS source text")
        conn.commit()
        cur.execute("SELECT bin, text_hash FROM building_lore_index")
        seen = set(cur.fetchall())

    prepared = []
    for b, raw in rows:
        t = clean(raw)[:MAX_CHARS]
        h = hashlib.md5(t.encode()).hexdigest()
        if (b, h) in seen:
            continue
        prepared.append((b, t, h))
    log.info("%d new (the rest are already indexed)", len(prepared))

    if args.dry_run or not prepared:
        for b, t, _ in prepared[:3]:
            log.info("  %s  %s...", b, t[:90])
        return 0

    from fastembed import TextEmbedding
    from services.text_embeddings import MODEL_NAME
    model = TextEmbedding(model_name=MODEL_NAME, threads=EMBED_THREADS)
    vecs = []
    for i in range(0, len(prepared), BATCH):
        chunk = prepared[i:i + BATCH]
        vecs += [v.tolist() for v in model.embed([c[1] for c in chunk], batch_size=32)]
        log.info("  embedded %d/%d", len(vecs), len(prepared))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO building_lore_index
                   (bin, text, text_hash, embedding, source_file, source)
            VALUES %s
            ON CONFLICT (bin, text_hash) DO UPDATE
               SET embedding = EXCLUDED.embedding, updated_at = now()
            """,
            [(b, t, h, str(v), "grok_narratives", "kit")
             for (b, t, h), v in zip(prepared, vecs)],
            page_size=200,
        )
        conn.commit()
        cur.execute("SELECT coalesce(source,'lpc'), count(*), count(DISTINCT bin) "
                    "  FROM building_lore_index GROUP BY 1 ORDER BY 2 DESC")
        for src, n, nb in cur.fetchall():
            log.info("  %-5s rows=%-7d bins=%d", src, n, nb)
    conn.close()
    log.info("done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

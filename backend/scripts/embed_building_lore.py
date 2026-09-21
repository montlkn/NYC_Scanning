"""
Populate building_lore_index from the LPC designation-report chunks.

Source: landmark_chunks on $FOOTPRINTS_DB_URL (121,466 rows).
Target: building_lore_index on $SEARCH_DB_URL.

Only specificity='building' chunks are taken — see the migration header for why
the NULL-specificity district boilerplate is excluded.

Distinct texts are embedded ONCE (keyed by md5) and the vector is reused for
every BIN that shares it, so 69,284 chunks cost ~55,087 forward passes.

Run: python -m scripts.embed_building_lore [--limit N] [--resume]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time

import pickle

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.text_embeddings import MODEL_NAME  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("embed_lore")

BATCH = 2048
# The shared services.text_embeddings model is single-threaded on purpose: the
# /search path embeds ONE short query and must not fan out. A 55k-text batch
# job is the opposite shape, so it gets its own model handle across all cores
# (~3.3h single-threaded vs minutes here).
# Measured, not assumed: on a 12-core M-series, threads=4 runs at 43 texts/s
# and threads=12 at 24.9 -- ONNX oversubscribes and the batch gets slower the
# more cores it is given. 4 puts this run at ~21 minutes.
_THREADS = int(os.environ.get("EMBED_THREADS", "4"))


def _batch_model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME, threads=_THREADS)
# bge-small's window is 512 tokens; the longest chunk is 1,500 chars (~375
# tokens) so nothing needs truncating, but keep the guard explicit.
MAX_CHARS = 1800


def _norm_bin(v: str | None) -> str | None:
    """Strip the float-cast '.0' and reject non-numeric BINs (see the 42-row
    unusable-BIN finding); a bad BIN would key lore onto the wrong building."""
    if not v:
        return None
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s if s.isdigit() and len(s) >= 6 else None


def fetch_chunks(limit: int | None):
    dsn = os.environ["FOOTPRINTS_DB_URL"]
    q = """
        SELECT bin, chunk_text, source_file, page_number
          FROM landmark_chunks
         WHERE specificity = 'building'
           AND chunk_text IS NOT NULL
           AND length(chunk_text) >= 40
         ORDER BY bin, chunk_index
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(q)
        return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--resume", action="store_true",
                    help="skip (bin, text_hash) pairs already present")
    args = ap.parse_args()

    t0 = time.time()
    rows = fetch_chunks(args.limit)
    log.info("fetched %d building-specific chunks", len(rows))

    # Deduplicate the work: one forward pass per distinct text.
    prepared = []          # (bin, text, hash, source_file, page)
    by_hash: dict[str, str] = {}
    for b, txt, src, page in rows:
        nb = _norm_bin(b)
        if not nb:
            continue
        t = " ".join(txt.split())[:MAX_CHARS]
        h = hashlib.md5(t.encode()).hexdigest()
        by_hash.setdefault(h, t)
        prepared.append((nb, t, h, src, page))

    # A BIN can carry the same text at two chunk_index values; ON CONFLICT
    # cannot touch one row twice in a single statement, so collapse here.
    seen_pairs: set = set()
    deduped = []
    for row in prepared:
        key = (row[0], row[2])
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deduped.append(row)
    log.info("%d chunks -> %d unique (bin, text) rows, %d distinct texts to embed",
             len(prepared), len(deduped), len(by_hash))
    prepared = deduped

    # Connect ONLY to read progress, then close. The embed phase below runs
    # for hours; a connection opened here and used at the end dies of an idle
    # timeout and takes every vector with it.
    if args.resume:
        with psycopg2.connect(os.environ["SEARCH_DB_URL"]) as c0, c0.cursor() as cur:
            cur.execute("SELECT bin, text_hash FROM building_lore_index")
            seen = set(cur.fetchall())
        before = len(prepared)
        prepared = [p for p in prepared if (p[0], p[2]) not in seen]
        needed = {p[2] for p in prepared}
        by_hash = {h: t for h, t in by_hash.items() if h in needed}
        log.info("resume: %d/%d rows remain, %d texts to embed",
                 len(prepared), before, len(by_hash))

    # Embed distinct texts in batches.
    # Vectors are cached to disk as they are produced: the ONNX work is the
    # expensive part (hours), and losing it to a transient DB or process
    # failure is not acceptable. A rerun reloads instead of recomputing.
    cache_path = os.path.join(os.path.dirname(__file__), ".lore_vecs.pkl")
    vecs: dict[str, list[float]] = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                vecs = pickle.load(fh)
            log.info("loaded %d cached vectors from %s", len(vecs), cache_path)
        except Exception as e:
            log.warning("vector cache unreadable (%s); recomputing", e)

    hashes = [h for h in by_hash if h not in vecs]
    model = _batch_model()
    log.info("embedding %d texts on %d threads", len(hashes), _THREADS)
    for i in range(0, len(hashes), BATCH):
        chunk = hashes[i:i + BATCH]
        # parallel=N (multiprocessing) deadlocks under macOS spawn: workers
        # start, then the pool sits at 0% CPU. threads=N on the model is
        # ONNX intra-op threading, in-process and safe.
        out = model.embed([by_hash[h] for h in chunk], batch_size=32)
        vecs.update((h, v.tolist()) for h, v in zip(chunk, out))
        done = min(i + BATCH, len(hashes))
        with open(cache_path, "wb") as fh:
            pickle.dump(vecs, fh, protocol=pickle.HIGHEST_PROTOCOL)
        log.info("embedded %d/%d (%.0fs)", done, len(hashes), time.time() - t0)

    # Write on a FRESH connection -- the one that read progress is long dead.
    target = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    target.autocommit = False
    written = 0
    with target.cursor() as cur:
        for i in range(0, len(prepared), 1000):
            batch = prepared[i:i + 1000]
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO building_lore_index
                       (bin, text, text_hash, embedding, source_file, page_number)
                VALUES %s
                ON CONFLICT (bin, text_hash) DO UPDATE
                   SET embedding = EXCLUDED.embedding,
                       text       = EXCLUDED.text,
                       updated_at = now()
                """,
                [(b, t, h, str(vecs[h]), src, pg) for b, t, h, src, pg in batch],
                page_size=200,
            )
            written += len(batch)
            target.commit()
            log.info("wrote %d/%d", written, len(prepared))

    with target.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT bin) FROM building_lore_index")
        n, nbins = cur.fetchone()
    target.close()
    log.info("done: %d rows, %d distinct BINs, %.0fs", n, nbins, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

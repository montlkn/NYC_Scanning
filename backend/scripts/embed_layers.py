#!/usr/bin/env python3
"""
Batch-embed the OTHER map layers (lore events, plaques, community contributions)
into the Railway `layer_search_index`.

Mirrors embed_buildings.py: pulls source rows from the MAIN Supabase
(DATABASE_URL) — the same DB embed_buildings reads — builds a descriptive text
per row, embeds it with the SAME bge-small model the /search query path uses
(services.text_embeddings), and upserts id → vector + display columns into
Railway. Idempotent: re-running updates rows; --rebuild re-embeds everything,
otherwise only rows missing from the index are processed.

The iOS app uses these hits to light up + filter the matching map layer, so we
only persist id / coords / title / snippet / year / category per row.

Env:
  MAIN_DB_URL      MAIN Supabase Postgres — holds lore_events / plaques /
                   community_posts. Falls back to DATABASE_URL if unset.
                   NOTE: this is the MAIN project, NOT the buildings project that
                   embed_buildings.py reads via DATABASE_URL.            [one required]
  SEARCH_DB_URL    Railway Postgres (the search index lives here)        [required]

Usage:
  python scripts/embed_layers.py --dry-run          # preview text + counts, no writes
  python scripts/embed_layers.py                    # embed rows not yet indexed
  python scripts/embed_layers.py --rebuild          # re-embed all
  python scripts/embed_layers.py --layer lore       # only one layer
"""

import argparse
import logging
import os
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from services.text_embeddings import embed_texts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("embed_layers")

_JUNK = {"", "none", "null", "n/a", "na", "unknown", "-", "0"}


def _clean(v) -> str:
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in _JUNK else s


def _join(*parts) -> str:
    return ". ".join(p for p in (_clean(x) for x in parts) if p)


# --- Per-layer source config. Each entry: SQL + a row→(text, title, snippet,
#     lat, lng, year, category) mapper. id is prefixed with the layer name. ---

def _lore_text(r):
    text = _join(r.get("title"), r.get("summary"), r.get("category"),
                 r.get("address"), r.get("year"))
    return text, _clean(r.get("title")), _clean(r.get("title")) or _clean(r.get("summary"))[:80], \
        r.get("lat"), r.get("lng"), r.get("year"), _clean(r.get("category")) or None


def _plaque_text(r):
    text = _join(r.get("title"), r.get("inscription"), r.get("subject"),
                 r.get("address"), r.get("year"))
    return text, _clean(r.get("title")), _clean(r.get("title")) or _clean(r.get("subject")), \
        r.get("lat"), r.get("lng"), r.get("year"), _clean(r.get("series")) or None


def _contribution_text(r):
    text = _join(r.get("place_name"), r.get("caption"))
    title = _clean(r.get("place_name")) or _clean(r.get("caption"))[:60]
    return text, title, title, \
        r.get("latitude"), r.get("longitude"), None, None


LAYERS = {
    "lore": {
        "sql": "SELECT id, title, summary, category, lat, lng, address, year "
               "FROM lore_events WHERE title IS NOT NULL",
        "map": _lore_text,
    },
    "plaque": {
        "sql": "SELECT id, title, inscription, subject, series, lat, lng, address, year "
               "FROM plaques WHERE title IS NOT NULL OR inscription IS NOT NULL",
        "map": _plaque_text,
    },
    "contribution": {
        "sql": "SELECT id, place_name, caption, latitude, longitude "
               "FROM community_posts WHERE is_flagged IS NOT TRUE "
               "AND (place_name IS NOT NULL OR caption IS NOT NULL)",
        "map": _contribution_text,
    },
}


def fetch_rows(supa_url, layer, cfg, rebuild, indexed):
    with psycopg.connect(supa_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(cfg["sql"])
        rows = cur.fetchall()
    out = []
    for r in rows:
        rid = f"{layer}:{r['id']}"
        if not rebuild and rid in indexed:
            continue
        text, title, snippet, lat, lng, year, category = cfg["map"](r)
        if not text:
            continue
        out.append({
            "id": rid, "layer": layer, "text": text, "title": title,
            "snippet": snippet, "lat": lat, "lng": lng, "year": year, "category": category,
        })
    return out


def load_indexed(rail_url) -> set:
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM layer_search_index")
        return {row[0] for row in cur.fetchall()}


def upsert(rail_url, batch):
    """batch: list of (id, layer, title, snippet, text, vec_literal, lat, lng, year, category)."""
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO layer_search_index
                (id, layer, title, snippet, text, embedding, lat, lng, year, category, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                layer = EXCLUDED.layer, title = EXCLUDED.title, snippet = EXCLUDED.snippet,
                text = EXCLUDED.text, embedding = EXCLUDED.embedding,
                lat = EXCLUDED.lat, lng = EXCLUDED.lng, year = EXCLUDED.year,
                category = EXCLUDED.category, updated_at = now()
            """,
            batch,
        )
        conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-embed all rows")
    ap.add_argument("--layer", choices=list(LAYERS), default=None, help="only this layer")
    ap.add_argument("--batch-size", type=int, default=128)
    args = ap.parse_args()

    # The lore/plaque/contribution tables live on the MAIN Supabase project, NOT
    # the buildings project that DATABASE_URL points at. Prefer MAIN_DB_URL.
    supa_url = os.environ.get("MAIN_DB_URL") or os.environ.get("DATABASE_URL")
    rail_url = os.environ.get("SEARCH_DB_URL")
    if not supa_url or not rail_url:
        logger.error("MAIN_DB_URL (or DATABASE_URL) and SEARCH_DB_URL must be set")
        sys.exit(1)

    indexed = set() if args.rebuild else load_indexed(rail_url)
    logger.info(f"{len(indexed)} layer rows already indexed")

    layers = [args.layer] if args.layer else list(LAYERS)
    all_rows = []
    for layer in layers:
        rows = fetch_rows(supa_url, layer, LAYERS[layer], args.rebuild, indexed)
        logger.info(f"  {layer}: {len(rows)} to embed")
        all_rows.extend(rows)

    if not all_rows:
        logger.info("nothing to embed")
        return

    if args.dry_run:
        for r in all_rows[:8]:
            logger.info(f"  [{r['id']}] {r['text'][:160]}")
        logger.info(f"dry-run: {len(all_rows)} rows, no writes")
        return

    total = 0
    for i in range(0, len(all_rows), args.batch_size):
        chunk = all_rows[i : i + args.batch_size]
        vectors = embed_texts([r["text"] for r in chunk])
        batch = []
        for r, vec in zip(chunk, vectors):
            batch.append((
                r["id"], r["layer"], r["title"], r["snippet"], r["text"],
                "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                r["lat"], r["lng"], r["year"], r["category"],
            ))
        upsert(rail_url, batch)
        total += len(batch)
        logger.info(f"  upserted {total}/{len(all_rows)}")

    logger.info(f"✅ done — {total} layer rows embedded into layer_search_index")


if __name__ == "__main__":
    main()

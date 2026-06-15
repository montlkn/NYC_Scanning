#!/usr/bin/env python3
"""
Batch-embed curated buildings into the Railway `building_search_index`.

Pulls the curated set from Supabase (`buildings_full_merge_scanning`), builds a
descriptive text per building, embeds it with the SAME bge-small model the
/search query path uses (services.text_embeddings), and upserts BIN → vector +
filter columns into Railway. Idempotent: re-running updates rows; pass
--rebuild to re-embed everything, otherwise only rows missing from the index
are processed.

Env:
  DATABASE_URL         Supabase Postgres (source of truth)         [required]
  FOOTPRINTS_DB_URL    Railway Postgres (the search index lives here) [required]

Usage:
  python scripts/embed_buildings.py --dry-run        # preview text + counts, no writes
  python scripts/embed_buildings.py                  # embed rows not yet indexed
  python scripts/embed_buildings.py --rebuild        # re-embed all
  python scripts/embed_buildings.py --limit 500      # cap (testing)
"""

import argparse
import logging
import os
import sys

import psycopg
from psycopg.rows import dict_row

# Allow `from services...` when run as a script from backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.text_embeddings import embed_texts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("embed_buildings")

SOURCE_COLUMNS = (
    "bin, bbl, building_name, address, architect, style, year_built, landmark, "
    "primary_aesthetic, secondary_aesthetic, storytelling, geocoded_lat, geocoded_lng"
)


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def _parse_int(v):
    try:
        return int(float(str(v))) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _parse_float(v):
    try:
        return float(str(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def build_text(row: dict) -> str:
    """Compose the descriptive string we embed. Order roughly by salience."""
    name = _clean(row.get("building_name"))
    parts = []
    if name and name != "0":
        parts.append(name)
    style = _clean(row.get("style"))
    if style:
        parts.append(f"{style} architecture")
    architect = _clean(row.get("architect"))
    if architect:
        parts.append(f"designed by {architect}")
    year = _parse_int(row.get("year_built"))
    if year:
        parts.append(f"built {year}")
    for key in ("primary_aesthetic", "secondary_aesthetic"):
        val = _clean(row.get(key))
        if val:
            parts.append(val)
    addr = _clean(row.get("address"))
    if addr:
        parts.append(addr)
    story = _clean(row.get("storytelling"))
    if story:
        parts.append(story)
    return ". ".join(parts)


def build_snippet(row: dict) -> str:
    name = _clean(row.get("building_name"))
    if not name or name == "0":
        name = _clean(row.get("address"))
    style = _clean(row.get("style"))
    return f"{name} — {style}" if style else name


def fetch_source_rows(supa_url: str, limit, rebuild: bool, indexed_bins: set) -> list:
    with psycopg.connect(supa_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        sql = f"SELECT {SOURCE_COLUMNS} FROM buildings_full_merge_scanning WHERE bin IS NOT NULL"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bin_clean = _clean(r.get("bin")).removesuffix(".0")
        if not bin_clean:
            continue
        if not rebuild and bin_clean in indexed_bins:
            continue
        r["_bin"] = bin_clean
        out.append(r)
    return out


def load_indexed_bins(rail_url: str) -> set:
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT bin FROM building_search_index")
        return {row[0] for row in cur.fetchall()}


def upsert(rail_url: str, batch: list):
    """batch: list of (bin, bbl, text, snippet, vec_literal, year, is_landmark, lat, lng)."""
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO building_search_index
                (bin, bbl, text, snippet, embedding, year_built, is_landmark, lat, lng, updated_at)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, now())
            ON CONFLICT (bin) DO UPDATE SET
                bbl = EXCLUDED.bbl, text = EXCLUDED.text, snippet = EXCLUDED.snippet,
                embedding = EXCLUDED.embedding, year_built = EXCLUDED.year_built,
                is_landmark = EXCLUDED.is_landmark, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                updated_at = now()
            """,
            batch,
        )
        conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-embed all rows")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=128)
    args = ap.parse_args()

    supa_url = os.environ.get("DATABASE_URL")
    rail_url = os.environ.get("FOOTPRINTS_DB_URL")
    if not supa_url or not rail_url:
        logger.error("DATABASE_URL and FOOTPRINTS_DB_URL must both be set")
        sys.exit(1)

    indexed = set() if args.rebuild else load_indexed_bins(rail_url)
    logger.info(f"{len(indexed)} BINs already indexed")

    rows = fetch_source_rows(supa_url, args.limit, args.rebuild, indexed)
    logger.info(f"{len(rows)} buildings to embed")
    if not rows:
        return

    if args.dry_run:
        for r in rows[:5]:
            logger.info(f"  [{r['_bin']}] {build_text(r)[:160]}")
        logger.info("dry-run: no writes")
        return

    total = 0
    for i in range(0, len(rows), args.batch_size):
        chunk = rows[i : i + args.batch_size]
        texts = [build_text(r) for r in chunk]
        vectors = embed_texts(texts)
        batch = []
        for r, txt, vec in zip(chunk, texts, vectors):
            landmark = _clean(r.get("landmark"))
            batch.append((
                r["_bin"],
                _clean(r.get("bbl")).removesuffix(".0") or None,
                txt,
                build_snippet(r),
                "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                _parse_int(r.get("year_built")),
                bool(landmark and landmark != "0"),
                _parse_float(r.get("geocoded_lat")),
                _parse_float(r.get("geocoded_lng")),
            ))
        upsert(rail_url, batch)
        total += len(batch)
        logger.info(f"  upserted {total}/{len(rows)}")

    logger.info(f"✅ done — {total} buildings embedded into building_search_index")


if __name__ == "__main__":
    main()

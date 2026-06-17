#!/usr/bin/env python3
"""
Seed the `venues` table from Foursquare Open Source Places (HF parquet).

Pipeline per venue:
  1. Read FSQ OSP parquet shard(s) (gated — needs HF_TOKEN), filter to a NYC
     bbox (a slice by default; --citywide for all five boroughs).
  2. Geo-join each venue to its nearest building in `building_search_index`
     (<= JOIN_RADIUS_M), pulling bin/bbl/year_built — the provenance the moat
     depends on.
  3. Embed "{name}. {category leaf}. {address}. in a {year} building" with the
     SAME bge-small model as buildings/search, so venue + building vectors are
     comparable. Provenance (era) is baked INTO the venue's own embedding text.
  4. Upsert to `venues` on the dedicated pgvector DB (SEARCH_DB_URL).

Env:
  HF_TOKEN         Hugging Face read token (dataset is license-gated)  [required]
  SEARCH_DB_URL    pgvector DB (building_search_index + venues live here) [required]

Usage:
  python scripts/seed_venues.py --dry-run            # preview text + counts
  python scripts/seed_venues.py                      # slice (SoHo/LES/Williamsburg)
  python scripts/seed_venues.py --citywide --shards 8
"""

import argparse
import logging
import os
import sys

import duckdb
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from services.text_embeddings import embed_texts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_venues")

HF_RELEASE = "dt=2024-12-03"
HF_BASE = (
    "https://huggingface.co/datasets/foursquare/fsq-os-places/resolve/main/"
    f"release/{HF_RELEASE}/places/parquet"
)

# Geo-join radius: a venue maps to the nearest building within this many meters.
# Building geocodes are centroids, venue coords are entrances → 60m absorbs the
# typical NYC offset without grabbing the building across the street.
JOIN_RADIUS_M = 60

# Default "slice" bboxes — high-density nightlife/retail areas to prove the join.
SLICE_BBOXES = {
    # name: (lat_min, lat_max, lng_min, lng_max)
    "soho_les":     (40.715, 40.730, -74.005, -73.985),
    "williamsburg": (40.705, 40.722, -73.965, -73.945),
}
CITY_BBOX = (40.55, 40.95, -74.05, -73.70)

# Only ingest categories that benefit from a "what's here" search. FSQ labels are
# hierarchical "A > B > C"; we keep these top-level buckets.
KEEP_PREFIXES = (
    "Dining and Drinking",
    "Arts and Entertainment",
    "Retail",
    "Landmarks and Outdoors",
)


def _shard_urls(n: int) -> list:
    return [f"{HF_BASE}/places-{i:05d}.zstd.parquet" for i in range(n)]


def _download_shard(i: int, token: str, cache_dir: str) -> str:
    """Download one FSQ parquet shard to a local cache (idempotent). The dataset
    is license-gated, and this DuckDB build can't send auth headers on httpfs
    reads — so we curl it down with the token, then read locally."""
    import subprocess
    os.makedirs(cache_dir, exist_ok=True)
    local = os.path.join(cache_dir, f"places-{i:05d}.parquet")
    if os.path.exists(local) and os.path.getsize(local) > 1_000_000:
        return local
    url = f"{HF_BASE}/places-{i:05d}.zstd.parquet"
    logger.info(f"  downloading shard {i} ...")
    subprocess.run(
        ["curl", "-sL", "-m", "600", "-H", f"Authorization: Bearer {token}",
         "-o", local, url],
        check=True,
    )
    if os.path.getsize(local) < 1_000_000:
        raise RuntimeError(f"shard {i} download too small — auth/license issue? ({local})")
    return local


def fetch_venues(bboxes: list, shards: int, token: str) -> list:
    """Download FSQ parquet shard(s), filter locally to bbox + category."""
    cache_dir = os.path.join(os.path.dirname(__file__), "..", ".fsq_cache")
    local_files = [_download_shard(i, token, cache_dir) for i in range(shards)]

    con = duckdb.connect()
    bbox_or = " OR ".join(
        f"(latitude BETWEEN {a} AND {b} AND longitude BETWEEN {c} AND {d})"
        for (a, b, c, d) in bboxes
    )
    cat_or = " OR ".join(
        f"list_aggregate(list_transform(fsq_category_labels, x -> x LIKE '{p} >%' OR x = '{p}'), 'bool_or')"
        for p in KEEP_PREFIXES
    )
    file_list = ", ".join(f"'{f}'" for f in local_files)
    sql = f"""
        SELECT fsq_place_id, name, latitude, longitude, address,
               fsq_category_ids, fsq_category_labels
        FROM read_parquet([{file_list}])
        WHERE date_closed IS NULL
          AND name IS NOT NULL
          AND ({bbox_or})
          AND ({cat_or})
    """
    logger.info(f"reading {shards} shard(s), filtering to {len(bboxes)} bbox(es)...")
    return con.execute(sql).fetchall()


def category_leaf(labels) -> str:
    """Most-specific category, e.g. 'Dining and Drinking > Bar > Speakeasy' -> 'Speakeasy'."""
    if not labels:
        return ""
    # Pick the longest (deepest) label, take its leaf segment.
    deepest = max(labels, key=lambda s: s.count(">"))
    return deepest.split(">")[-1].strip()


def load_buildings(rail_url: str) -> list:
    """All building geocodes for the in-memory nearest-neighbor join."""
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT bin, bbl, year_built, snippet, lat, lng FROM building_search_index "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL"
        )
        return cur.fetchall()


def nearest_building(vlat, vlng, buildings, grid):
    """Nearest building within JOIN_RADIUS_M, via a coarse lat/lng grid bucket."""
    import math
    key = (round(vlat, 3), round(vlng, 3))  # ~110m cells
    best, best_d = None, JOIN_RADIUS_M + 1
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for b in grid.get((key[0] + dy * 0.001, key[1] + dx * 0.001), ()):
                _, _, _, _, blat, blng = b
                # equirectangular approx — fine at city scale, fast
                dlat = (blat - vlat) * 111_320
                dlng = (blng - vlng) * 111_320 * math.cos(math.radians(vlat))
                d = math.hypot(dlat, dlng)
                if d < best_d:
                    best, best_d = b, d
    return best


def building_style(snippet) -> str:
    """Style phrase from a building_search_index snippet, e.g.
    '1150 Grand Concourse — art deco with alterations' -> 'art deco with alterations'.
    The snippet is '{name/address} — {style}'; take the part after the em dash.
    Derived dynamically — never a hardcoded style list."""
    if not snippet or "—" not in snippet:
        return ""
    style = snippet.split("—", 1)[1].strip()
    return style if style.lower() not in ("", "unknown", "none") else ""


def build_text(name, cat_leaf, address, byear, style="") -> str:
    parts = [name]
    if cat_leaf:
        parts.append(cat_leaf)
    if address:
        parts.append(address)
    # Fold the host building's architectural style into the embedding so style
    # queries ("art deco bars") can match the venue, not just the building.
    if byear and style:
        parts.append(f"in a {byear} {style} building")
    elif byear:
        parts.append(f"in a {byear} building")
    elif style:
        parts.append(f"in a {style} building")
    return ". ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--citywide", action="store_true", help="all five boroughs (not just the slice)")
    ap.add_argument("--shards", type=int, default=1, help="how many FSQ parquet shards to scan")
    ap.add_argument("--batch-size", type=int, default=128)
    args = ap.parse_args()

    token = os.environ.get("HF_TOKEN")
    rail_url = os.environ.get("SEARCH_DB_URL")
    if not token or not rail_url:
        logger.error("HF_TOKEN and SEARCH_DB_URL must both be set")
        sys.exit(1)

    bboxes = [CITY_BBOX] if args.citywide else list(SLICE_BBOXES.values())
    rows = fetch_venues(bboxes, args.shards, token)
    logger.info(f"{len(rows)} venues after bbox + category filter")
    if not rows:
        return

    # Build the join grid over buildings.
    buildings = load_buildings(rail_url)
    logger.info(f"{len(buildings)} buildings loaded for geo-join")
    grid: dict = {}
    for b in buildings:
        gk = (round(b[4], 3), round(b[5], 3))
        grid.setdefault(gk, []).append(b)

    prepared = []
    joined = 0
    for r in rows:
        fsq_id, name, lat, lng, address, cat_ids, labels = r
        leaf = category_leaf(labels)
        b = nearest_building(lat, lng, buildings, grid)
        bin_ = bbl = byear = None
        style = ""
        if b:
            # b = (bin, bbl, year_built, snippet, lat, lng) — snippet carries style.
            bin_, bbl, byear = b[0], b[1], b[2]
            style = building_style(b[3])
            joined += 1
        prepared.append({
            "fsq_id": fsq_id, "name": name, "lat": lat, "lng": lng,
            "category": leaf, "category_id": (cat_ids[0] if cat_ids else None),
            "address": address, "bin": bin_, "bbl": bbl, "byear": byear,
            "text": build_text(name, leaf, address, byear, style),
            "snippet": f"{name} — {leaf}" if leaf else name,
        })

    logger.info(f"{joined}/{len(prepared)} venues geo-joined to a building (<= {JOIN_RADIUS_M}m)")

    if args.dry_run:
        for p in prepared[:8]:
            tag = f"[bin {p['bin']} · {p['byear']}]" if p["bin"] else "[no building]"
            logger.info(f"  {tag} {p['text'][:140]}")
        logger.info("dry-run: no writes")
        return

    total = 0
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        for i in range(0, len(prepared), args.batch_size):
            chunk = prepared[i : i + args.batch_size]
            vectors = embed_texts([p["text"] for p in chunk])
            batch = [
                (
                    p["fsq_id"], p["name"], p["category"], p["category_id"],
                    p["text"], p["snippet"],
                    "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                    p["lat"], p["lng"], p["bin"], p["bbl"], p["byear"], None,
                )
                for p, v in zip(chunk, vectors)
            ]
            cur.executemany(
                """
                INSERT INTO venues
                    (fsq_id, name, category, category_id, text, snippet, embedding,
                     lat, lng, bin, bbl, building_year, building_style, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (fsq_id) DO UPDATE SET
                    name=EXCLUDED.name, category=EXCLUDED.category,
                    category_id=EXCLUDED.category_id, text=EXCLUDED.text,
                    snippet=EXCLUDED.snippet, embedding=EXCLUDED.embedding,
                    lat=EXCLUDED.lat, lng=EXCLUDED.lng, bin=EXCLUDED.bin,
                    bbl=EXCLUDED.bbl, building_year=EXCLUDED.building_year,
                    updated_at=now()
                """,
                batch,
            )
            conn.commit()
            total += len(batch)
            logger.info(f"  upserted {total}/{len(prepared)}")

    logger.info(f"✅ done — {total} venues seeded ({joined} with building provenance)")


if __name__ == "__main__":
    main()

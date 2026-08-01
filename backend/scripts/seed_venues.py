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
from typing import Optional

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
               fsq_category_ids, fsq_category_labels,
               instagram, website, tel
        FROM read_parquet([{file_list}])
        WHERE date_closed IS NULL
          AND name IS NOT NULL
          AND ({bbox_or})
          AND ({cat_or})
    """
    logger.info(f"reading {shards} shard(s), filtering to {len(bboxes)} bbox(es)...")
    return con.execute(sql).fetchall()


def category_leaf(labels) -> str:
    """Most-specific category, e.g. 'Dining and Drinking > Bar > Speakeasy' -> 'Speakeasy'.

    FSQ gives a LIST of labels and this keeps exactly one. `max()` on
    `>`-count returns the FIRST element on a depth tie, i.e. arbitrary parquet
    order — so a place labelled both 'Arts and Entertainment > Art Gallery'
    AND 'Dining and Drinking > Bar' got whichever came first, and the
    corroborating label that would have outvoted it was thrown away. That is
    how an art gallery ended up typed BAR and ranked #1 for "art deco bars
    near me". `category_labels` below preserves the rest so the ranker can
    see the conflict.
    """
    if not labels:
        return ""
    # Deterministic on ties (deepest, then alphabetical) so re-ingesting
    # unchanged data produces an unchanged column.
    deepest = max(labels, key=lambda s: (s.count(">"), s))
    return deepest.split(">")[-1].strip()


def category_labels(labels) -> list:
    """Every category label's leaf segment, deduped, order-stable."""
    if not labels:
        return []
    out, seen = [], set()
    for label in labels:
        leaf = label.split(">")[-1].strip()
        if leaf and leaf.lower() not in seen:
            seen.add(leaf.lower())
            out.append(leaf)
    return out


def category_top_levels(labels) -> list:
    """Top-level FSQ domains ('Dining and Drinking', 'Arts and Entertainment').
    Two different domains on one venue is the mislabel signal."""
    if not labels:
        return []
    out, seen = [], set()
    for label in labels:
        top = label.split(">")[0].strip()
        if top and top.lower() not in seen:
            seen.add(top.lower())
            out.append(top)
    return out


def normalize_instagram(raw) -> Optional[str]:
    """Bare IG handle from whatever FSQ stored (URL, @handle, or handle). The app
    deep-links `instagram://user?username=<handle>`, so strip URL/@/slashes."""
    if not raw:
        return None
    h = str(raw).strip()
    if not h:
        return None
    low = h.lower()
    if "instagram.com/" in low:
        h = h[low.index("instagram.com/") + len("instagram.com/"):]
    h = h.strip("@/ ")
    h = h.split("/")[0].split("?")[0]
    return h or None


def clean_text(raw) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def load_buildings(rail_url: str) -> list:
    """All building geocodes for the in-memory nearest-neighbor join.

    LEGACY fallback path — only used when FOOTPRINTS_DB_URL is unset. This
    reads `building_search_index`, which is the ~35k curated LANDMARK set, not
    the city. Joining venues against it left 79% of them with no building at
    all (and therefore no year and no style), which is why an "art deco bar"
    query had almost nothing to work with. See join_buildings_via_footprints.
    """
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT bin, bbl, year_built, snippet, lat, lng FROM building_search_index "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL"
        )
        return cur.fetchall()


def nearest_building(vlat, vlng, buildings, grid):
    """Nearest building within JOIN_RADIUS_M, via a coarse lat/lng grid bucket.
    Legacy in-memory path — see load_buildings."""
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


# ---------------------------------------------------------------------------
# Footprint-based geo-join (the real one).
#
# Three sources, three different databases/tables, in priority order:
#
#   building_footprints   (FOOTPRINTS_DB, 1.08M rows) — the spatial index.
#                         Gives bin + bbl + construction_year for essentially
#                         every structure in NYC. GiST index on `centroid`, so
#                         KNN is cheap.
#   pluto_buildings       (FOOTPRINTS_DB, 858k rows) — year_built + building
#                         class, keyed by BBL. Has NO coordinates, so it can
#                         only be reached through the footprint's bbl.
#   building_search_index (SEARCH_DB, 35k rows) — the curated landmark set,
#                         the ONLY source of architectural STYLE.
#
# Era therefore covers ~the whole city while style stays limited to the curated
# set — which is the correct shape, because `venue_style_affinity` scores a
# venue on its building's ERA when no style string exists (deco = 1920-1941).
# ---------------------------------------------------------------------------

# Venues are sent to the footprints DB in chunks; one round-trip per chunk
# rather than per venue.
JOIN_CHUNK = 2000


def join_buildings_via_footprints(fp_url: str, search_url: str, venues: list) -> dict:
    """Nearest footprint within JOIN_RADIUS_M for each venue.

    `venues` is the raw parquet row list; returns {index: (bin, bbl, year, style)}
    for the ones that joined. Indices absent from the dict didn't join.
    """
    out: dict = {}
    with psycopg.connect(fp_url) as conn, conn.cursor() as cur:
        for start in range(0, len(venues), JOIN_CHUNK):
            chunk = venues[start : start + JOIN_CHUNK]
            # (idx, lat, lng) tuples; skip rows with no coordinate.
            vals = [
                (start + i, float(r[2]), float(r[3]))
                for i, r in enumerate(chunk)
                if r[2] is not None and r[3] is not None
            ]
            if not vals:
                continue
            placeholders = ",".join(["(%s,%s::float8,%s::float8)"] * len(vals))
            params: list = []
            for v in vals:
                params.extend(v)
            params.append(JOIN_RADIUS_M)
            cur.execute(
                f"""
                WITH v(idx, lat, lng) AS (VALUES {placeholders})
                SELECT v.idx, f.bin, f.bbl, f.construction_year
                FROM v
                CROSS JOIN LATERAL (
                    SELECT bin, bbl, construction_year, centroid
                    FROM building_footprints
                    WHERE centroid IS NOT NULL
                    -- <-> against the GiST index: index-assisted KNN, so this
                    -- is a bounded lookup, not a scan of 1.08M rows.
                    ORDER BY centroid <-> ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326)
                    LIMIT 1
                ) f
                WHERE ST_DWithin(
                    f.centroid::geography,
                    ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326)::geography,
                    %s
                )
                """,
                params,
            )
            for idx, bin_, bbl, year in cur.fetchall():
                out[idx] = [bin_, bbl, year, ""]
            logger.info(f"  geo-join {min(start + JOIN_CHUNK, len(venues))}/{len(venues)}")

        # PLUTO year_built fills gaps where the footprint has no
        # construction_year (a large share of the DOB footprint set).
        bbls = {v[1] for v in out.values() if v[1]}
        if bbls:
            cur.execute(
                "SELECT bbl, year_built FROM pluto_buildings "
                "WHERE bbl = ANY(%s) AND year_built IS NOT NULL AND year_built > 1600",
                (list(bbls),),
            )
            pluto_years = dict(cur.fetchall())
            filled = 0
            for v in out.values():
                if not v[2] and v[1] and pluto_years.get(v[1]):
                    v[2] = pluto_years[v[1]]
                    filled += 1
            logger.info(f"  PLUTO filled {filled} missing year_built values")

    # Style, from the curated landmark index on the OTHER database.
    bins = {v[0] for v in out.values() if v[0]}
    if bins:
        with psycopg.connect(search_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT bin, snippet FROM building_search_index WHERE bin = ANY(%s)",
                (list(bins),),
            )
            styles = {b: building_style(s) for b, s in cur.fetchall()}
        styled = 0
        for v in out.values():
            s = styles.get(v[0])
            if s:
                v[3] = s
                styled += 1
        logger.info(f"  {styled} venues inherited a building STYLE from the curated index")

    return out


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

    # Geo-join. Prefer the full 1.08M-row footprint set; fall back to the
    # legacy 35k landmark index only if the footprints DB isn't configured.
    fp_url = os.environ.get("FOOTPRINTS_DB_URL")
    joins: dict = {}
    buildings: list = []
    grid: dict = {}
    if fp_url:
        logger.info("geo-joining against building_footprints (PostGIS)...")
        joins = join_buildings_via_footprints(fp_url, rail_url, rows)
    else:
        logger.warning(
            "FOOTPRINTS_DB_URL unset — falling back to the 35k landmark index. "
            "Most venues will get no building, year or style."
        )
        buildings = load_buildings(rail_url)
        logger.info(f"{len(buildings)} buildings loaded for geo-join")
        for b in buildings:
            gk = (round(b[4], 3), round(b[5], 3))
            grid.setdefault(gk, []).append(b)

    prepared = []
    joined = 0
    with_year = 0
    with_style = 0
    for idx, r in enumerate(rows):
        fsq_id, name, lat, lng, address, cat_ids, labels, instagram, website, tel = r
        leaf = category_leaf(labels)
        bin_ = bbl = byear = None
        style = ""
        if fp_url:
            j = joins.get(idx)
            if j:
                bin_, bbl, byear, style = j[0], j[1], j[2], j[3]
                joined += 1
        else:
            b = nearest_building(lat, lng, buildings, grid)
            if b:
                # b = (bin, bbl, year_built, snippet, lat, lng) — snippet carries style.
                bin_, bbl, byear = b[0], b[1], b[2]
                style = building_style(b[3])
                joined += 1
        if byear:
            with_year += 1
        if style:
            with_style += 1
        prepared.append({
            "fsq_id": fsq_id, "name": name, "lat": lat, "lng": lng,
            "category": leaf, "category_id": (cat_ids[0] if cat_ids else None),
            # All labels + top-level domains preserved so the ranker can tell a
            # genuine bar from a gallery that FSQ also tagged "Bar".
            "category_labels": category_labels(labels),
            "category_domains": category_top_levels(labels),
            "address": address, "bin": bin_, "bbl": bbl, "byear": byear,
            "instagram": normalize_instagram(instagram),
            "website": clean_text(website), "tel": clean_text(tel),
            "text": build_text(name, leaf, address, byear, style),
            "snippet": f"{name} — {leaf}" if leaf else name,
        })

    n = len(prepared) or 1
    logger.info(
        f"{joined}/{len(prepared)} venues geo-joined to a building "
        f"(<= {JOIN_RADIUS_M}m)  [{100 * joined // n}%]"
    )
    # Year and style are reported separately because they matter for different
    # queries: ERA drives the "art deco bar" affinity for the whole city, STYLE
    # only exists for the curated landmark subset.
    logger.info(
        f"  provenance: {with_year} with a build YEAR ({100 * with_year // n}%), "
        f"{with_style} with a STYLE ({100 * with_style // n}%)"
    )

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
            # photo_url: FSQ Open Source Places (fsq-os-places, dt=2024-12-03) has
            # NO photo/image field in its schema (verified against the parquet
            # columns actually read in fetch_venues()) — left NULL, not invented.
            batch = [
                (
                    p["fsq_id"], p["name"], p["category"], p["category_id"],
                    p["category_labels"], p["category_domains"],
                    p["text"], p["snippet"],
                    "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                    p["lat"], p["lng"], p["bin"], p["bbl"], p["byear"], None,
                    p["instagram"], p["website"], p["tel"], None,
                )
                for p, v in zip(chunk, vectors)
            ]
            cur.executemany(
                """
                INSERT INTO venues
                    (fsq_id, name, category, category_id,
                     category_labels, category_domains, text, snippet, embedding,
                     lat, lng, bin, bbl, building_year, building_style,
                     instagram, website, tel, photo_url, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (fsq_id) DO UPDATE SET
                    name=EXCLUDED.name, category=EXCLUDED.category,
                    category_id=EXCLUDED.category_id,
                    category_labels=EXCLUDED.category_labels,
                    category_domains=EXCLUDED.category_domains, text=EXCLUDED.text,
                    snippet=EXCLUDED.snippet, embedding=EXCLUDED.embedding,
                    lat=EXCLUDED.lat, lng=EXCLUDED.lng, bin=EXCLUDED.bin,
                    bbl=EXCLUDED.bbl, building_year=EXCLUDED.building_year,
                    instagram=EXCLUDED.instagram, website=EXCLUDED.website,
                    tel=EXCLUDED.tel, photo_url=EXCLUDED.photo_url, updated_at=now()
                """,
                batch,
            )
            conn.commit()
            total += len(batch)
            logger.info(f"  upserted {total}/{len(prepared)}")

    logger.info(f"✅ done — {total} venues seeded ({joined} with building provenance)")


if __name__ == "__main__":
    main()

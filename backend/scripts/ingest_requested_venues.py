#!/usr/bin/env python3
"""
Add the places people found through Apple Maps to our own `venues`.

When a search has nothing of ours for a place ("moonstruck diner", "chess
forum"), the app shows Apple's card, and a tap on it writes a row to MAIN
`venue_requests`. This job turns those rows into real venues: joined to the
building they are in (bin, year, style, the same footprint join seed_venues
uses), embedded with the same model, and enriched. Next time the search finds
it as ours, read through its building and that building's story, instead of a
bare Apple card.

It only adds what people asked for, so it stays small: a few rows a night.

Per request group (same name within ~30 m):
  - already in venues (same normalized name within 60 m) -> 'exists'
  - otherwise inserted with source='apple_request'      -> 'added'
Every request in the group is marked processed with that result.

Env:
  MAIN_DB_URL         MAIN Postgres, owner login (reads + marks venue_requests)
  SEARCH_DB_URL       pgvector DB, WRITE login (the cron passes SEARCH_DB_WRITE_URL)
  FOOTPRINTS_DB_URL   building footprints, for the building join (optional:
                      without it a venue is still added, with no building)

Usage:
  python -m scripts.ingest_requested_venues --dry-run
  python -m scripts.ingest_requested_venues --limit 200
"""
import argparse
import logging
import math
import os
import re
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from scripts.seed_venues import JOIN_RADIUS_M, build_text, join_buildings_via_footprints  # noqa: E402
from services.text_embeddings import embed_texts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_requested_venues")

GROUP_RADIUS_M = 30
EXISTS_RADIUS_M = 60


def norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", "", (name or "").lower().replace("&", " and "))
    return " ".join(w for w in s.split() if w != "the")


def meters(a_lat, a_lng, b_lat, b_lng) -> float:
    dlat = (b_lat - a_lat) * 111_320
    dlng = (b_lng - a_lng) * 111_320 * math.cos(math.radians(a_lat))
    return math.hypot(dlat, dlng)


def venue_id(name: str, lat: float, lng: float) -> str:
    """Stable key in the fsq_id column, which every venue row uses as its id.
    Prefixed so these can never collide with a Foursquare id and are one
    DELETE away from reverted (with source='apple_request')."""
    slug = re.sub(r"\s+", "-", norm(name))[:60]
    return f"apple:{lat:.5f},{lng:.5f}:{slug}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=200, help="max requests per run")
    args = ap.parse_args()

    main_url = os.environ.get("MAIN_DB_URL")
    search_url = os.environ.get("SEARCH_DB_URL")
    fp_url = os.environ.get("FOOTPRINTS_DB_URL")
    if not main_url or not search_url:
        logger.error("MAIN_DB_URL and SEARCH_DB_URL must both be set")
        return 1

    with psycopg.connect(main_url) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, category, address, lat, lng
                 FROM public.venue_requests
                WHERE processed_at IS NULL
                ORDER BY created_at
                LIMIT %s""",
            (args.limit,),
        )
        requests = cur.fetchall()
    logger.info(f"{len(requests)} unprocessed requests")
    if not requests:
        return 0

    # Group taps on the same place (several people, or one person twice).
    groups: list = []
    for rid, name, cat, addr, lat, lng in requests:
        for g in groups:
            if norm(g["name"]) == norm(name) and meters(g["lat"], g["lng"], lat, lng) <= GROUP_RADIUS_M:
                g["ids"].append(rid)
                break
        else:
            groups.append({"ids": [rid], "name": name, "category": cat, "address": addr, "lat": lat, "lng": lng})
    logger.info(f"{len(groups)} distinct places")

    # Already ours? Same normalized name within EXISTS_RADIUS_M.
    with psycopg.connect(search_url) as conn, conn.cursor() as cur:
        for g in groups:
            cur.execute(
                """SELECT name, lat, lng FROM venues
                    WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s""",
                (g["lat"] - 0.001, g["lat"] + 0.001, g["lng"] - 0.0013, g["lng"] + 0.0013),
            )
            g["exists"] = any(
                norm(n) == norm(g["name"]) and meters(g["lat"], g["lng"], la, ln) <= EXISTS_RADIUS_M
                for n, la, ln in cur.fetchall() if la is not None and ln is not None
            )
    new = [g for g in groups if not g["exists"]]
    logger.info(f"{len(groups) - len(new)} already in venues, {len(new)} to add")

    # Building join, in the parquet-row shape join_buildings_via_footprints reads
    # (index 2 = lat, 3 = lng).
    joins = {}
    if fp_url and new:
        rows = [(None, g["name"], g["lat"], g["lng"]) for g in new]
        joins = join_buildings_via_footprints(fp_url, search_url, rows)
    for i, g in enumerate(new):
        j = joins.get(i)
        g["bin"], g["bbl"], g["byear"], g["style"] = (j[0], j[1], j[2], j[3]) if j else (None, None, None, "")
        g["text"] = build_text(g["name"], g["category"], g["address"], g["byear"], g["style"])
        g["fsq_id"] = venue_id(g["name"], g["lat"], g["lng"])
    joined = sum(1 for g in new if g["bin"])
    logger.info(f"{joined}/{len(new)} joined to a building (<= {JOIN_RADIUS_M}m)")

    if args.dry_run:
        for g in new[:20]:
            tag = f"[bin {g['bin']} · {g['byear']}]" if g["bin"] else "[no building]"
            logger.info(f"  {tag} {g['text'][:140]}")
        logger.info("dry-run: no writes")
        return 0

    if new:
        vectors = embed_texts([g["text"] for g in new])
        with psycopg.connect(search_url) as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE venues ADD COLUMN IF NOT EXISTS source text")
            cur.executemany(
                """INSERT INTO venues
                       (fsq_id, name, category, text, snippet, embedding,
                        lat, lng, bin, bbl, building_year, building_style, source, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,'apple_request', now())
                   ON CONFLICT (fsq_id) DO NOTHING""",
                [
                    (g["fsq_id"], g["name"], g["category"], g["text"],
                     f"{g['name']} — {g['category']}" if g["category"] else g["name"],
                     "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                     g["lat"], g["lng"], g["bin"], g["bbl"], g["byear"], g["style"] or None)
                    for g, v in zip(new, vectors)
                ],
            )
            conn.commit()
        logger.info(f"inserted {len(new)} venues")

    with psycopg.connect(main_url) as conn, conn.cursor() as cur:
        for g in groups:
            result = "exists" if g["exists"] else ("added" if g.get("bin") else "added_no_building")
            cur.execute(
                "UPDATE public.venue_requests SET processed_at = now(), result = %s WHERE id = ANY(%s)",
                (result, g["ids"]),
            )
        conn.commit()

    # New rows have no neighborhood or NYC flag yet; enrich before anyone
    # searches them (same step seed_venues ends with).
    if new:
        from scripts.enrich_venues import run as enrich_venues
        enrich_venues()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

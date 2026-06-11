#!/usr/bin/env python3
"""
One-time export: Railway `building_footprints` -> static JSON tiles on R2.

The iOS scan overlay does identification fully on-device. It needs building
footprints near the user with zero server on the critical path, so we
pre-slice the city into fixed lat/lng grid cells and upload one gzipped JSON
file per cell to the public R2 bucket. The client computes the same cell key
from its GPS fix and fetches the 3x3 neighbourhood (CDN-cached, then
disk-cached on device).

Tile scheme (must match iOS ScanFootprintService):
    cell size   : 0.005 deg lat x 0.005 deg lng  (~555m x ~420m in NYC)
    cell key    : "{floor(lat/0.005)}_{floor(lng/0.005)}"  (indices, not degrees)
    R2 key      : footprints/v1/{cell_key}.json   (gzip body, Content-Encoding: gzip)
    URL         : {R2_PUBLIC_URL}/footprints/v1/{cell_key}.json

Tile payload: JSON array, one object per building whose footprint centroid
falls in the cell:
    b : BIN (string, no ".0")
    n : name (omitted when null/"0"/empty)
    h : height_roof in metres (omitted when null)
    c : [lat, lng] centroid
    r : [[[lat, lng], ...], ...]  outer ring per polygon, simplified ~0.5m

Usage:
    cd backend && python ../scripts/generate_footprint_tiles.py            # full run
    python ../scripts/generate_footprint_tiles.py --dry-run               # no upload, write ./tiles_out
    python ../scripts/generate_footprint_tiles.py --limit 5000            # smoke test
    python ../scripts/generate_footprint_tiles.py --only-cell 8147_-14794 # regen one cell

Requires backend/.env with a CURRENT FOOTPRINTS_DB_URL (Railway) and the R2_*
keys. Resumable: tiles are built fully in memory per run but upload is
idempotent (same key, overwrite), so re-running is safe.
"""

import argparse
import gzip
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import boto3
import psycopg2
from botocore.client import Config

CELL_DEG = 0.005
SIMPLIFY_DEG = 0.000005  # ~0.5m
GEOJSON_PRECISION = 6
R2_PREFIX = "footprints/v1"

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"


def load_env():
    env = {}
    env_path = BACKEND_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    env.update(os.environ)  # real env wins over .env
    return env


def pg_url(env):
    url = env.get("FOOTPRINTS_DB_URL")
    if not url:
        sys.exit("FOOTPRINTS_DB_URL not set (backend/.env or environment)")
    return re.sub(r"^postgres(ql)?\+\w+", "postgresql", url).split("?")[0]


def cell_key(lat: float, lng: float) -> str:
    return f"{math.floor(lat / CELL_DEG)}_{math.floor(lng / CELL_DEG)}"


def clean_bin(value) -> str:
    s = str(value)
    return s[:-2] if s.endswith(".0") else s


def outer_rings(geojson_str: str):
    """GeoJSON (Multi)Polygon -> list of outer rings as [lat, lng] pairs."""
    g = json.loads(geojson_str)
    if g["type"] == "Polygon":
        polys = [g["coordinates"]]
    elif g["type"] == "MultiPolygon":
        polys = g["coordinates"]
    else:
        return []
    rings = []
    for poly in polys:
        if not poly:
            continue
        # GeoJSON ring is [lng, lat]; tiles store [lat, lng] (client convention).
        rings.append([[pt[1], pt[0]] for pt in poly[0]])
    return rings


def fetch_buildings(conn, limit=None):
    """Server-side cursor over the whole table; yields tile-ready dicts."""
    cur = conn.cursor(name="fp_export")
    cur.itersize = 5000
    sql = f"""
        SELECT
            bin,
            name,
            height_roof,
            ST_Y(ST_Centroid(footprint)) AS lat,
            ST_X(ST_Centroid(footprint)) AS lng,
            ST_AsGeoJSON(ST_SimplifyPreserveTopology(footprint, {SIMPLIFY_DEG}), {GEOJSON_PRECISION}) AS gj
        FROM building_footprints
        WHERE footprint IS NOT NULL
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    for bin_, name, height, lat, lng, gj in cur:
        if lat is None or lng is None or not gj:
            continue
        entry = {
            "b": clean_bin(bin_),
            "c": [round(lat, GEOJSON_PRECISION), round(lng, GEOJSON_PRECISION)],
            "r": outer_rings(gj),
        }
        if name and str(name).strip() and str(name).strip() != "0":
            entry["n"] = str(name).strip()
        if height is not None:
            try:
                h = float(height)
                if h > 0:
                    entry["h"] = round(h, 1)
            except (TypeError, ValueError):
                pass
        yield entry
    cur.close()


def r2_client(env):
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write ./tiles_out instead of uploading")
    ap.add_argument("--limit", type=int, default=None, help="row limit for smoke tests")
    ap.add_argument("--only-cell", default=None, help="only emit/upload this cell key")
    args = ap.parse_args()

    env = load_env()
    conn = psycopg2.connect(pg_url(env))
    conn.set_session(readonly=True)

    print("building tiles…")
    t0 = time.time()
    tiles = defaultdict(list)
    n = 0
    for entry in fetch_buildings(conn, args.limit):
        key = cell_key(entry["c"][0], entry["c"][1])
        if args.only_cell and key != args.only_cell:
            continue
        tiles[key].append(entry)
        n += 1
        if n % 100_000 == 0:
            print(f"  {n} buildings -> {len(tiles)} cells ({time.time()-t0:.0f}s)")
    conn.close()
    print(f"done: {n} buildings in {len(tiles)} cells ({time.time()-t0:.0f}s)")

    if args.dry_run:
        out = Path("tiles_out")
        out.mkdir(exist_ok=True)
        total = 0
        for key, entries in tiles.items():
            body = gzip.compress(json.dumps(entries, separators=(",", ":")).encode())
            (out / f"{key}.json.gz").write_bytes(body)
            total += len(body)
        print(f"dry run: wrote {len(tiles)} files, {total/1e6:.1f}MB gz to {out}/")
        return

    s3 = r2_client(env)
    bucket = env.get("R2_BUCKET", "building-images")
    uploaded = 0
    total_bytes = 0
    for key, entries in sorted(tiles.items()):
        body = gzip.compress(json.dumps(entries, separators=(",", ":")).encode())
        s3.put_object(
            Bucket=bucket,
            Key=f"{R2_PREFIX}/{key}.json",
            Body=body,
            ContentType="application/json",
            ContentEncoding="gzip",
            CacheControl="public, max-age=2592000",  # footprints are near-static; 30d
        )
        uploaded += 1
        total_bytes += len(body)
        if uploaded % 500 == 0:
            print(f"  uploaded {uploaded}/{len(tiles)} ({total_bytes/1e6:.1f}MB)")
    print(f"uploaded {uploaded} tiles, {total_bytes/1e6:.1f}MB gz -> r2://{bucket}/{R2_PREFIX}/")
    print(f"public base: {env.get('R2_PUBLIC_URL', '<R2_PUBLIC_URL>')}/{R2_PREFIX}/")


if __name__ == "__main__":
    main()

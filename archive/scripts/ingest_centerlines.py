"""
One-off: load NYC Centerline CSV into the Railway footprints DB.

Source: NYC OpenData "Centerline" — https://data.cityofnewyork.us/.../8rma-cm9c
Expected CSV path is passed via --csv. The Modal Secret `FOOTPRINTS_DB_URL`
or your local backend/.env value is used for the DB connection.

Usage:
    cd /Users/lucienmount/coding/nyc_scan
    source venv/bin/activate
    python -m backend.scripts.ingest_centerlines --csv /path/to/Centerline.csv

The script is idempotent: it CREATEs the table IF NOT EXISTS, then UPSERTs by
physical_id. Re-running over the same CSV is safe.
"""

import argparse
import asyncio
import csv
import os
import sys
import time
from pathlib import Path

import asyncpg

# Allow `python backend/scripts/foo.py` and `python -m backend.scripts.foo`
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Lightweight .env loader so we don't depend on pydantic settings here.
def _load_env(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS street_centerlines (
    physical_id      BIGINT PRIMARY KEY,
    full_street_name TEXT,
    borough_code     SMALLINT,
    geom             geometry(MultiLineString, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_street_centerlines_geom
    ON street_centerlines USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_street_centerlines_borough
    ON street_centerlines (borough_code);
"""


# The CSV's PHYSICALID and Borough Code come in as comma-formatted strings
# ("46,810" or "3") and the borough code field is sometimes blank. Handle both.
def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_row(row: dict) -> tuple | None:
    geom = (row.get("the_geom") or "").strip()
    if not geom:
        return None
    pid = _int_or_none(row.get("PHYSICALID"))
    if pid is None:
        return None
    name = (row.get("Full Street Name") or "").strip() or None
    boro = _int_or_none(row.get("Borough Code"))
    return (pid, name, boro, geom)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to Centerline CSV")
    ap.add_argument("--batch", type=int, default=2000, help="Rows per INSERT batch")
    args = ap.parse_args()

    env = _load_env(_REPO / "backend" / ".env")
    db_url = os.environ.get("FOOTPRINTS_DB_URL") or env.get("FOOTPRINTS_DB_URL")
    if not db_url:
        print("FOOTPRINTS_DB_URL missing from env / backend/.env", file=sys.stderr)
        sys.exit(1)

    # asyncpg expects raw postgres:// (no driver prefix, no ?ssl=…)
    raw = db_url
    if raw.startswith("postgresql+asyncpg://"):
        raw = raw.replace("postgresql+asyncpg://", "postgres://", 1)
    elif raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgres://", 1)
    if "?" in raw:
        raw = raw.split("?", 1)[0]

    conn = await asyncpg.connect(raw, ssl="require")
    try:
        print("→ ensuring schema…")
        # asyncpg cannot run multi-statement DDL in one call; split on ;
        for stmt in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
            await conn.execute(stmt)

        path = Path(args.csv)
        if not path.exists():
            print(f"CSV not found: {path}", file=sys.stderr)
            sys.exit(1)

        upsert = """
            INSERT INTO street_centerlines
                (physical_id, full_street_name, borough_code, geom)
            VALUES
                ($1, $2, $3, ST_GeomFromText($4, 4326))
            ON CONFLICT (physical_id) DO UPDATE
              SET full_street_name = EXCLUDED.full_street_name,
                  borough_code     = EXCLUDED.borough_code,
                  geom             = EXCLUDED.geom
        """

        t0 = time.time()
        n_ok = 0
        n_bad = 0
        batch: list[tuple] = []

        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                parsed = _parse_row(row)
                if parsed is None:
                    n_bad += 1
                    continue
                batch.append(parsed)
                if len(batch) >= args.batch:
                    await conn.executemany(upsert, batch)
                    n_ok += len(batch)
                    batch = []
                    print(f"  {n_ok:>7} rows  ({time.time() - t0:.1f}s)")

        if batch:
            await conn.executemany(upsert, batch)
            n_ok += len(batch)

        print(f"✓ {n_ok} rows inserted/updated, {n_bad} skipped, {time.time() - t0:.1f}s total")

        total = await conn.fetchval("SELECT COUNT(*) FROM street_centerlines")
        print(f"✓ street_centerlines now contains {total} rows")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

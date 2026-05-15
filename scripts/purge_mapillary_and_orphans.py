"""
Purge poisoned reference_embeddings rows and orphaned R2 keys.

Three actions, all DRY-RUN by default:

  1. (SQL, you run) DELETE FROM reference_embeddings
     WHERE reference_source IN ('mapillary','mapillary_pano')
     — 18 known rows from the pre-2026-05-15 Mapillary chain.

  2. (this script) Walk R2 'ondemand/*' and list keys with NO matching
     `reference_embeddings.image_key` row. Those are phantoms from the
     pre-fix `cache_embedding` bug where DB rows were written before R2
     upload. Default: print the keys. With --apply: delete them from R2.

  3. (SQL, --purge-untagged only) DELETE FROM reference_embeddings
     WHERE reference_source IS NULL — 5,618 unauditable pre-tag rows.
     Lazy refetch via Google will repopulate as needed.

Why this script doesn't touch the DB directly: the buildings DB live
behind a separate Supabase project that the operator runs SQL against by
hand. So we emit copy-pasteable SQL and only do the R2 walk locally.

Usage:
    python scripts/purge_mapillary_and_orphans.py            # dry-run, print plan
    python scripts/purge_mapillary_and_orphans.py --apply    # actually delete R2 orphans
    python scripts/purge_mapillary_and_orphans.py --purge-untagged   # also show SQL for NULL-source rows
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import boto3
from botocore.client import Config
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / "backend" / ".env")


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _r2_bucket() -> str:
    return os.environ["R2_BUCKET"]


def list_ondemand_keys(client, bucket: str) -> list[str]:
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="ondemand/"):
        for obj in page.get("Contents") or []:
            keys.append(obj["Key"])
    return keys


async def known_image_keys() -> set[str]:
    """Pull every `image_key` referenced by reference_embeddings.

    Uses the same DATABASE_URL as the running backend (Supabase buildings DB).
    """
    db_url = os.environ.get("DATABASE_URL") or os.environ["SCAN_DB_URL"]
    # asyncpg wants plain postgres:// without sslmode params
    raw_url = db_url.split("?")[0]
    if raw_url.startswith("postgresql+psycopg://"):
        raw_url = raw_url.replace("postgresql+psycopg://", "postgresql://", 1)

    conn = await asyncpg.connect(raw_url, ssl="require")
    try:
        rows = await conn.fetch(
            "SELECT image_key FROM reference_embeddings WHERE image_key IS NOT NULL"
        )
    finally:
        await conn.close()
    return {r["image_key"] for r in rows}


def sql_block_mapillary() -> str:
    return """
-- Step 1: drop Mapillary-tagged rows (run against the buildings DB)
BEGIN;
SELECT COUNT(*) AS to_delete
FROM reference_embeddings
WHERE reference_source IN ('mapillary','mapillary_pano');

DELETE FROM reference_embeddings
WHERE reference_source IN ('mapillary','mapillary_pano');
COMMIT;
""".strip()


def sql_block_untagged() -> str:
    return """
-- Step 3: drop untagged (NULL-source) rows — lazy refetch will repopulate
BEGIN;
SELECT COUNT(*) AS to_delete
FROM reference_embeddings
WHERE reference_source IS NULL;

DELETE FROM reference_embeddings
WHERE reference_source IS NULL;
COMMIT;
""".strip()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the R2 orphan keys (default: dry-run).",
    )
    parser.add_argument(
        "--purge-untagged",
        action="store_true",
        help="Also print the SQL for dropping NULL-source rows (5,618 rows).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("STEP 1 — Mapillary-tagged DB rows")
    print("=" * 60)
    print("Run this SQL against the buildings DB (gzzvhmmywaaxljpmoacm):\n")
    print(sql_block_mapillary())

    print()
    print("=" * 60)
    print("STEP 2 — R2 phantom orphans")
    print("=" * 60)
    print("Connecting to R2 + buildings DB to compute orphan set...")

    client = _r2_client()
    bucket = _r2_bucket()
    r2_keys = list_ondemand_keys(client, bucket)
    db_keys = await known_image_keys()
    orphans = sorted(set(r2_keys) - db_keys)

    print(f"  R2 ondemand/* keys: {len(r2_keys)}")
    print(f"  Known image_keys in DB: {len(db_keys)}")
    print(f"  Orphan keys (in R2 but no DB row): {len(orphans)}")

    if orphans:
        print("\n  First 20 orphans:")
        for k in orphans[:20]:
            print(f"    {k}")

    if args.apply and orphans:
        print(f"\n  --apply: deleting {len(orphans)} R2 keys...")
        # S3 batch delete is capped at 1,000 per request.
        deleted = 0
        for chunk_start in range(0, len(orphans), 1000):
            chunk = orphans[chunk_start : chunk_start + 1000]
            resp = client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
            deleted += len(chunk) - len(resp.get("Errors") or [])
            for err in resp.get("Errors") or []:
                print(f"    ! failed to delete {err.get('Key')}: {err.get('Message')}")
        print(f"  ✓ deleted {deleted} keys")
    elif orphans:
        print("\n  (Dry-run — pass --apply to actually delete.)")

    if args.purge_untagged:
        print()
        print("=" * 60)
        print("STEP 3 — NULL-source DB rows (5,618 unauditable)")
        print("=" * 60)
        print("Run this SQL against the buildings DB:\n")
        print(sql_block_untagged())


if __name__ == "__main__":
    asyncio.run(main())

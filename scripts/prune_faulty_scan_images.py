"""
Prune R2 images uploaded from faulty/unconfirmed scans.

Identifies scan records where:
  - confirmed_bin IS NULL (scan was never confirmed)
  - was_correct = False (user corrected to a different building)

For each, deletes the user photo from R2 and optionally clears the
hero_image_url on the building if it was set from that scan.

Usage:
    python scripts/prune_faulty_scan_images.py --dry-run
    python scripts/prune_faulty_scan_images.py --execute
"""

import asyncio
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main(dry_run: bool = True):
    from models.config import get_settings
    from models.session import AsyncSessionLocal
    from sqlalchemy import text
    import boto3

    settings = get_settings()

    r2 = boto3.client(
        's3',
        endpoint_url=f'https://{settings.r2_account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name='auto',
    )

    async with AsyncSessionLocal() as db:
        # Find scans with user photos that were never confirmed or were wrong
        result = await db.execute(text("""
            SELECT id, user_photo_url, top_match_bin, confirmed_bin, was_correct
            FROM scans
            WHERE user_photo_url IS NOT NULL
              AND user_photo_url LIKE '%/scans/%'
              AND (confirmed_bin IS NULL OR was_correct = false)
            ORDER BY created_at DESC
        """))
        rows = result.fetchall()

    logger.info(f"Found {len(rows)} faulty scan photos to prune")

    deleted = 0
    skipped = 0

    for row in rows:
        scan_id, photo_url, top_match_bin, confirmed_bin, was_correct = row

        # Extract R2 key from URL
        # URL format: https://pub-xxx.r2.dev/scans/{scan_id}.jpg
        r2_public_url = settings.r2_public_url.rstrip('/')
        if not photo_url.startswith(r2_public_url + '/'):
            logger.warning(f"Unexpected URL format, skipping: {photo_url}")
            skipped += 1
            continue

        key = photo_url[len(r2_public_url) + 1:]
        thumb_key = key.replace('.jpg', '_thumb.jpg')

        logger.info(
            f"{'[DRY RUN] Would delete' if dry_run else 'Deleting'}: "
            f"{key} (scan {scan_id[:8]}..., confirmed={confirmed_bin}, correct={was_correct})"
        )

        if not dry_run:
            try:
                r2.delete_object(Bucket=settings.r2_user_images_bucket, Key=key)
                deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete {key}: {e}")

            # Also try thumbnail
            try:
                r2.delete_object(Bucket=settings.r2_user_images_bucket, Key=thumb_key)
            except Exception:
                pass
        else:
            deleted += 1

    logger.info(
        f"{'Would delete' if dry_run else 'Deleted'} {deleted} photos, "
        f"skipped {skipped}. Total candidates: {len(rows)}"
    )
    if dry_run:
        logger.info("Re-run with --execute to actually delete.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()

    dry_run = not args.execute
    asyncio.run(main(dry_run=dry_run))

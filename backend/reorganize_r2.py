"""
Reorganize Cloudflare R2 storage with readable structure
Migrates from UUID-based paths to human-readable paths with metadata
"""

import asyncio
import logging
import json
from sqlalchemy import text
from typing import List, Dict

from models.session import get_db_context, init_db, close_db
from utils.storage import s3_client, upload_image, delete_image
from models.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


def slugify(text: str) -> str:
    """Convert text to URL-friendly slug"""
    import re
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text


async def get_buildings_with_images(session) -> List[Dict]:
    """Get all buildings that have cached reference images"""
    query = text("""
        SELECT
            id,
            des_addres as address,
            ST_Y(ST_Centroid(geom::geometry)) as latitude,
            ST_X(ST_Centroid(geom::geometry)) as longitude,
            style_prim,
            arch_build,
            hist_dist
        FROM buildings
        WHERE geom IS NOT NULL
        LIMIT 1000
    """)

    result = await session.execute(query)
    buildings = []

    for row in result:
        buildings.append({
            'id': str(row.id),
            'address': row.address or 'Unknown',
            'latitude': float(row.latitude),
            'longitude': float(row.longitude),
            'style': row.style_prim or 'Unknown',
            'architect': row.arch_build or 'Unknown',
            'district': row.hist_dist or None
        })

    return buildings


def list_r2_objects(prefix: str = ""):
    """List objects in R2 with given prefix"""
    try:
        response = s3_client.list_objects_v2(
            Bucket=settings.r2_bucket,
            Prefix=prefix
        )

        if 'Contents' not in response:
            return []

        return [obj['Key'] for obj in response['Contents']]
    except Exception as e:
        logger.error(f"Failed to list R2 objects: {e}")
        return []


async def copy_r2_object(source_key: str, dest_key: str):
    """Copy object within R2"""
    try:
        # Copy object
        s3_client.copy_object(
            Bucket=settings.r2_bucket,
            CopySource={'Bucket': settings.r2_bucket, 'Key': source_key},
            Key=dest_key
        )
        logger.info(f"Copied {source_key} → {dest_key}")
        return True
    except Exception as e:
        logger.error(f"Failed to copy {source_key}: {e}")
        return False


async def create_metadata_file(building: Dict, new_path: str):
    """Create metadata.json for building"""
    metadata = {
        'id': building['id'],
        'address': building['address'],
        'location': {
            'latitude': building['latitude'],
            'longitude': building['longitude']
        },
        'style': building['style'],
        'architect': building['architect'],
        'district': building['district'],
        'images': {
            f"{angle}deg": f"{new_path}/{angle}deg.jpg"
            for angle in [0, 45, 90, 135, 180, 225, 270, 315]
        }
    }

    metadata_json = json.dumps(metadata, indent=2)
    metadata_key = f"{new_path}/metadata.json"

    try:
        s3_client.put_object(
            Bucket=settings.r2_bucket,
            Key=metadata_key,
            Body=metadata_json.encode('utf-8'),
            ContentType='application/json',
            ACL='public-read'
        )
        logger.info(f"Created metadata: {metadata_key}")
        return True
    except Exception as e:
        logger.error(f"Failed to create metadata: {e}")
        return False


async def reorganize_building(building: Dict, dry_run: bool = True):
    """Reorganize images for a single building"""
    building_id = building['id']
    address = building['address']

    # Create readable path
    address_slug = slugify(address.split(',')[0])  # First part of address
    new_path = f"reference/by-address/{address_slug}-{building_id[:8]}"

    logger.info(f"\n{'='*60}")
    logger.info(f"Building: {address}")
    logger.info(f"Old path: reference/{building_id}/")
    logger.info(f"New path: {new_path}/")

    # List existing images
    old_prefix = f"reference/{building_id}/"
    existing_images = list_r2_objects(old_prefix)

    if not existing_images:
        logger.warning(f"No images found for {building_id}")
        return False

    logger.info(f"Found {len(existing_images)} images")

    if dry_run:
        logger.info("[DRY RUN] Would reorganize:")
        for old_key in existing_images:
            # Convert heading_X.jpg to Xdeg.jpg
            if 'heading_' in old_key:
                heading = old_key.split('heading_')[1].replace('.jpg', '')
                new_key = f"{new_path}/{heading}deg.jpg"
                logger.info(f"  {old_key} → {new_key}")
        logger.info(f"  Would create: {new_path}/metadata.json")
        return True

    # Actually reorganize
    success_count = 0
    for old_key in existing_images:
        if 'heading_' in old_key:
            heading = old_key.split('heading_')[1].replace('.jpg', '')
            new_key = f"{new_path}/{heading}deg.jpg"

            if await copy_r2_object(old_key, new_key):
                success_count += 1

        # Also copy thumbnail if exists
        if '_thumb' in old_key:
            heading = old_key.split('heading_')[1].replace('_thumb.jpg', '')
            new_key = f"{new_path}/{heading}deg_thumb.jpg"
            await copy_r2_object(old_key, new_key)

    # Create metadata
    if success_count > 0:
        await create_metadata_file(building, new_path)

    logger.info(f"✅ Reorganized {success_count}/{len(existing_images)} images")
    return success_count > 0


async def main():
    import argparse

    parser = argparse.ArgumentParser(description='Reorganize R2 storage with readable paths')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--limit', type=int, default=10, help='Number of buildings to process (default: 10)')
    parser.add_argument('--delete-old', action='store_true', help='Delete old UUID-based paths after copying')

    args = parser.parse_args()

    logger.info("Starting R2 reorganization...")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    logger.info(f"Limit: {args.limit} buildings")

    await init_db()

    try:
        async with get_db_context() as session:
            # Get buildings
            buildings = await get_buildings_with_images(session)
            logger.info(f"Found {len(buildings)} buildings in database")

            # Check which ones have images in R2
            buildings_with_images = []
            for building in buildings[:args.limit * 2]:  # Check 2x limit to account for some without images
                old_prefix = f"reference/{building['id']}/"
                if list_r2_objects(old_prefix):
                    buildings_with_images.append(building)
                    if len(buildings_with_images) >= args.limit:
                        break

            logger.info(f"Found {len(buildings_with_images)} buildings with images in R2")

            # Reorganize each building
            success = 0
            for i, building in enumerate(buildings_with_images, 1):
                logger.info(f"\n[{i}/{len(buildings_with_images)}]")

                if await reorganize_building(building, dry_run=args.dry_run):
                    success += 1

                # Small delay to avoid rate limits
                if not args.dry_run:
                    await asyncio.sleep(0.5)

            logger.info(f"\n{'='*60}")
            logger.info(f"SUMMARY")
            logger.info(f"{'='*60}")
            logger.info(f"Successfully reorganized: {success}/{len(buildings_with_images)} buildings")

            if args.dry_run:
                logger.info("\nℹ️  This was a dry run. Run without --dry-run to apply changes.")
            elif args.delete_old:
                logger.info("\n⚠️  Deleting old UUID-based paths...")
                for building in buildings_with_images:
                    old_prefix = f"reference/{building['id']}/"
                    old_images = list_r2_objects(old_prefix)
                    for old_key in old_images:
                        await delete_image(old_key)
                        logger.info(f"Deleted: {old_key}")

    finally:
        await close_db()


if __name__ == '__main__':
    asyncio.run(main())

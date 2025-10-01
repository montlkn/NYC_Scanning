"""
Reorganize existing R2 images from UUID paths to readable paths
"""

import asyncio
import logging
import json
from sqlalchemy import text

from models.session import get_db_context, init_db, close_db
from utils.storage import s3_client
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
    return text[:50]  # Limit length


async def get_building_by_id(session, building_id: str):
    """Get building info by UUID"""
    query = text("""
        SELECT
            id,
            des_addres as address,
            ST_Y(ST_Centroid(geom::geometry)) as latitude,
            ST_X(ST_Centroid(geom::geometry)) as longitude,
            style_prim,
            arch_build
        FROM buildings
        WHERE id::text = :building_id
    """)

    result = await session.execute(query, {"building_id": building_id})
    row = result.first()

    if not row:
        return None

    return {
        'id': str(row.id),
        'address': row.address or 'Unknown',
        'latitude': float(row.latitude),
        'longitude': float(row.longitude),
        'style': row.style_prim or 'Unknown',
        'architect': row.arch_build or 'Unknown'
    }


def list_r2_folders():
    """List top-level folders in reference/"""
    try:
        response = s3_client.list_objects_v2(
            Bucket=settings.r2_bucket,
            Prefix='reference/',
            Delimiter='/'
        )

        folders = []
        if 'CommonPrefixes' in response:
            for prefix in response['CommonPrefixes']:
                folder = prefix['Prefix'].replace('reference/', '').rstrip('/')
                if folder:  # Not empty
                    folders.append(folder)

        return folders
    except Exception as e:
        logger.error(f"Failed to list folders: {e}")
        return []


def list_images_in_folder(folder: str):
    """List all images in a folder"""
    try:
        prefix = f"reference/{folder}/"
        response = s3_client.list_objects_v2(
            Bucket=settings.r2_bucket,
            Prefix=prefix
        )

        images = []
        if 'Contents' in response:
            for obj in response['Contents']:
                images.append(obj['Key'])

        return images
    except Exception as e:
        logger.error(f"Failed to list images in {folder}: {e}")
        return []


def copy_object(source_key: str, dest_key: str):
    """Copy object within R2"""
    try:
        s3_client.copy_object(
            Bucket=settings.r2_bucket,
            CopySource={'Bucket': settings.r2_bucket, 'Key': source_key},
            Key=dest_key
        )
        logger.info(f"  ✅ Copied: {source_key.split('/')[-1]} → {dest_key}")
        return True
    except Exception as e:
        logger.error(f"  ❌ Failed to copy {source_key}: {e}")
        return False


def create_metadata(building: dict, new_path: str, images: list):
    """Create metadata.json"""
    metadata = {
        'id': building['id'],
        'address': building['address'],
        'location': {
            'latitude': building['latitude'],
            'longitude': building['longitude']
        },
        'style': building['style'],
        'architect': building['architect'],
        'images': {}
    }

    # Map images
    for img in images:
        if 'heading_' in img and '.jpg' in img and '_thumb' not in img:
            heading = img.split('heading_')[1].replace('.jpg', '')
            metadata['images'][f'{heading}deg'] = f"{new_path}/{heading}deg.jpg"

    metadata_json = json.dumps(metadata, indent=2)

    try:
        s3_client.put_object(
            Bucket=settings.r2_bucket,
            Key=f"{new_path}/metadata.json",
            Body=metadata_json.encode('utf-8'),
            ContentType='application/json',
            ACL='public-read'
        )
        logger.info(f"  ✅ Created metadata.json")
        return True
    except Exception as e:
        logger.error(f"  ❌ Failed to create metadata: {e}")
        return False


async def reorganize_folder(session, uuid_folder: str, dry_run: bool = True):
    """Reorganize a single UUID folder"""
    # Get building info
    building = await get_building_by_id(session, uuid_folder)

    if not building:
        logger.warning(f"❌ Building {uuid_folder} not found in database")
        return False

    # Create readable path
    address_part = building['address'].split(',')[0].strip()
    slug = slugify(address_part)
    new_path = f"reference/buildings/{slug}"

    logger.info(f"\n{'='*60}")
    logger.info(f"📍 {building['address']}")
    logger.info(f"   Style: {building['style']}")
    logger.info(f"   Old: reference/{uuid_folder}/")
    logger.info(f"   New: {new_path}/")

    # List images
    images = list_images_in_folder(uuid_folder)
    logger.info(f"   Images: {len(images)}")

    if dry_run:
        logger.info(f"   [DRY RUN] Would reorganize {len(images)} files")
        return True

    # Copy images
    success_count = 0
    for old_key in images:
        filename = old_key.split('/')[-1]

        # Rename heading_X.jpg to Xdeg.jpg
        if 'heading_' in filename:
            if '_thumb' in filename:
                heading = filename.split('heading_')[1].replace('_thumb.jpg', '')
                new_filename = f"{heading}deg_thumb.jpg"
            else:
                heading = filename.split('heading_')[1].replace('.jpg', '')
                new_filename = f"{heading}deg.jpg"

            new_key = f"{new_path}/{new_filename}"
            if copy_object(old_key, new_key):
                success_count += 1

    # Create metadata
    if success_count > 0:
        create_metadata(building, new_path, images)

    logger.info(f"✅ Reorganized {success_count}/{len(images)} files")
    return success_count > 0


async def main():
    import argparse

    parser = argparse.ArgumentParser(description='Reorganize existing R2 storage')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without applying')
    parser.add_argument('--delete-old', action='store_true', help='Delete old UUID folders after copying')

    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "LIVE"
    logger.info(f"🚀 R2 Reorganization - {mode} MODE")

    # List UUID folders
    uuid_folders = list_r2_folders()
    logger.info(f"📂 Found {len(uuid_folders)} UUID folders in R2")

    await init_db()

    try:
        async with get_db_context() as session:
            success = 0
            for i, uuid_folder in enumerate(uuid_folders, 1):
                logger.info(f"\n[{i}/{len(uuid_folders)}]")
                if await reorganize_folder(session, uuid_folder, dry_run=args.dry_run):
                    success += 1

            logger.info(f"\n{'='*60}")
            logger.info(f"✅ SUMMARY: {success}/{len(uuid_folders)} folders reorganized")

            if args.dry_run:
                logger.info("\nℹ️  DRY RUN - No changes made. Run without --dry-run to apply.")
            elif args.delete_old:
                logger.info("\n🗑️  Deleting old UUID folders...")
                for uuid_folder in uuid_folders:
                    images = list_images_in_folder(uuid_folder)
                    for img in images:
                        s3_client.delete_object(Bucket=settings.r2_bucket, Key=img)
                    logger.info(f"Deleted: reference/{uuid_folder}/")

    finally:
        await close_db()


if __name__ == '__main__':
    asyncio.run(main())

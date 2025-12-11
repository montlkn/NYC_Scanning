#!/usr/bin/env python3
"""
Cron job to re-embed user-submitted images.

This script can be run daily to:
1. Re-generate CLIP embeddings for user images (if model updated)
2. Verify image quality and update scores
3. Process any images that failed initial embedding
4. Clean up orphaned references

Schedule with cron or Modal scheduled function:
    0 2 * * * python scripts/reembed_user_images.py

Or deploy as Modal scheduled function for automatic execution.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import httpx

# Import from parent package
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.clip_matcher import encode_photo, get_model
from models.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


async def fetch_image_bytes(url: str) -> bytes:
    """Fetch image bytes from URL"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def reembed_user_images(
    db: AsyncSession,
    days_old: int = None,
    force_all: bool = False,
    batch_size: int = 100
):
    """
    Re-embed user-submitted images with current CLIP model.

    Args:
        db: Database session
        days_old: Only process images older than N days (None = all)
        force_all: Re-embed even if already has embedding
        batch_size: Number of images to process per batch
    """
    logger.info("Starting user image re-embedding job...")

    # Ensure model is loaded
    get_model()

    # Build query to find user images
    # Note: reference_embeddings uses image_key pattern to identify user images
    # User images have image_key pattern: {BIN}/user_images/{user_id}/{scan_id}.jpg
    query_parts = ["image_key LIKE '%/user_images/%'"]
    params = {}

    if not force_all:
        # Only images without embeddings
        query_parts.append("embedding IS NULL")

    if days_old:
        cutoff_date = datetime.utcnow() - timedelta(days=days_old)
        query_parts.append("created_at < :cutoff_date")
        params['cutoff_date'] = cutoff_date

    where_clause = " AND ".join(query_parts)

    # Get total count
    count_query = text(f"SELECT COUNT(*) FROM reference_embeddings WHERE {where_clause}")
    result = await db.execute(count_query, params)
    total_count = result.scalar()

    logger.info(f"Found {total_count} user images to process")

    if total_count == 0:
        logger.info("No images to process")
        return {
            'processed': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0
        }

    # Process in batches
    processed = 0
    success = 0
    failed = 0
    skipped = 0

    offset = 0
    while offset < total_count:
        # Fetch batch
        batch_query = text(f"""
            SELECT id, building_id, image_key, embedding
            FROM reference_embeddings
            WHERE {where_clause}
            ORDER BY created_at ASC
            LIMIT :limit OFFSET :offset
        """)

        batch_params = {**params, 'limit': batch_size, 'offset': offset}
        result = await db.execute(batch_query, batch_params)
        images = result.fetchall()

        if not images:
            break

        logger.info(f"Processing batch {offset // batch_size + 1}: {len(images)} images")

        for img in images:
            img_id, building_id, image_key, existing_embedding = img

            try:
                # Skip if already has embedding (unless force_all)
                if existing_embedding and not force_all:
                    skipped += 1
                    continue

                # Construct full URL from image_key
                image_url = f"{settings.r2_public_url}/{image_key}"

                # Fetch image
                logger.debug(f"Fetching image {img_id} from {image_url}")
                image_bytes = await fetch_image_bytes(image_url)

                # Generate embedding
                logger.debug(f"Generating embedding for image {img_id}")
                embedding = await encode_photo(image_bytes)
                embedding_list = embedding.tolist()

                # Update database
                update_query = text("""
                    UPDATE reference_embeddings
                    SET embedding = :embedding
                    WHERE id = :id
                """)

                await db.execute(update_query, {
                    'id': img_id,
                    'embedding': embedding_list,
                })

                success += 1
                processed += 1

                if processed % 10 == 0:
                    logger.info(f"Processed {processed}/{total_count} images...")
                    await db.commit()  # Commit every 10 images

            except Exception as e:
                logger.error(f"Failed to process image {img_id}: {e}")
                failed += 1
                processed += 1

        offset += batch_size
        await db.commit()  # Commit after each batch

    # Final commit
    await db.commit()

    result = {
        'processed': processed,
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'total_user_images': total_count
    }

    logger.info(f"Re-embedding job completed: {result}")
    return result


async def verify_user_image_quality(db: AsyncSession):
    """
    Verify quality of user-submitted images and update scores.

    Checks:
    - Image is accessible
    - Embedding exists and has correct dimensions
    """
    logger.info("Verifying user image quality...")

    query = text("""
        SELECT id, image_key, embedding
        FROM reference_embeddings
        WHERE image_key LIKE '%/user_images/%'
        LIMIT 100
    """)

    result = await db.execute(query)
    images = result.fetchall()

    verified = 0
    issues = 0

    for img in images:
        img_id, image_key, embedding = img

        try:
            # Construct full URL
            image_url = f"{settings.r2_public_url}/{image_key}"

            # Check image is accessible
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.head(image_url)
                response.raise_for_status()

            # Check embedding exists and has correct shape
            if embedding is None:
                logger.warning(f"Image {img_id} has no embedding")
                issues += 1
                continue

            # Parse embedding if it's a string
            if isinstance(embedding, str):
                import json
                emb_list = json.loads(embedding)
                if len(emb_list) != 512:
                    logger.warning(f"Image {img_id} has invalid embedding size: {len(emb_list)}")
                    issues += 1
                    continue

            verified += 1

        except Exception as e:
            logger.warning(f"Image {img_id} verification failed: {e}")
            issues += 1

    logger.info(f"Verified {verified} images, found {issues} issues")
    return {'verified': verified, 'issues': issues}


async def cleanup_orphaned_references(db: AsyncSession):
    """
    Clean up reference images that point to non-existent buildings.
    """
    logger.info("Cleaning up orphaned references...")

    # Find references with invalid building_ids
    query = text("""
        SELECT COUNT(*) FROM reference_embeddings r
        LEFT JOIN buildings_full_merge_scanning b ON r.building_id = b.id
        WHERE b.id IS NULL AND r.image_key LIKE '%/user_images/%'
    """)

    result = await db.execute(query)
    orphan_count = result.scalar()

    if orphan_count > 0:
        logger.warning(f"Found {orphan_count} orphaned user references")
        # Don't delete automatically - just report
    else:
        logger.info("No orphaned references found")

    return {'orphaned_count': orphan_count}


async def get_user_image_stats(db: AsyncSession):
    """Get statistics about user-submitted images"""

    stats_query = text("""
        SELECT
            COUNT(*) as total_user_images,
            COUNT(CASE WHEN embedding IS NOT NULL THEN 1 END) as with_embedding,
            COUNT(DISTINCT building_id) as unique_buildings,
            MIN(created_at) as oldest,
            MAX(created_at) as newest
        FROM reference_embeddings
        WHERE image_key LIKE '%/user_images/%'
    """)

    result = await db.execute(stats_query)
    row = result.fetchone()

    stats = {
        'total_user_images': row[0],
        'with_embedding': row[1],
        'unique_buildings': row[2],
        'oldest': str(row[3]) if row[3] else None,
        'newest': str(row[4]) if row[4] else None
    }

    logger.info(f"User image stats: {stats}")
    return stats


async def main():
    """Main entry point for cron job"""
    logger.info("=" * 50)
    logger.info("Starting daily user image processing job")
    logger.info(f"Time: {datetime.utcnow().isoformat()}")
    logger.info("=" * 50)

    # Create database session
    # Convert postgresql:// to postgresql+asyncpg:// for async connection
    database_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        try:
            # Get current stats
            stats = await get_user_image_stats(db)

            # Re-embed images without embeddings
            reembed_result = await reembed_user_images(db, force_all=False)

            # Verify image quality
            verify_result = await verify_user_image_quality(db)

            # Check for orphans
            cleanup_result = await cleanup_orphaned_references(db)

            # Final report
            logger.info("=" * 50)
            logger.info("Job completed successfully!")
            logger.info(f"Stats: {stats}")
            logger.info(f"Re-embedding: {reembed_result}")
            logger.info(f"Verification: {verify_result}")
            logger.info(f"Cleanup: {cleanup_result}")
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"Job failed: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())

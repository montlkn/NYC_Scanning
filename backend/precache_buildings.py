"""
Pre-cache Street View reference images for NYC buildings
Fetches images from multiple headings and uploads to R2 storage
"""

import asyncio
import logging
from sqlalchemy import text
from typing import List, Dict
import time

from models.session import get_db_context, init_db, close_db
from services.reference_images import fetch_street_view
from utils.storage import upload_image
from models.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Headings to capture for each building (degrees)
HEADINGS = [0, 45, 90, 135, 180, 225, 270, 315]


async def get_buildings_to_cache(
    session,
    limit: int = 100,
    offset: int = 0
) -> List[Dict]:
    """
    Get buildings that need reference images cached

    Args:
        session: Database session
        limit: Max number of buildings to return
        offset: Offset for pagination

    Returns:
        List of building dicts with id, address, lat, lon
    """
    # Query buildings with geometry data
    # Use ST_Centroid to get center point of building footprint
    query = text("""
        SELECT
            id,
            des_addres as address,
            ST_Y(ST_Centroid(geom::geometry)) as latitude,
            ST_X(ST_Centroid(geom::geometry)) as longitude,
            style_prim,
            arch_build
        FROM buildings
        WHERE geom IS NOT NULL
        ORDER BY random()
        LIMIT :limit
        OFFSET :offset
    """)

    result = await session.execute(query, {"limit": limit, "offset": offset})
    buildings = []

    for row in result:
        buildings.append({
            'id': str(row.id),
            'address': row.address or 'Unknown',
            'latitude': float(row.latitude),
            'longitude': float(row.longitude),
            'style': row.style_prim or 'Unknown',
            'architect': row.arch_build or 'Unknown'
        })

    return buildings


async def cache_building_images(
    session,
    building: Dict,
    headings: List[int] = HEADINGS
) -> int:
    """
    Cache Street View images for a building from multiple headings

    Args:
        session: Database session
        building: Building dict with id, address, lat, lon
        headings: List of heading angles to capture

    Returns:
        Number of images successfully cached
    """
    cached_count = 0

    logger.info(f"Caching images for building {building['id'][:8]}...")
    logger.info(f"  Address: {building['address']}")
    logger.info(f"  Location: ({building['latitude']:.6f}, {building['longitude']:.6f})")
    logger.info(f"  Style: {building['style']}")

    for heading in headings:
        try:
            # Fetch Street View image
            image_bytes = await fetch_street_view(
                lat=building['latitude'],
                lng=building['longitude'],
                bearing=heading,
                pitch=10  # Slight upward angle for buildings
            )

            if not image_bytes:
                logger.warning(f"  ⚠️  No Street View available for heading {heading}°")
                continue

            # Generate storage key
            key = f"reference/{building['id']}/heading_{heading}.jpg"

            # Upload to R2
            url = await upload_image(
                image_bytes=image_bytes,
                key=key,
                content_type='image/jpeg',
                make_public=True,
                create_thumbnail=True
            )

            logger.info(f"  ✅ Cached heading {heading}°: {len(image_bytes):,} bytes")
            cached_count += 1

            # Small delay to respect API rate limits
            await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"  ❌ Failed to cache heading {heading}°: {e}")
            continue

    if cached_count > 0:
        logger.info(f"✅ Successfully cached {cached_count}/{len(headings)} images for building {building['id'][:8]}")

    return cached_count


async def precache_batch(
    limit: int = 10,
    offset: int = 0,
    delay: float = 1.0
) -> dict:
    """
    Pre-cache a batch of buildings

    Args:
        limit: Number of buildings to process
        offset: Starting offset
        delay: Delay between buildings (seconds)

    Returns:
        Statistics dict with counts
    """
    await init_db()

    stats = {
        'buildings_processed': 0,
        'buildings_success': 0,
        'buildings_failed': 0,
        'total_images': 0,
        'start_time': time.time()
    }

    try:
        async with get_db_context() as session:
            # Get buildings to cache
            buildings = await get_buildings_to_cache(
                session=session,
                limit=limit,
                offset=offset
            )

            logger.info(f"\n{'='*60}")
            logger.info(f"STARTING BATCH: {len(buildings)} buildings")
            logger.info(f"{'='*60}\n")

            if not buildings:
                logger.warning("No buildings found to cache")
                return stats

            # Process each building
            for i, building in enumerate(buildings, 1):
                logger.info(f"\n[{i}/{len(buildings)}] Processing building...")

                try:
                    cached_count = await cache_building_images(session, building)
                    stats['buildings_processed'] += 1
                    stats['total_images'] += cached_count

                    if cached_count > 0:
                        stats['buildings_success'] += 1
                    else:
                        stats['buildings_failed'] += 1

                    # Delay between buildings
                    if i < len(buildings):
                        await asyncio.sleep(delay)

                except Exception as e:
                    logger.error(f"Failed to process building: {e}")
                    stats['buildings_failed'] += 1
                    stats['buildings_processed'] += 1

            # Print summary
            elapsed = time.time() - stats['start_time']
            logger.info(f"\n{'='*60}")
            logger.info(f"BATCH COMPLETE")
            logger.info(f"{'='*60}")
            logger.info(f"Buildings processed: {stats['buildings_processed']}")
            logger.info(f"Buildings success: {stats['buildings_success']}")
            logger.info(f"Buildings failed: {stats['buildings_failed']}")
            logger.info(f"Total images cached: {stats['total_images']}")
            logger.info(f"Time elapsed: {elapsed:.1f}s")
            if stats['buildings_processed'] > 0:
                logger.info(f"Avg time per building: {elapsed/stats['buildings_processed']:.1f}s")
            logger.info(f"{'='*60}\n")

    finally:
        await close_db()

    return stats


async def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Pre-cache Street View images for NYC buildings')
    parser.add_argument('--limit', type=int, default=10, help='Number of buildings to cache (default: 10)')
    parser.add_argument('--offset', type=int, default=0, help='Starting offset (default: 0)')
    parser.add_argument('--delay', type=float, default=1.0, help='Delay between buildings in seconds (default: 1.0)')

    args = parser.parse_args()

    logger.info("Starting pre-cache process...")
    logger.info(f"Settings: limit={args.limit}, offset={args.offset}, delay={args.delay}s")

    stats = await precache_batch(
        limit=args.limit,
        offset=args.offset,
        delay=args.delay
    )

    return stats


if __name__ == '__main__':
    asyncio.run(main())

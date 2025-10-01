"""
Reference image fetching and caching service
Handles Street View, Mapillary, and user uploads
"""

import httpx
import asyncio
from typing import Optional, Dict, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging

from models.database import ReferenceImage, Building
from models.config import get_settings
from utils.storage import upload_image

logger = logging.getLogger(__name__)
settings = get_settings()


async def fetch_street_view(
    lat: float,
    lng: float,
    bearing: float,
    size: Optional[str] = None,
    pitch: int = 10,
    fov: int = 60
) -> Optional[bytes]:
    """
    Fetch image from Google Street View Static API

    Args:
        lat: Latitude
        lng: Longitude
        bearing: Camera heading (0-360)
        size: Image size (default from settings)
        pitch: Camera pitch
        fov: Field of view

    Returns:
        Image bytes or None if failed
    """
    if size is None:
        size = settings.street_view_size

    url = (
        f"https://maps.googleapis.com/maps/api/streetview?"
        f"size={size}&"
        f"location={lat},{lng}&"
        f"heading={bearing}&"
        f"pitch={pitch}&"
        f"fov={fov}&"
        f"key={settings.google_maps_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)

            if response.status_code == 200:
                # Check if it's an actual image or "no imagery" placeholder
                content_length = len(response.content)
                if content_length > 5000:  # Placeholder images are typically smaller
                    logger.info(f"Fetched Street View image: {lat},{lng} @ {bearing}°")
                    return response.content
                else:
                    logger.warning(f"Street View returned placeholder (no imagery): {lat},{lng}")
                    return None
            else:
                logger.error(f"Street View API error: {response.status_code}")
                return None

    except Exception as e:
        logger.error(f"Failed to fetch Street View image: {e}")
        return None


async def get_or_fetch_reference_image(
    session: AsyncSession,
    bbl: str,
    lat: float,
    lng: float,
    user_bearing: float
) -> Optional[str]:
    """
    Get cached reference image or fetch new one from Street View

    Args:
        session: Database session
        bbl: Building BBL
        lat: Building latitude
        lng: Building longitude
        user_bearing: User's compass bearing

    Returns:
        Image URL or None if failed
    """
    # Check cache first (within bearing tolerance)
    tolerance = settings.reference_image_bearing_tolerance

    query = (
        select(ReferenceImage)
        .where(ReferenceImage.bbl == bbl)
        .where(ReferenceImage.compass_bearing.between(
            user_bearing - tolerance,
            user_bearing + tolerance
        ))
        .order_by(ReferenceImage.quality_score.desc())
        .limit(1)
    )

    result = await session.execute(query)
    cached_image = result.scalar_one_or_none()

    if cached_image:
        logger.info(f"Cache hit for BBL {bbl} @ bearing {user_bearing}°")
        return cached_image.image_url

    # Cache miss - fetch from Street View
    logger.info(f"Cache miss for BBL {bbl} @ bearing {user_bearing}° - fetching...")

    # Calculate facade bearing (opposite of user bearing for best view)
    facade_bearing = (user_bearing + 180) % 360

    # Fetch from Street View
    image_bytes = await fetch_street_view(lat, lng, facade_bearing)

    if not image_bytes:
        logger.warning(f"Failed to fetch Street View for BBL {bbl}")
        return None

    # Upload to R2
    key = f"reference/{bbl}/{int(facade_bearing)}.jpg"
    try:
        image_url = await upload_image(
            image_bytes,
            key,
            create_thumbnail=True
        )

        # Store in database
        ref_image = ReferenceImage(
            bbl=bbl,
            image_url=image_url,
            thumbnail_url=f"{settings.r2_public_url}/reference/{bbl}/{int(facade_bearing)}_thumb.jpg",
            source='street_view',
            compass_bearing=facade_bearing,
            capture_lat=lat,
            capture_lng=lng,
            distance_from_building=0.0,  # Approximate
            quality_score=1.0,
            created_at=datetime.utcnow()
        )

        session.add(ref_image)
        await session.commit()

        logger.info(f"Stored reference image for BBL {bbl}")
        return image_url

    except Exception as e:
        logger.error(f"Failed to upload/store reference image: {e}")
        return None


async def get_reference_images_for_candidates(
    session: AsyncSession,
    candidates: List[Dict],
    user_bearing: float
) -> Dict[str, str]:
    """
    Fetch reference images for all candidates in parallel

    Args:
        session: Database session
        candidates: List of candidate building dicts
        user_bearing: User's compass bearing

    Returns:
        Dictionary mapping BBL to image URL
    """
    logger.info(f"Fetching reference images for {len(candidates)} candidates")

    # Create tasks for parallel fetching
    tasks = []
    for candidate in candidates:
        task = get_or_fetch_reference_image(
            session,
            candidate['bbl'],
            candidate['latitude'],
            candidate['longitude'],
            user_bearing
        )
        tasks.append((candidate['bbl'], task))

    # Execute in parallel with semaphore to limit concurrency
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent fetches

    async def fetch_with_limit(bbl: str, task):
        async with semaphore:
            return bbl, await task

    results = await asyncio.gather(*[
        fetch_with_limit(bbl, task) for bbl, task in tasks
    ], return_exceptions=True)

    # Build result dict
    reference_images = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Error fetching reference image: {result}")
            continue

        bbl, url = result
        if url:
            reference_images[bbl] = url

    logger.info(f"Successfully fetched {len(reference_images)} reference images")
    return reference_images


async def get_all_reference_images_for_building(
    session: AsyncSession,
    bbl: str
) -> List[Dict]:
    """
    Get all cached reference images for a building

    Args:
        session: Database session
        bbl: Building BBL

    Returns:
        List of reference image dicts
    """
    query = (
        select(ReferenceImage)
        .where(ReferenceImage.bbl == bbl)
        .order_by(ReferenceImage.compass_bearing)
    )

    result = await session.execute(query)
    images = result.scalars().all()

    return [
        {
            'id': img.id,
            'image_url': img.image_url,
            'thumbnail_url': img.thumbnail_url,
            'source': img.source,
            'compass_bearing': img.compass_bearing,
            'quality_score': img.quality_score,
            'created_at': img.created_at.isoformat() if img.created_at else None
        }
        for img in images
    ]


async def check_street_view_availability(lat: float, lng: float) -> bool:
    """
    Check if Street View imagery is available at location

    Args:
        lat: Latitude
        lng: Longitude

    Returns:
        True if available, False otherwise
    """
    url = (
        f"https://maps.googleapis.com/maps/api/streetview/metadata?"
        f"location={lat},{lng}&"
        f"key={settings.google_maps_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            data = response.json()
            return data.get('status') == 'OK'

    except Exception as e:
        logger.error(f"Failed to check Street View availability: {e}")
        return False


async def precache_building_images(
    session: AsyncSession,
    bbl: str,
    lat: float,
    lng: float,
    bearings: Optional[List[int]] = None
) -> int:
    """
    Pre-cache reference images for a building at multiple bearings

    Args:
        session: Database session
        bbl: Building BBL
        lat: Building latitude
        lng: Building longitude
        bearings: List of bearings to cache (default: cardinal directions)

    Returns:
        Number of images successfully cached
    """
    if bearings is None:
        bearings = settings.precache_cardinal_directions

    logger.info(f"Pre-caching {len(bearings)} images for BBL {bbl}")

    cached_count = 0
    for bearing in bearings:
        image_url = await get_or_fetch_reference_image(
            session, bbl, lat, lng, bearing
        )
        if image_url:
            cached_count += 1
        await asyncio.sleep(0.1)  # Small delay to avoid rate limiting

    logger.info(f"Cached {cached_count}/{len(bearings)} images for BBL {bbl}")
    return cached_count

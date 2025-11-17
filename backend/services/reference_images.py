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
    bin: str,
    lat: float,
    lng: float,
    user_bearing: float
) -> Optional[str]:
    """
    Get cached reference image or fetch new one from Street View

    Args:
        session: Database session
        bin: Building Identification Number (now primary key)
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
        .where(ReferenceImage.bin == bin)  # Use BIN (primary key) instead of BBL
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
        logger.info(f"✅ Cache hit for BIN {bin} @ bearing {user_bearing}°")
        return cached_image.image_url

    # Cache miss - fetch from Street View
    logger.info(f"Cache miss for BIN {bin} @ bearing {user_bearing}° - fetching...")

    # Calculate facade bearing (opposite of user bearing for best view)
    facade_bearing = (user_bearing + 180) % 360

    # Fetch from Street View
    image_bytes = await fetch_street_view(lat, lng, facade_bearing)

    if not image_bytes:
        logger.warning(f"Failed to fetch Street View for BIN {bin}")
        return None

    # Upload to R2 - now using BIN instead of BBL for path
    key = f"reference/{bin}/{int(facade_bearing)}.jpg"
    try:
        image_url = await upload_image(
            image_bytes,
            key,
            create_thumbnail=True
        )

        # Store in database
        ref_image = ReferenceImage(
            bin=bin,  # Use BIN (primary key) instead of BBL
            image_url=image_url,
            thumbnail_url=f"{settings.r2_public_url}/reference/{bin}/{int(facade_bearing)}_thumb.jpg",
            source='street_view',
            compass_bearing=facade_bearing,
            capture_lat=lat,
            capture_lng=lng,
            distance_from_building=0.0,
            quality_score=1.0,
            created_at=datetime.utcnow()
        )

        session.add(ref_image)
        await session.commit()

        logger.info(f"✅ Stored reference image for BIN {bin}")
        return image_url

    except Exception as e:
        logger.error(f"Failed to upload/store reference image: {e}")
        return None


async def get_reference_images_for_candidates(
    session: AsyncSession,
    candidates: List[Dict],
    user_bearing: float
) -> Dict[str, List[Dict]]:
    """
    Get reference images with embeddings for all candidates

    Queries reference_embeddings table which stores pregenerated images and CLIP embeddings

    Args:
        session: Database session
        candidates: List of candidate building dicts (with 'bin' field)
        user_bearing: User's compass bearing

    Returns:
        Dictionary mapping BIN to list of reference image dicts with embeddings
    """
    logger.info(f"📥 Fetching reference embeddings for {len(candidates)} candidates")

    # Get all unique BINs from candidates
    bins = list(set(candidate['bin'] for candidate in candidates))

    # Query reference_embeddings table
    # Note: reference_embeddings uses building_id (integer) as foreign key
    # Need to join with buildings table to match BIN
    from sqlalchemy import text

    query = text("""
        SELECT
            b.bin,
            re.angle,
            re.pitch,
            re.image_key,
            re.embedding
        FROM reference_embeddings re
        JOIN buildings_full_merge_scanning b ON b.id = re.building_id
        WHERE REPLACE(b.bin, '.0', '') = ANY(:bins)
    """)

    result = await session.execute(query, {"bins": bins})
    rows = result.fetchall()

    # Group by BIN
    reference_data = {}
    for row in rows:
        bin_val = str(row[0]).replace('.0', '')
        if bin_val not in reference_data:
            reference_data[bin_val] = []

        # Construct R2 URL from image_key
        image_url = f"{settings.r2_public_url}/{row[3]}"

        # Parse embedding from string to list
        # PostgreSQL vector type returns as string like "[0.1, 0.2, ...]"
        import json
        embedding_str = row[4]
        if isinstance(embedding_str, str):
            # Remove brackets and split by comma
            embedding = json.loads(embedding_str)
        else:
            embedding = embedding_str

        reference_data[bin_val].append({
            'angle': row[1],
            'pitch': row[2],
            'image_key': row[3],
            'image_url': image_url,
            'embedding': embedding  # List of floats
        })

    logger.info(f"✅ Found reference images for {len(reference_data)}/{len(bins)} buildings")
    for bin_val, images in reference_data.items():
        logger.info(f"  BIN {bin_val}: {len(images)} reference images")

    return reference_data


async def get_all_reference_images_for_building(
    session: AsyncSession,
    bin: str
) -> List[Dict]:
    """
    Get all cached reference images for a building

    Args:
        session: Database session
        bin: Building Identification Number (primary key)

    Returns:
        List of reference image dicts
    """
    query = (
        select(ReferenceImage)
        .where(ReferenceImage.bin == bin)  # Use BIN (primary key) instead of BBL
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

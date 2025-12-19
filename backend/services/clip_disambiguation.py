"""
CLIP Disambiguation Service

On-demand CLIP embedding comparison for ambiguous building identification.
Only used when GPS+footprint cannot determine a clear winner (est. 20% of scans).

This service:
1. Checks for existing embeddings in reference_embeddings table
2. Falls back to user-contributed images
3. As last resort, fetches Street View on-demand (cached for future use)
4. Compares user photo against candidates to pick winner

Cost optimization:
- Only called for ambiguous cases (~20% of scans)
- Checks cached embeddings first (free)
- Only fetches Street View when necessary ($0.007 per image)
- Caches new embeddings to reduce future costs
"""

import numpy as np
import httpx
import logging
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from io import BytesIO

from models.config import get_settings
from models.footprints_session import get_footprints_db
from services.clip_matcher import encode_photo, get_model
from services.geospatial_v2 import calculate_bearing

logger = logging.getLogger(__name__)
settings = get_settings()

# Similarity threshold for confident match
SIMILARITY_CONFIDENCE_THRESHOLD = 0.70  # 70% similarity = confident
SIMILARITY_GAP_THRESHOLD = 0.10  # 10% gap between top 2 = clear winner


async def disambiguate_candidates(
    session: AsyncSession,
    user_photo_url: str,
    candidates: List[Dict[str, Any]],
    user_lat: float,
    user_lng: float,
    user_bearing: float
) -> Dict[str, Any]:
    """
    Use CLIP to disambiguate between similar-scoring building candidates.

    This is only called when GPS+footprint returns 2-3 buildings with
    similar visibility scores. Uses visual matching to pick the winner.

    Args:
        session: Database session
        user_photo_url: URL to user's uploaded photo
        candidates: List of ambiguous candidates (usually 2-3)
        user_lat: User GPS latitude
        user_lng: User GPS longitude
        user_bearing: User compass bearing

    Returns:
        Dictionary with:
        - matches: Re-ranked candidates with CLIP confidence
        - method: 'cached_embeddings', 'user_images', or 'street_view'
        - cost_usd: API cost incurred (0 if cached)
        - processing_time_ms: Time taken
    """
    import time
    start_time = time.time()

    logger.info(
        f"CLIP disambiguating {len(candidates)} candidates: "
        f"{[c.get('bin') for c in candidates]}"
    )

    # Encode user's photo
    user_embedding = await fetch_and_encode_image(user_photo_url)
    if user_embedding is None:
        logger.error("Failed to encode user photo")
        return {
            'matches': candidates,  # Return original order
            'method': 'failed',
            'cost_usd': 0,
            'error': 'Failed to encode user photo'
        }

    # Get reference embeddings for each candidate
    candidate_embeddings = {}
    method_used = 'cached_embeddings'
    total_cost = 0.0

    for candidate in candidates:
        bin_val = candidate.get('bin')
        if not bin_val:
            continue

        # Try to get embedding from cache first
        embedding, source, cost = await get_or_create_embedding(
            session=session,
            bin_val=bin_val,
            user_lat=user_lat,
            user_lng=user_lng,
            user_bearing=user_bearing
        )

        if embedding is not None:
            candidate_embeddings[bin_val] = {
                'embedding': embedding,
                'source': source
            }
            total_cost += cost

            # Track method (prioritize showing most expensive method used)
            if source == 'street_view' and method_used != 'street_view':
                method_used = 'street_view'
            elif source == 'user_images' and method_used == 'cached_embeddings':
                method_used = 'user_images'
        else:
            logger.warning(f"No embedding available for BIN {bin_val}")

    # Compare user photo against each candidate
    similarities = {}
    for bin_val, data in candidate_embeddings.items():
        ref_embedding = np.array(data['embedding'])
        similarity = float(np.dot(user_embedding, ref_embedding))
        similarities[bin_val] = {
            'similarity': similarity,
            'source': data['source']
        }
        logger.info(
            f"  BIN {bin_val}: {similarity:.3f} similarity ({data['source']})"
        )

    # Re-rank candidates by similarity
    ranked_candidates = []
    for candidate in candidates:
        bin_val = candidate.get('bin')
        if bin_val in similarities:
            candidate['clip_similarity'] = round(similarities[bin_val]['similarity'] * 100, 2)
            candidate['embedding_source'] = similarities[bin_val]['source']
            # Combine footprint score with CLIP score
            footprint_score = candidate.get('score', 50)
            clip_score = candidate['clip_similarity']
            # Weighted average: 40% footprint, 60% CLIP (CLIP is the tiebreaker)
            candidate['combined_score'] = round(0.4 * footprint_score + 0.6 * clip_score, 2)
        else:
            # No embedding - lower confidence
            candidate['clip_similarity'] = 0
            candidate['embedding_source'] = None
            candidate['combined_score'] = candidate.get('score', 50) * 0.5

        ranked_candidates.append(candidate)

    # Sort by combined score
    ranked_candidates.sort(key=lambda x: x.get('combined_score', 0), reverse=True)

    # Calculate confidence based on similarity gap
    if len(ranked_candidates) >= 2:
        top_sim = ranked_candidates[0].get('clip_similarity', 0)
        second_sim = ranked_candidates[1].get('clip_similarity', 0)
        gap = (top_sim - second_sim) / 100

        if gap >= SIMILARITY_GAP_THRESHOLD:
            confidence = min(95, top_sim + (gap * 20))
        else:
            confidence = top_sim * 0.8  # Lower confidence for close matches
    else:
        confidence = ranked_candidates[0].get('clip_similarity', 50) if ranked_candidates else 0

    # Set confidence on top match
    if ranked_candidates:
        ranked_candidates[0]['confidence'] = round(confidence, 2)

    processing_time = int((time.time() - start_time) * 1000)

    logger.info(
        f"Disambiguation complete: winner BIN {ranked_candidates[0].get('bin') if ranked_candidates else 'none'}, "
        f"confidence {confidence:.1f}%, method {method_used}, cost ${total_cost:.4f}"
    )

    return {
        'matches': ranked_candidates,
        'method': method_used,
        'cost_usd': round(total_cost, 4),
        'processing_time_ms': processing_time,
    }


async def get_or_create_embedding(
    session: AsyncSession,
    bin_val: str,
    user_lat: float,
    user_lng: float,
    user_bearing: float
) -> Tuple[Optional[List[float]], str, float]:
    """
    Get embedding for a building, creating one if necessary.

    Priority:
    1. Check reference_embeddings table (pre-computed, free)
    2. Check user_images table (community photos, compute on-fly)
    3. Fetch Street View on-demand (last resort, costs $0.007)

    Returns:
        Tuple of (embedding, source, cost_usd)
    """
    # Clean BIN
    clean_bin = str(bin_val).replace('.0', '')

    # 1. Check reference_embeddings table
    embedding = await check_reference_embeddings(session, clean_bin, user_bearing)
    if embedding is not None:
        logger.info(f"  Found cached embedding for BIN {clean_bin}")
        return (embedding, 'cached', 0.0)

    # 2. Check user-contributed images
    embedding = await check_user_images(session, clean_bin)
    if embedding is not None:
        logger.info(f"  Found user image embedding for BIN {clean_bin}")
        return (embedding, 'user_images', 0.0)

    # 3. Fetch Street View on-demand
    embedding, cost = await fetch_street_view_embedding(
        session, clean_bin, user_lat, user_lng, user_bearing
    )
    if embedding is not None:
        logger.info(f"  Fetched Street View embedding for BIN {clean_bin}")
        return (embedding, 'street_view', cost)

    logger.warning(f"  No embedding source available for BIN {clean_bin}")
    return (None, 'none', 0.0)


async def check_reference_embeddings(
    session: AsyncSession,
    bin_val: str,
    user_bearing: float
) -> Optional[List[float]]:
    """
    Check reference_embeddings table for pre-computed embeddings.

    Prefers embeddings closest to user's viewing angle.
    """
    try:
        # Query for embeddings, preferring angles close to user bearing
        result = await session.execute(
            text("""
                SELECT re.embedding, re.angle
                FROM reference_embeddings re
                JOIN buildings_full_merge_scanning b ON b.id = re.building_id
                WHERE REPLACE(b.bin, '.0', '') = :bin
                ORDER BY ABS(re.angle - :bearing)
                LIMIT 1
            """),
            {'bin': bin_val, 'bearing': user_bearing}
        )

        row = result.fetchone()
        if row and row[0]:
            # Parse embedding from string/list
            embedding = row[0]
            if isinstance(embedding, str):
                import json
                embedding = json.loads(embedding)
            return embedding

    except Exception as e:
        logger.error(f"Error checking reference_embeddings: {e}")

    return None


async def check_user_images(
    session: AsyncSession,
    bin_val: str
) -> Optional[List[float]]:
    """
    Check for user-contributed images and compute embedding if found.

    User images are stored in user_contributed_buildings table.
    """
    try:
        # Check for user images in user_contributed_buildings table
        result = await session.execute(
            text("""
                SELECT image_url
                FROM user_contributed_buildings
                WHERE bin = :bin
                AND image_url IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {'bin': bin_val}
        )

        row = result.fetchone()
        if row and row[0]:
            # Compute embedding from image
            embedding = await fetch_and_encode_image(row[0])
            if embedding is not None:
                return embedding.tolist()

    except Exception as e:
        # Table may not exist or other error - just skip
        logger.debug(f"No user images found for BIN {bin_val}: {e}")

    return None


async def fetch_street_view_embedding(
    session: AsyncSession,
    bin_val: str,
    user_lat: float,
    user_lng: float,
    user_bearing: float
) -> Tuple[Optional[List[float]], float]:
    """
    Fetch Street View image on-demand and compute embedding.

    This is the most expensive option ($0.007 per image) so it's
    only used when cached embeddings and user images aren't available.

    The embedding is cached in reference_embeddings for future use.

    Returns:
        Tuple of (embedding, cost_usd)
    """
    try:
        row = None

        # Get building centroid from Railway footprints database
        async with get_footprints_db() as footprints_db:
            if footprints_db:
                result = await footprints_db.execute(
                    text("""
                        SELECT
                            ST_X(centroid) as lng,
                            ST_Y(centroid) as lat
                        FROM building_footprints
                        WHERE bin = :bin
                    """),
                    {'bin': bin_val}
                )
                row = result.fetchone()

        if not row:
            # Fallback to buildings_full_merge_scanning
            result = await session.execute(
                text("""
                    SELECT geocoded_lng, geocoded_lat
                    FROM buildings_full_merge_scanning
                    WHERE REPLACE(bin, '.0', '') = :bin
                """),
                {'bin': bin_val}
            )
            row = result.fetchone()

        if not row or not row[0] or not row[1]:
            logger.warning(f"No location found for BIN {bin_val}")
            return (None, 0.0)

        building_lng = float(row[0])
        building_lat = float(row[1])

        # Calculate optimal camera heading (from user toward building)
        camera_heading = calculate_bearing(user_lat, user_lng, building_lat, building_lng)

        # Fetch Street View image
        image_bytes = await fetch_street_view_image(
            building_lat, building_lng, camera_heading
        )

        if image_bytes is None:
            logger.warning(f"Street View not available for BIN {bin_val}")
            return (None, 0.007)  # Still costs money even if no image

        # Compute CLIP embedding
        embedding = await encode_photo(image_bytes)
        embedding_list = embedding.tolist()

        # Cache the embedding for future use
        await cache_embedding(
            session, bin_val, embedding_list, camera_heading,
            f"street_view_ondemand"
        )

        return (embedding_list, 0.007)

    except Exception as e:
        logger.error(f"Error fetching Street View: {e}")
        return (None, 0.0)


async def fetch_street_view_image(
    lat: float,
    lng: float,
    heading: float,
    pitch: int = 10,
    fov: int = 60,
    size: str = "400x400"  # Smaller size for embedding (saves bandwidth)
) -> Optional[bytes]:
    """
    Fetch image from Google Street View Static API.

    Args:
        lat: Building latitude
        lng: Building longitude
        heading: Camera heading (0-360)
        pitch: Camera pitch (default 10 = slight upward tilt)
        fov: Field of view (default 60)
        size: Image size (smaller = faster, CLIP resizes anyway)

    Returns:
        Image bytes or None if failed
    """
    url = (
        f"https://maps.googleapis.com/maps/api/streetview?"
        f"size={size}&"
        f"location={lat},{lng}&"
        f"heading={heading}&"
        f"pitch={pitch}&"
        f"fov={fov}&"
        f"key={settings.google_maps_api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)

            if response.status_code == 200:
                # Check if it's an actual image or placeholder
                content_length = len(response.content)
                if content_length > 5000:
                    return response.content
                else:
                    logger.warning(f"Street View returned placeholder (no imagery)")
                    return None
            else:
                logger.error(f"Street View API error: {response.status_code}")
                return None

    except Exception as e:
        logger.error(f"Failed to fetch Street View: {e}")
        return None


async def cache_embedding(
    session: AsyncSession,
    bin_val: str,
    embedding: List[float],
    angle: float,
    source: str
) -> bool:
    """
    Cache a computed embedding in reference_embeddings for future use.
    """
    try:
        # Get building_id from buildings_full_merge_scanning
        result = await session.execute(
            text("""
                SELECT id FROM buildings_full_merge_scanning
                WHERE REPLACE(bin, '.0', '') = :bin
                LIMIT 1
            """),
            {'bin': bin_val}
        )
        row = result.fetchone()

        if not row:
            logger.warning(f"Cannot cache embedding: building not found for BIN {bin_val}")
            return False

        building_id = row[0]

        # Insert embedding
        import json
        embedding_str = json.dumps(embedding)

        await session.execute(
            text("""
                INSERT INTO reference_embeddings (
                    building_id, angle, pitch, image_key, embedding, source
                )
                VALUES (
                    :building_id, :angle, 10,
                    :image_key, CAST(:embedding AS vector(512)), :source
                )
                ON CONFLICT (building_id, angle, pitch) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
            """),
            {
                'building_id': building_id,
                'angle': int(angle),
                'image_key': f"ondemand/{bin_val}/{int(angle)}.jpg",
                'embedding': embedding_str,
                'source': source
            }
        )

        await session.commit()
        logger.info(f"Cached embedding for BIN {bin_val} at {angle}°")
        return True

    except Exception as e:
        logger.error(f"Failed to cache embedding: {e}")
        await session.rollback()
        return False


async def fetch_and_encode_image(image_url: str) -> Optional[np.ndarray]:
    """
    Fetch an image from URL and encode with CLIP.

    Returns:
        Normalized CLIP embedding as numpy array, or None if failed
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(image_url)

            if response.status_code != 200:
                logger.error(f"Failed to fetch image: HTTP {response.status_code}")
                return None

            embedding = await encode_photo(response.content)
            return embedding

    except Exception as e:
        logger.error(f"Failed to fetch/encode image: {e}")
        return None

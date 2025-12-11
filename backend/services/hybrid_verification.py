"""
Hybrid building verification service - Option 3

Handles building verification when no embeddings exist:
1. GPS + Cone → 1 candidate: Instant confirm (high confidence)
2. GPS + Cone → 2-5 candidates: Lazy fetch Street View → CLIP match
3. GPS + Cone → 6+ candidates: Return list for manual picker (too ambiguous)

This minimizes Google Maps API costs while maintaining accuracy.
"""

import logging
from typing import Dict, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from services import geospatial, clip_matcher, reference_images
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def verify_building_without_embeddings(
    db: AsyncSession,
    user_photo_url: str,
    gps_lat: float,
    gps_lng: float,
    compass_bearing: float,
    phone_pitch: float = 0,
) -> Dict:
    """
    Verify building when no CLIP embeddings exist in database.

    Uses hybrid approach:
    - 1 candidate: Instant verification (GPS+cone is confident)
    - 2-5 candidates: Lazy fetch Street View → CLIP comparison
    - 6+ candidates: Return candidates for manual selection

    Args:
        db: Database session
        user_photo_url: URL of user's uploaded photo
        gps_lat: User's latitude
        gps_lng: User's longitude
        compass_bearing: Compass bearing (0-360)
        phone_pitch: Phone pitch angle

    Returns:
        Dict with verification result:
        {
            'method': 'instant' | 'clip_fallback' | 'manual_picker',
            'confidence': float (0-1),
            'matches': List[Dict],  # Sorted by confidence
            'candidates': List[Dict],  # All candidates (for manual picker)
            'api_cost_usd': float,  # Google Maps API cost
            'processing_time_ms': int
        }
    """
    import time
    start_time = time.time()

    logger.info(f"Hybrid verification: No embeddings found, using GPS+cone at ({gps_lat}, {gps_lng}), bearing {compass_bearing}°")

    # Step 1: Get candidates using cone of vision
    candidates = await geospatial.get_candidate_buildings(
        db, gps_lat, gps_lng, compass_bearing, phone_pitch
    )

    num_candidates = len(candidates)
    logger.info(f"Cone of vision found {num_candidates} candidates")

    # === CASE 1: Single candidate - Instant verification ===
    if num_candidates == 1:
        logger.info("✅ Single candidate found - instant verification (high confidence)")

        candidate = candidates[0]

        # Calculate confidence based on geometry
        # Close + aligned = high confidence
        distance = candidate['distance_meters']
        bearing_diff = candidate['bearing_difference']

        # Confidence formula:
        # - Distance: 0-20m = 1.0, 20-50m = 0.8, 50-100m = 0.6
        # - Bearing: 0-10° = 1.0, 10-30° = 0.8, 30+° = 0.6
        distance_conf = 1.0 if distance < 20 else (0.8 if distance < 50 else 0.6)
        bearing_conf = 1.0 if bearing_diff < 10 else (0.8 if bearing_diff < 30 else 0.6)

        confidence = (distance_conf + bearing_conf) / 2

        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            'method': 'instant',
            'confidence': confidence,
            'matches': [{
                **candidate,
                'confidence': confidence,
                'verification_method': 'gps_cone_single'
            }],
            'candidates': candidates,
            'api_cost_usd': 0.0,  # No API calls
            'processing_time_ms': elapsed_ms
        }

    # === CASE 2: 2-5 candidates - Lazy fetch Street View ===
    elif 2 <= num_candidates <= 5:
        logger.info(f"🔍 {num_candidates} candidates - fetching Street View for CLIP comparison")

        # Fetch Street View images for each candidate
        reference_imgs = await _fetch_street_view_for_candidates(
            db, candidates, compass_bearing
        )

        if not reference_imgs:
            logger.warning("Failed to fetch Street View images, falling back to manual picker")
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                'method': 'manual_picker',
                'confidence': 0.0,
                'matches': [],
                'candidates': candidates,
                'api_cost_usd': 0.0,
                'processing_time_ms': elapsed_ms,
                'error': 'street_view_unavailable'
            }

        # Run CLIP comparison
        matches = await clip_matcher.compare_images(
            user_photo_url, candidates, reference_imgs
        )

        # Calculate API cost ($0.007 per Street View image)
        api_cost = len(reference_imgs) * 0.007

        elapsed_ms = int((time.time() - start_time) * 1000)

        logger.info(f"✅ CLIP comparison complete, top match: {matches[0]['bin']} ({matches[0]['confidence']:.2f})")

        return {
            'method': 'clip_fallback',
            'confidence': matches[0]['confidence'] if matches else 0.0,
            'matches': matches,
            'candidates': candidates,
            'api_cost_usd': round(api_cost, 4),
            'processing_time_ms': elapsed_ms
        }

    # === CASE 3: 0 or 6+ candidates - Manual picker ===
    else:
        if num_candidates == 0:
            logger.warning("No candidates found in cone of vision")
            reason = 'no_candidates'
        else:
            logger.warning(f"{num_candidates} candidates - too ambiguous for automatic verification")
            reason = 'too_many_candidates'

        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            'method': 'manual_picker',
            'confidence': 0.0,
            'matches': [],
            'candidates': candidates[:10],  # Limit to top 10 for picker UI
            'api_cost_usd': 0.0,
            'processing_time_ms': elapsed_ms,
            'reason': reason
        }


async def _fetch_street_view_for_candidates(
    db: AsyncSession,
    candidates: List[Dict],
    user_bearing: float
) -> Dict[str, List[Dict]]:
    """
    Fetch Street View images for multiple candidates.

    This is only called when we have 2-5 candidates and need CLIP comparison.
    Images are fetched fresh (not cached) to minimize storage costs.

    Args:
        db: Database session
        candidates: List of candidate buildings
        user_bearing: User's compass bearing for best view angle

    Returns:
        Dict mapping BIN to list of reference image dicts
    """
    import asyncio

    logger.info(f"Fetching Street View images for {len(candidates)} candidates")

    # Fetch images in parallel for speed
    tasks = []
    for candidate in candidates:
        task = reference_images.fetch_street_view(
            lat=candidate['latitude'],
            lng=candidate['longitude'],
            bearing=(user_bearing + 180) % 360,  # Opposite bearing for facade view
            pitch=settings.street_view_pitch,
            fov=settings.street_view_fov
        )
        tasks.append((candidate['bin'], task))

    # Execute all fetches in parallel
    results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)

    # Build reference image dict
    reference_imgs = {}
    for (bin_val, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.error(f"Failed to fetch Street View for BIN {bin_val}: {result}")
            continue

        if result:  # Image bytes returned
            # Store temporarily for CLIP comparison (don't save to R2 yet)
            reference_imgs[bin_val] = [{
                'image_bytes': result,
                'source': 'street_view_lazy',
                'angle': (user_bearing + 180) % 360,
                'pitch': settings.street_view_pitch
            }]
            logger.info(f"✅ Fetched Street View for BIN {bin_val}")
        else:
            logger.warning(f"No Street View imagery available for BIN {bin_val}")

    logger.info(f"Successfully fetched Street View for {len(reference_imgs)}/{len(candidates)} candidates")

    return reference_imgs


async def confirm_with_photo(
    db: AsyncSession,
    scan_id: str,
    photo_bytes: bytes,
    confirmed_bin: str,
    gps_lat: float,
    gps_lng: float,
    compass_bearing: float,
    phone_pitch: float = 0.0,
    user_id: Optional[str] = None,
) -> Dict:
    """
    Confirm building using photo from walk verification.

    This is called when:
    - User is on a walk and verifies building using GPS+cone
    - Building has no embeddings yet
    - User takes photo to contribute to model

    Flow:
    1. Store user photo in user-images bucket
    2. Generate CLIP embedding
    3. Add to reference_embeddings table
    4. Return success (no immediate matching needed)

    Args:
        db: Database session
        scan_id: Unique scan ID
        photo_bytes: User's photo bytes
        confirmed_bin: BIN user confirmed (from GPS+cone)
        gps_lat: User's latitude
        gps_lng: User's longitude
        compass_bearing: Compass bearing
        phone_pitch: Phone pitch angle
        user_id: Optional user ID

    Returns:
        Dict with confirmation result
    """
    from services.user_images import process_confirmed_scan
    from utils.storage import upload_image

    logger.info(f"[{scan_id}] Confirming building with photo: BIN {confirmed_bin}")

    # Upload photo temporarily for processing
    user_photo_url = await upload_image(
        photo_bytes,
        f"scans/{scan_id}.jpg",
        create_thumbnail=True
    )

    # Process and add to reference database
    result = await process_confirmed_scan(
        db=db,
        scan_id=scan_id,
        photo_bytes=photo_bytes,
        user_photo_url=user_photo_url,
        confirmed_bin=confirmed_bin,
        gps_lat=gps_lat,
        gps_lng=gps_lng,
        compass_bearing=compass_bearing,
        phone_pitch=phone_pitch,
        user_id=user_id,
    )

    if result['added_to_references']:
        logger.info(f"[{scan_id}] ✅ Photo added to reference database for BIN {confirmed_bin}")
    else:
        logger.error(f"[{scan_id}] ❌ Failed to add photo to reference database")

    return {
        'status': 'confirmed',
        'scan_id': scan_id,
        'confirmed_bin': confirmed_bin,
        'contribution': {
            'stored': result['stored_in_bin_folder'],
            'embedding_generated': result['embedding_generated'],
            'user_image_url': result['user_image_url']
        }
    }

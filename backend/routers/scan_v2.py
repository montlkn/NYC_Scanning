"""
Scan V2 API endpoints - Bulletproof building identification

This router implements the V2 scan system using:
1. GPS + footprint intersection as primary method (100% coverage)
2. On-demand CLIP disambiguation for ambiguous cases only
3. Progressive radius expansion for edge cases

Key improvements:
- Works for ALL 1.08M NYC buildings (vs 485 with embeddings in V1)
- Faster: <200ms for 80% of scans (no CLIP needed)
- Cheaper: CLIP only for ~20% of scans, cached embeddings used when available
- More reliable: Deterministic geometry math instead of ML model
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update, select
from datetime import datetime, timezone
import uuid
import logging
import time

from models.database import Scan
from models.session import get_db
from models.config import get_settings
from services import geospatial_v2, clip_disambiguation
from services.analytics import track_scan, track_confirmation
from services.user_images import process_confirmed_scan
from services.building_contribution import reverse_geocode_google
from utils.storage import upload_image

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

# Scan data cache for confirmation flow
_scan_cache = {}
_cache_ttl_seconds = 1800  # 30 minutes


def _clean_old_cache_entries():
    """Remove expired cache entries"""
    current_time = time.time()
    expired_keys = [
        key for key, value in _scan_cache.items()
        if current_time - value.get('timestamp', 0) > _cache_ttl_seconds
    ]
    for key in expired_keys:
        del _scan_cache[key]


@router.post("/scan")
async def scan_building_v2(
    photo: UploadFile = File(..., description="Building photo from user's camera"),
    gps_lat: float = Form(..., description="User's GPS latitude"),
    gps_lng: float = Form(..., description="User's GPS longitude"),
    compass_bearing: float = Form(..., description="Compass bearing (0-360, 0=North)"),
    phone_pitch: float = Form(0, description="Phone pitch angle (-90 to 90)"),
    phone_roll: float = Form(0, description="Phone roll angle"),
    gps_accuracy: float = Form(None, description="GPS accuracy in meters"),
    user_id: str = Form(None, description="Optional user ID for tracking"),
    db: AsyncSession = Depends(get_db)
):
    """
    V2 Scan endpoint - Bulletproof building identification.

    Flow:
    1. Upload user photo to R2
    2. Query building_footprints for cone intersection
    3. Classify result (single/clear_winner/ambiguous/none)
    4. If ambiguous: Use CLIP disambiguation
    5. Return ranked matches with verification method

    Returns:
        matches: Top 3 candidate buildings with confidence scores
        verification_method: How the result was determined
        show_picker: Whether UI should show building selection
        can_contribute: Whether user can contribute data
    """
    start_time = datetime.now()
    scan_id = str(uuid.uuid4())

    try:
        # Validate inputs
        if not (-90 <= gps_lat <= 90):
            raise HTTPException(status_code=400, detail="Invalid latitude")
        if not (-180 <= gps_lng <= 180):
            raise HTTPException(status_code=400, detail="Invalid longitude")
        if not (0 <= compass_bearing <= 360):
            raise HTTPException(status_code=400, detail="Invalid compass bearing")

        logger.info(
            f"[{scan_id}] V2 scan at ({gps_lat:.6f}, {gps_lng:.6f}), "
            f"bearing {compass_bearing:.1f}°, pitch {phone_pitch:.1f}°"
        )

        # === STEP 1: Process and upload user photo ===
        photo_start = time.time()
        photo_bytes = await photo.read()

        # Resize image for efficiency
        from PIL import Image
        from io import BytesIO

        image = Image.open(BytesIO(photo_bytes))
        max_size = 1024
        if max(image.size) > max_size:
            ratio = max_size / max(image.size)
            new_size = tuple(int(dim * ratio) for dim in image.size)
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        buffer = BytesIO()
        image.save(buffer, format='JPEG', quality=85, optimize=True)
        photo_bytes = buffer.getvalue()

        user_photo_url = await upload_image(
            photo_bytes,
            f"scans/{scan_id}.jpg",
            create_thumbnail=True
        )
        upload_time_ms = int((time.time() - photo_start) * 1000)
        logger.info(f"[{scan_id}] Photo uploaded in {upload_time_ms}ms")

        # Cache scan data for confirmation flow
        _clean_old_cache_entries()
        _scan_cache[scan_id] = {
            'photo_bytes': photo_bytes,
            'user_photo_url': user_photo_url,
            'gps_lat': gps_lat,
            'gps_lng': gps_lng,
            'compass_bearing': compass_bearing,
            'phone_pitch': phone_pitch,
            'user_id': user_id,
            'timestamp': time.time(),
        }

        # === STEP 2: Footprint cone query ===
        geo_start = time.time()
        footprint_result = await geospatial_v2.get_candidates_by_footprint(
            db, gps_lat, gps_lng, compass_bearing, phone_pitch
        )
        geo_time_ms = int((time.time() - geo_start) * 1000)

        candidates = footprint_result['candidates']
        classification = footprint_result['classification']
        is_ambiguous = footprint_result['is_ambiguous']

        logger.info(
            f"[{scan_id}] Footprint query: {len(candidates)} candidates, "
            f"classification={classification}, in {geo_time_ms}ms"
        )

        # === STEP 3: Handle based on classification ===

        # CASE D: No buildings found
        if classification == 'none':
            logger.info(f"[{scan_id}] No buildings in cone, expanding search...")

            # Try expanding radius
            expanded_result = await geospatial_v2.expand_search_radius(
                db, gps_lat, gps_lng, compass_bearing
            )

            if expanded_result['candidates']:
                candidates = expanded_result['candidates']
                classification = 'expanded_radius'
            else:
                # Still no buildings - offer contribution
                address_suggestions = await reverse_geocode_google(gps_lat, gps_lng)

                return JSONResponse(
                    status_code=200,
                    content={
                        'scan_id': scan_id,
                        'error': 'no_candidates',
                        'message': 'No buildings found. Try moving closer to a building.',
                        'matches': [],
                        'can_contribute': True,
                        'address_suggestions': address_suggestions,
                        'verification_method': 'none',
                        'processing_time_ms': int((datetime.now() - start_time).total_seconds() * 1000)
                    }
                )

        # Enrich candidates with metadata
        candidates = await geospatial_v2.enrich_candidates_with_metadata(db, candidates)

        # === STEP 4: CLIP disambiguation for ambiguous cases ===
        clip_time_ms = 0
        clip_cost = 0.0
        verification_method = f'footprint_{classification}'

        if is_ambiguous and len(candidates) >= 2:
            logger.info(f"[{scan_id}] Ambiguous result, using CLIP disambiguation...")

            clip_start = time.time()
            clip_result = await clip_disambiguation.disambiguate_candidates(
                session=db,
                user_photo_url=user_photo_url,
                candidates=candidates[:3],  # Only disambiguate top 3
                user_lat=gps_lat,
                user_lng=gps_lng,
                user_bearing=compass_bearing
            )
            clip_time_ms = int((time.time() - clip_start) * 1000)

            candidates = clip_result['matches']
            clip_cost = clip_result.get('cost_usd', 0)
            verification_method = f"clip_{clip_result['method']}"

            logger.info(
                f"[{scan_id}] CLIP disambiguation: {clip_result['method']}, "
                f"cost ${clip_cost:.4f}, in {clip_time_ms}ms"
            )

        # === STEP 5: Prepare response ===
        total_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        # Determine confidence and picker behavior
        if candidates:
            top_match = candidates[0]
            top_confidence = top_match.get('confidence') or top_match.get('combined_score') or top_match.get('score', 50)

            # Show picker if confidence is below threshold
            show_picker = top_confidence < settings.confidence_threshold * 100
        else:
            top_confidence = 0
            show_picker = True

        # Format matches for response
        matches = []
        for i, c in enumerate(candidates[:3]):
            match = {
                'bin': c.get('bin'),
                'bbl': c.get('bbl'),
                'address': c.get('address'),
                'name': c.get('building_name') or c.get('name') or c.get('address'),
                'distance_meters': c.get('distance_meters'),
                'bearing_difference': c.get('bearing_difference'),
                'confidence': round(c.get('confidence') or c.get('combined_score') or c.get('score', 0), 2),
                'is_landmark': c.get('is_landmark', False),
                # BuildingInfo screen fields
                'architect': c.get('architect'),
                'style': c.get('style'),
                'year_built': c.get('year_built'),
                'use': c.get('use'),
                'type': c.get('type'),
                'materials': c.get('materials'),
            }

            # Add CLIP data if available
            if 'clip_similarity' in c:
                match['clip_similarity'] = c['clip_similarity']

            matches.append(match)

        # Store scan in database
        try:
            scan = Scan(
                id=scan_id,
                user_id=user_id,
                user_photo_url=user_photo_url,
                gps_lat=gps_lat,
                gps_lng=gps_lng,
                gps_accuracy=gps_accuracy,
                compass_bearing=compass_bearing,
                phone_pitch=phone_pitch,
                phone_roll=phone_roll,
                candidate_bins=[m['bin'] for m in matches],
                top_match_bin=matches[0]['bin'] if matches else None,
                top_confidence=top_confidence,
                processing_time_ms=total_time_ms,
                num_candidates=len(candidates),
                geospatial_query_ms=geo_time_ms,
                clip_comparison_ms=clip_time_ms if clip_time_ms > 0 else None,
                created_at=datetime.now(timezone.utc)
            )
            db.add(scan)
            await db.commit()
        except Exception as e:
            logger.error(f"[{scan_id}] Failed to store scan: {e}")
            await db.rollback()

        # Track analytics
        track_scan(scan_id, {
            'confidence': top_confidence,
            'num_candidates': len(candidates),
            'processing_time_ms': total_time_ms,
            'status': 'match_found' if matches else 'no_candidates',
            'bin': matches[0]['bin'] if matches else None,
            'verification_method': verification_method,
        })

        logger.info(
            f"[{scan_id}] Scan complete: {verification_method}, "
            f"confidence {top_confidence:.1f}%, {total_time_ms}ms"
        )

        return {
            'scan_id': scan_id,
            'matches': matches,
            'show_picker': show_picker,
            'can_contribute': True,
            'verification_method': verification_method,
            'processing_time_ms': total_time_ms,
            'performance': {
                'upload_ms': upload_time_ms,
                'geospatial_ms': geo_time_ms,
                'clip_ms': clip_time_ms if clip_time_ms > 0 else None,
                'clip_cost_usd': clip_cost if clip_cost > 0 else None,
            },
            'debug_info': {
                'num_candidates': len(candidates),
                'classification': classification,
                'was_ambiguous': is_ambiguous,
            } if settings.debug else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Scan failed: {e}", exc_info=True)

        # Store error in database
        try:
            scan = Scan(
                id=scan_id,
                user_id=user_id,
                error_message=str(e),
                error_type=type(e).__name__,
                gps_lat=gps_lat,
                gps_lng=gps_lng,
                compass_bearing=compass_bearing,
                created_at=datetime.now(timezone.utc)
            )
            db.add(scan)
            await db.commit()
        except Exception as db_error:
            logger.error(f"[{scan_id}] Failed to store error: {db_error}")

        raise HTTPException(
            status_code=500,
            detail=f"Scan failed: {str(e)}" if settings.debug else "Scan failed"
        )


@router.post("/scans/{scan_id}/confirm")
async def confirm_building_v2(
    scan_id: str,
    confirmed_bin: str = Form(..., description="BIN of confirmed building"),
    confirmation_time_ms: int = Form(None, description="Time taken to confirm (ms)"),
    user_id: str = Form(None, description="User ID for tracking"),
    db: AsyncSession = Depends(get_db)
):
    """
    Confirm which building the user scanned.

    This:
    1. Updates scan record with confirmation
    2. If confirmed BIN was in top 3, stores user photo for future CLIP matching
    3. Tracks accuracy for analytics
    """
    try:
        logger.info(f"[{scan_id}] V2 confirmation: BIN {confirmed_bin}")

        # Fetch scan record
        scan = await db.get(Scan, scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        # Check if confirmed BIN was in top 3
        was_in_top_3 = (
            scan.candidate_bins and
            confirmed_bin in scan.candidate_bins[:3]
        )

        was_correct = scan.top_match_bin == confirmed_bin

        # Process photo for re-embedding if in top 3
        reembedding_result = None
        if scan_id in _scan_cache and was_in_top_3:
            cached_data = _scan_cache[scan_id]

            reembedding_result = await process_confirmed_scan(
                db=db,
                scan_id=scan_id,
                photo_bytes=cached_data['photo_bytes'],
                user_photo_url=cached_data['user_photo_url'],
                confirmed_bin=confirmed_bin,
                gps_lat=cached_data['gps_lat'],
                gps_lng=cached_data['gps_lng'],
                compass_bearing=cached_data['compass_bearing'],
                phone_pitch=cached_data.get('phone_pitch', 0.0),
                user_id=cached_data.get('user_id'),
            )

            del _scan_cache[scan_id]
            logger.info(f"[{scan_id}] Photo processed for re-embedding")
        elif scan_id in _scan_cache:
            del _scan_cache[scan_id]
            logger.info(f"[{scan_id}] Photo discarded (not in top 3)")

        # Update scan record
        await db.execute(
            update(Scan)
            .where(Scan.id == scan_id)
            .values(
                confirmed_bin=confirmed_bin,
                confirmed_at=datetime.now(timezone.utc),
                confirmation_time_ms=confirmation_time_ms,
                was_correct=was_correct
            )
        )
        await db.commit()

        # Track confirmation
        track_confirmation(scan_id, confirmed_bin, was_top_match=was_correct)

        # Calculate rewards
        if was_in_top_3 and reembedding_result and reembedding_result.get('added_to_references'):
            rewards = {'xp': 10, 'message': 'Photo contribution accepted! +10 XP'}
        elif was_correct:
            rewards = {'xp': 5, 'message': 'Confirmed! +5 XP'}
        else:
            rewards = {'xp': 2, 'message': 'Feedback recorded! +2 XP'}

        return {
            'status': 'confirmed',
            'scan_id': scan_id,
            'confirmed_bin': confirmed_bin,
            'was_in_top_3': was_in_top_3,
            'was_correct': was_correct,
            'embedding_generated': was_in_top_3 and (reembedding_result is not None),
            'rewards': rewards
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Confirmation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to confirm scan")


@router.get("/scan/health")
async def scan_health_check(db: AsyncSession = Depends(get_db)):
    """
    Health check for V2 scan system.

    Verifies:
    - Database connection
    - building_footprints table exists and has data
    - PostGIS functions are available
    """
    try:
        # Check footprints table
        from sqlalchemy import text

        result = await db.execute(
            text("SELECT COUNT(*) FROM building_footprints")
        )
        footprint_count = result.scalar()

        # Check PostGIS function
        result = await db.execute(
            text("""
                SELECT COUNT(*) FROM find_buildings_in_cone(
                    40.7128, -74.0060, 45, 100, 60, 5
                )
            """)
        )
        test_count = result.scalar()

        return {
            'status': 'healthy',
            'version': 'v2',
            'footprints_loaded': footprint_count,
            'test_query_results': test_count,
            'postgis_working': test_count is not None
        }

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                'status': 'unhealthy',
                'error': str(e),
                'version': 'v2',
                'footprints_loaded': 0
            }
        )

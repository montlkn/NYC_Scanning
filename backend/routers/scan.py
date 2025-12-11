"""
Scan API endpoints - Main building identification flow
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update, select
from datetime import datetime, timezone
import uuid
import logging
import time

from models.database import Scan, ScanFeedback
from models.session import get_db
from services import geospatial, reference_images, clip_matcher, hybrid_verification, stamps
from services.analytics import track_scan, track_confirmation
from services.user_images import process_confirmed_scan, store_user_image
from services.building_contribution import reverse_geocode_google, lookup_bin_from_gps
from utils.storage import upload_image
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

# In-memory cache for scan data (needed for confirmation flow)
# Key: scan_id, Value: dict with photo_bytes, gps_lat, gps_lng, compass_bearing, user_id
# TTL: 30 minutes
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
        logger.info(f"Removed expired scan cache entry: {key}")


@router.post("/scan")
async def scan_building(
    photo: UploadFile = File(..., description="Building photo from user's camera"),
    gps_lat: float = Form(..., description="User's GPS latitude"),
    gps_lng: float = Form(..., description="User's GPS longitude"),
    compass_bearing: float = Form(..., description="Compass bearing (0-360, 0=North)"),
    phone_pitch: float = Form(0, description="Phone pitch angle (-90 to 90)"),
    phone_roll: float = Form(0, description="Phone roll angle"),
    altitude: float = Form(None, description="Altitude in meters (from barometer)"),
    floor: int = Form(None, description="Estimated floor number"),
    confidence: int = Form(None, description="Position confidence score (0-100)"),
    movement_type: str = Form(None, description="Movement type: stationary/walking/running"),
    gps_accuracy: float = Form(None, description="GPS accuracy in meters"),
    user_id: str = Form(None, description="Optional user ID for tracking"),
    db: AsyncSession = Depends(get_db)
):
    """
    Main scan endpoint - identifies building from photo + GPS + compass

    Process:
    1. Upload user photo to R2
    2. Geospatial filtering (cone-of-vision)
    3. Fetch reference images for candidates
    4. CLIP comparison
    5. Return sorted matches
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

        logger.info(f"[{scan_id}] Starting scan at ({gps_lat}, {gps_lng}), bearing {compass_bearing}°, floor {floor}, confidence {confidence}%")

        # === STEP 1: Resize and upload user photo ===
        photo_start = time.time()
        photo_bytes = await photo.read()

        # Resize image to reduce upload time (CLIP uses 224x224 anyway)
        from PIL import Image
        from io import BytesIO

        image = Image.open(BytesIO(photo_bytes))

        # Resize to max 1024px on longest side (preserves aspect ratio)
        max_size = 1024
        if max(image.size) > max_size:
            ratio = max_size / max(image.size)
            new_size = tuple(int(dim * ratio) for dim in image.size)
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        # Compress to JPEG with quality 85
        buffer = BytesIO()
        image.save(buffer, format='JPEG', quality=85, optimize=True)
        photo_bytes = buffer.getvalue()

        logger.info(f"[{scan_id}] Resized image to {image.size}, size: {len(photo_bytes) / 1024:.1f}KB")

        user_photo_url = await upload_image(
            photo_bytes,
            f"scans/{scan_id}.jpg",
            create_thumbnail=True
        )
        upload_time_ms = int((time.time() - photo_start) * 1000)
        logger.info(f"[{scan_id}] Photo processed and uploaded in {upload_time_ms}ms")

        # Cache scan data for confirmation flow (needed for re-embedding)
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
        logger.info(f"[{scan_id}] Cached scan data for confirmation flow")

        # === STEP 2: Geospatial filtering ===
        geo_start = time.time()
        candidates = await geospatial.get_candidate_buildings(
            db, gps_lat, gps_lng, compass_bearing, phone_pitch
        )
        geo_time_ms = int((time.time() - geo_start) * 1000)
        logger.info(f"[{scan_id}] Found {len(candidates)} candidates in {geo_time_ms}ms")

        if len(candidates) == 0:
            logger.warning(f"[{scan_id}] No candidate buildings found - fetching address suggestions")

            # When no candidates found, get address suggestions from Google for contribution flow
            address_suggestions = []
            bin_bbl_lookup = None
            try:
                # Get addresses from Google Maps reverse geocode
                address_suggestions = await reverse_geocode_google(gps_lat, gps_lng)
                logger.info(f"[{scan_id}] Found {len(address_suggestions)} address suggestions from Google")

                # Also try to find BIN/BBL from PLUTO data
                bin_bbl_result = lookup_bin_from_gps(gps_lat, gps_lng, radius_meters=50)
                if bin_bbl_result:
                    bin_value, bbl_value = bin_bbl_result
                    bin_bbl_lookup = {
                        'bin': bin_value,
                        'bbl': bbl_value
                    }
                    logger.info(f"[{scan_id}] Found BIN/BBL from PLUTO: BIN={bin_value}, BBL={bbl_value}")
            except Exception as e:
                logger.error(f"[{scan_id}] Failed to get address suggestions: {e}")

            return JSONResponse(
                status_code=200,
                content={
                    'scan_id': scan_id,
                    'error': 'no_candidates',
                    'message': 'No buildings found in our database. Help us by contributing!',
                    'matches': [],
                    'can_contribute': True,
                    'address_suggestions': address_suggestions,
                    'bin_bbl_lookup': bin_bbl_lookup,
                    'processing_time_ms': int((datetime.now() - start_time).total_seconds() * 1000)
                }
            )

        # === STEP 3: Get reference images ===
        ref_start = time.time()
        reference_imgs = await reference_images.get_reference_images_for_candidates(
            db, candidates, compass_bearing
        )
        ref_time_ms = int((time.time() - ref_start) * 1000)
        logger.info(f"[{scan_id}] Fetched {len(reference_imgs)} reference images in {ref_time_ms}ms")

        # === STEP 3.5: Hybrid verification if no reference images ===
        if len(reference_imgs) == 0:
            logger.warning(f"[{scan_id}] No reference images available - using hybrid verification")

            hybrid_result = await hybrid_verification.verify_building_without_embeddings(
                db=db,
                user_photo_url=user_photo_url,
                gps_lat=gps_lat,
                gps_lng=gps_lng,
                compass_bearing=compass_bearing,
                phone_pitch=phone_pitch
            )

            total_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            # Store scan for analytics
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
                    candidate_bins=[c['bin'] for c in hybrid_result['candidates'][:5]],
                    top_match_bin=hybrid_result['matches'][0]['bin'] if hybrid_result['matches'] else None,
                    top_confidence=hybrid_result['matches'][0]['confidence'] if hybrid_result['matches'] else 0,
                    processing_time_ms=total_time_ms,
                    num_candidates=len(hybrid_result['candidates']),
                    geospatial_query_ms=geo_time_ms,
                    image_fetch_ms=ref_time_ms,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(scan)
                await db.commit()
            except Exception as e:
                logger.error(f"[{scan_id}] Failed to store scan: {e}")
                await db.rollback()

            # Determine UI behavior based on hybrid method
            method = hybrid_result['method']
            if method == 'instant':
                show_picker = False  # High confidence single match
                message = 'Building verified using GPS and compass'
            elif method == 'clip_fallback':
                show_picker = hybrid_result['confidence'] < settings.confidence_threshold
                message = 'Building identified using visual matching'
            else:  # manual_picker
                show_picker = True
                if hybrid_result.get('reason') == 'no_candidates':
                    message = 'No buildings found in your view. Try getting closer or adjusting your angle.'
                else:
                    message = 'Multiple buildings detected. Please select the correct one.'

            return {
                'scan_id': scan_id,
                'matches': hybrid_result['matches'][:3],
                'show_picker': show_picker,
                'can_contribute': True,  # Always allow contribution for buildings without embeddings
                'processing_time_ms': total_time_ms,
                'verification_method': method,
                'message': message,
                'performance': {
                    'upload_ms': upload_time_ms,
                    'geospatial_ms': geo_time_ms,
                    'reference_images_ms': ref_time_ms,
                    'hybrid_verification_ms': hybrid_result['processing_time_ms'],
                    'api_cost_usd': hybrid_result['api_cost_usd'],
                },
                'debug_info': {
                    'num_candidates': len(hybrid_result['candidates']),
                    'hybrid_method': method,
                    'num_matches': len(hybrid_result['matches']),
                } if settings.debug else None
            }

        # === STEP 4: CLIP comparison ===
        clip_start = time.time()
        matches = await clip_matcher.compare_images(
            user_photo_url, candidates, reference_imgs
        )
        clip_time_ms = int((time.time() - clip_start) * 1000)
        logger.info(f"[{scan_id}] CLIP comparison completed in {clip_time_ms}ms")

        # === STEP 5: Store scan for analytics ===
        total_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        # Store scan in database for analytics
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
                candidate_bins=[m['bin'] for m in matches[:5]],
                top_match_bin=matches[0]['bin'] if matches else None,
                top_confidence=matches[0]['confidence'] if matches else 0,
                processing_time_ms=total_time_ms,
                num_candidates=len(candidates),
                geospatial_query_ms=geo_time_ms,
                image_fetch_ms=ref_time_ms,
                clip_comparison_ms=clip_time_ms,
                created_at=datetime.now(timezone.utc)
            )
            db.add(scan)
            await db.commit()
            logger.info(f"[{scan_id}] Scan stored in database")
        except Exception as e:
            logger.error(f"[{scan_id}] Failed to store scan in database: {e}")
            # Don't fail the request if database storage fails
            await db.rollback()

        # Determine if we should show picker UI
        show_picker = True
        if matches and matches[0]['confidence'] >= settings.confidence_threshold:
            show_picker = False

        logger.info(f"[{scan_id}] Scan completed in {total_time_ms}ms")

        # Track scan event for analytics
        track_result = {
            'confidence': matches[0]['confidence'] if matches else 0,
            'num_candidates': len(candidates),
            'processing_time_ms': total_time_ms,
            'status': 'match_found' if matches else 'no_candidates',
            'bin': matches[0]['bin'] if matches else None,
        }
        track_scan(scan_id, track_result)

        # Determine if user can contribute this building
        # Allow contribution if: no matches OR low confidence OR user wants to add metadata
        can_contribute = (
            not matches or  # No matches found
            (matches and matches[0]['confidence'] < 0.7)  # Low confidence match
        )

        return {
            'scan_id': scan_id,
            'matches': matches[:3],  # Return top 3
            'show_picker': show_picker,
            'can_contribute': can_contribute,  # NEW: Allow building contribution
            'processing_time_ms': total_time_ms,
            'performance': {
                'upload_ms': upload_time_ms,
                'geospatial_ms': geo_time_ms,
                'reference_images_ms': ref_time_ms,
                'clip_comparison_ms': clip_time_ms,
            },
            'debug_info': {
                'num_candidates': len(candidates),
                'num_reference_images': len(reference_imgs),
                'num_matches': len(matches),
            } if settings.debug else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Scan failed: {e}", exc_info=True)

        # Store error in database for debugging
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
            logger.error(f"[{scan_id}] Failed to store error in database: {db_error}")
            # Ignore database errors when already handling an error

        raise HTTPException(
            status_code=500,
            detail=f"Scan failed: {str(e)}" if settings.debug else "Scan failed"
        )


@router.post("/scans/{scan_id}/confirm")
async def confirm_building(
    scan_id: str,
    confirmed_bin: str = Form(..., description="BIN of confirmed building"),
    confirmation_time_ms: int = Form(None, description="Time taken to confirm (ms)"),
    user_contributed_address: str = Form(None, description="Optional: Address contributed by user"),
    user_contributed_architect: str = Form(None, description="Optional: Architect name"),
    user_contributed_year_built: int = Form(None, description="Optional: Year built"),
    user_contributed_style: str = Form(None, description="Optional: Architectural style"),
    user_contributed_notes: str = Form(None, description="Optional: Additional notes"),
    user_contributed_mat_prim: str = Form(None, description="Optional: Primary material"),
    user_contributed_mat_secondary: str = Form(None, description="Optional: Secondary material"),
    user_contributed_mat_tertiary: str = Form(None, description="Optional: Tertiary material"),
    user_id: str = Form(None, description="User ID for tracking"),
    db: AsyncSession = Depends(get_db)
):
    """
    User confirms which building they scanned
    Used for accuracy tracking and model improvement
    Now uses BIN (Building Identification Number) instead of BBL

    IMPORTANT: Only stores user photo + generates embedding if confirmed BIN
    is in the top 3 matches. This prevents database poisoning from incorrect
    user confirmations.

    This also:
    1. Validates confirmed BIN is in top 3 matches
    2. Stores the user's image in the building's BIN folder (if valid)
    3. Generates a CLIP embedding for the image (if valid)
    4. Adds the image to reference_embeddings table (if valid)
    """
    try:
        logger.info(f"[{scan_id}] User confirmed BIN: {confirmed_bin}")

        # First, fetch the scan to check if confirmed BIN was in top 3 matches
        scan_result = await db.execute(select(Scan).where(Scan.id == scan_id))
        scan = scan_result.scalar_one_or_none()

        if not scan:
            logger.error(f"[{scan_id}] Scan not found in database")
            raise HTTPException(status_code=404, detail="Scan not found")

        # Check if confirmed BIN was in the top 3 matches (candidate_bins field)
        # This prevents poisoning the database with wrong confirmations
        was_in_top_3 = False
        is_pioneer_contribution = False

        if scan.candidate_bins and confirmed_bin in scan.candidate_bins[:3]:
            was_in_top_3 = True
            logger.info(f"[{scan_id}] ✅ Confirmed BIN {confirmed_bin} was in top 3 matches - will store embedding")
        else:
            was_in_top_3 = False
            logger.warning(f"[{scan_id}] ⚠️ Confirmed BIN {confirmed_bin} was NOT in top 3 matches - will NOT store embedding (prevents poisoning)")
            logger.warning(f"[{scan_id}] Top 3 were: {scan.candidate_bins[:3] if scan.candidate_bins else 'None'}")

            # EXCEPTION: If user provides address verification, they become a "Pioneer Contributor"
            # This rewards users who help improve the database while maintaining quality
            if user_contributed_address and len(user_contributed_address.strip()) > 5:
                is_pioneer_contribution = True
                logger.info(f"[{scan_id}] 🏆 PIONEER CONTRIBUTION: User provided address '{user_contributed_address}' for non-top-3 building")
                logger.info(f"[{scan_id}] Will NOT store embedding (prevents poisoning) but user gets bonus rewards")

        # Track confirmation for analytics
        track_confirmation(scan_id, confirmed_bin, was_top_match=(scan.top_match_bin == confirmed_bin))

        # Only process re-embedding if confirmed BIN was in top 3
        reembedding_result = None
        if scan_id in _scan_cache and was_in_top_3:
            cached_data = _scan_cache[scan_id]
            logger.info(f"[{scan_id}] Found cached scan data, processing for re-embedding...")

            # Process the confirmed scan - store in BIN folder and add to references
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

            # Clean up cache entry after processing
            del _scan_cache[scan_id]
            logger.info(f"[{scan_id}] Removed scan from cache after processing")
        elif scan_id in _scan_cache and not was_in_top_3:
            # User selected building NOT in top 3 - discard photo, don't add to embeddings
            logger.warning(f"[{scan_id}] Discarding photo - confirmed BIN not in top 3 (prevents poisoning)")
            # Still clean up cache
            del _scan_cache[scan_id]
        else:
            logger.warning(f"[{scan_id}] No cached scan data found - cannot re-embed image")

        # Update scan record with confirmation
        try:
            # Calculate if the top match was correct
            was_correct = scan.top_match_bin == confirmed_bin

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
            logger.info(f"[{scan_id}] Scan confirmation stored in database (was_correct={was_correct}, was_in_top_3={was_in_top_3})")
        except Exception as e:
            logger.error(f"[{scan_id}] Failed to update scan confirmation: {e}")
            await db.rollback()
            # Don't fail the request if database update fails

        # Calculate rewards based on contribution type
        rewards = {}
        contribution_result = None

        # Check if user provided any contribution data
        contribution_data = {
            'address': user_contributed_address,
            'architect': user_contributed_architect,
            'year_built': user_contributed_year_built,
            'style': user_contributed_style,
            'notes': user_contributed_notes,
            'mat_prim': user_contributed_mat_prim,
            'mat_secondary': user_contributed_mat_secondary,
            'mat_tertiary': user_contributed_mat_tertiary
        }

        has_contribution = any([
            user_contributed_address and len(user_contributed_address.strip()) > 5,
            user_contributed_architect and len(user_contributed_architect.strip()) > 2,
            user_contributed_year_built,
            user_contributed_style and len(user_contributed_style.strip()) > 2,
            user_contributed_notes and len(user_contributed_notes.strip()) > 10,
            user_contributed_mat_prim and len(user_contributed_mat_prim.strip()) > 2,
            user_contributed_mat_secondary and len(user_contributed_mat_secondary.strip()) > 2,
            user_contributed_mat_tertiary and len(user_contributed_mat_tertiary.strip()) > 2
        ])

        # Record contribution and award stamps if user provided data
        if has_contribution and user_id:
            contribution_result = await stamps.record_contribution(
                db=db,
                scan_id=scan_id,
                user_id=user_id,
                confirmed_bin=confirmed_bin,
                contribution_data=contribution_data,
                was_in_top_3=was_in_top_3
            )

            if contribution_result['success']:
                rewards = {
                    'xp': contribution_result['xp_awarded'],
                    'stamps': contribution_result['stamps_awarded'],
                    'contribution_type': contribution_result['contribution_type'],
                    'is_pioneer': contribution_result['is_pioneer'],
                    'message': f"🏆 {contribution_result['xp_awarded']} XP + {len(contribution_result['stamps_awarded'])} stamp(s)!"
                }
        elif was_in_top_3 and reembedding_result and reembedding_result.get('added_to_references'):
            # Standard contribution: Photo used for training, no extra data
            rewards = {
                'xp': 10,
                'stamps': [],
                'message': 'Photo contribution accepted! +10 XP'
            }
            # Update user achievements
            if user_id:
                await stamps.update_user_achievements(db, user_id, xp_delta=10, confirmation_delta=1)
        elif not was_in_top_3 and not has_contribution:
            # Basic feedback: Just confirmation, no contribution
            rewards = {
                'xp': 2,
                'stamps': [],
                'message': 'Thanks for the feedback! +2 XP'
            }
            if user_id:
                await stamps.update_user_achievements(db, user_id, xp_delta=2, confirmation_delta=1)
        else:
            # Fallback
            rewards = {
                'xp': 5,
                'stamps': [],
                'message': 'Confirmation recorded! +5 XP'
            }
            if user_id:
                await stamps.update_user_achievements(db, user_id, xp_delta=5, confirmation_delta=1)

        response = {
            'status': 'confirmed',
            'scan_id': scan_id,
            'confirmed_bin': confirmed_bin,
            'was_in_top_3': was_in_top_3,
            'is_pioneer_contribution': is_pioneer_contribution,
            'embedding_generated': was_in_top_3 and (reembedding_result is not None),
            'rewards': rewards
        }

        # Add re-embedding result if available
        if reembedding_result:
            response['reembedding'] = reembedding_result
            if reembedding_result.get('added_to_references'):
                logger.info(f"[{scan_id}] Successfully added user image to reference database for BIN {confirmed_bin}")
        elif not was_in_top_3 and not is_pioneer_contribution:
            response['note'] = 'Photo not used for model training (not in top 3 matches)'

        return response

    except Exception as e:
        logger.error(f"Failed to confirm scan: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to confirm scan")


@router.post("/scans/{scan_id}/feedback")
async def submit_feedback(
    scan_id: str,
    rating: int = Form(..., ge=1, le=5, description="Rating 1-5"),
    feedback_text: str = Form(None, description="Optional feedback text"),
    feedback_type: str = Form(None, description="Feedback type: correct/incorrect/slow/no_match"),
    db: AsyncSession = Depends(get_db)
):
    """
    Submit feedback on scan results
    """
    try:
        logger.info(f"[{scan_id}] Feedback: {rating} stars, type: {feedback_type}")

        # Store feedback in database
        feedback = ScanFeedback(
            scan_id=scan_id,
            rating=rating,
            feedback_text=feedback_text,
            feedback_type=feedback_type,
            created_at=datetime.now(timezone.utc)
        )
        db.add(feedback)
        await db.commit()

        logger.info(f"[{scan_id}] Feedback stored in database")

        return {
            'status': 'success',
            'message': 'Thank you for your feedback!'
        }

    except Exception as e:
        logger.error(f"[{scan_id}] Failed to submit feedback: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to submit feedback")


@router.post("/confirm-with-photo")
async def confirm_with_photo(
    photo: UploadFile = File(..., description="Building photo from user's camera"),
    confirmed_bin: str = Form(..., description="BIN confirmed by user (from GPS+cone)"),
    gps_lat: float = Form(..., description="User's GPS latitude"),
    gps_lng: float = Form(..., description="User's GPS longitude"),
    compass_bearing: float = Form(..., description="Compass bearing (0-360)"),
    phone_pitch: float = Form(0, description="Phone pitch angle"),
    user_id: str = Form(None, description="Optional user ID"),
    db: AsyncSession = Depends(get_db)
):
    """
    Confirm building with photo for walk verification.

    Used when:
    - User is on a walk and verifies building using GPS+cone
    - Building has no embeddings yet
    - User takes photo to contribute to model

    IMPORTANT: Validates that confirmed BIN is actually in cone of vision
    to prevent database poisoning from GPS drift or incorrect frontend logic.

    This endpoint:
    1. Validates confirmed BIN is in cone of vision
    2. Stores user photo in user-images bucket (if valid)
    3. Generates CLIP embedding (if valid)
    4. Adds to reference_embeddings table (if valid)
    5. Returns success (no immediate matching needed)

    Unlike /scan, this doesn't do CLIP matching - it just adds the photo
    to improve the model for future scans.
    """
    scan_id = str(uuid.uuid4())

    try:
        logger.info(f"[{scan_id}] Confirming building with photo: BIN {confirmed_bin}")

        # SAFETY CHECK: Verify confirmed BIN is in cone of vision
        # This prevents poisoning from GPS drift or incorrect frontend logic
        candidates = await geospatial.get_candidate_buildings(
            db, gps_lat, gps_lng, compass_bearing, phone_pitch
        )

        candidate_bins = [c['bin'] for c in candidates]
        if confirmed_bin not in candidate_bins:
            logger.error(f"[{scan_id}] ⚠️ REJECTED: Confirmed BIN {confirmed_bin} NOT in cone of vision (prevents poisoning)")
            logger.error(f"[{scan_id}] Candidates in cone: {candidate_bins[:5]}")
            return {
                'status': 'rejected',
                'scan_id': scan_id,
                'confirmed_bin': confirmed_bin,
                'error': 'bin_not_in_cone',
                'message': 'Building not in cone of vision. Photo not used for model training.',
                'embedding_generated': False
            }

        logger.info(f"[{scan_id}] ✅ Confirmed BIN {confirmed_bin} is in cone of vision - will process")

        # Read and process photo
        photo_bytes = await photo.read()

        # Resize image
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

        # Confirm and process
        result = await hybrid_verification.confirm_with_photo(
            db=db,
            scan_id=scan_id,
            photo_bytes=photo_bytes,
            confirmed_bin=confirmed_bin,
            gps_lat=gps_lat,
            gps_lng=gps_lng,
            compass_bearing=compass_bearing,
            phone_pitch=phone_pitch,
            user_id=user_id,
        )

        # Track contribution for analytics
        track_confirmation(scan_id, confirmed_bin, was_top_match=True)

        return result

    except Exception as e:
        logger.error(f"[{scan_id}] Failed to confirm with photo: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to confirm building")


@router.get("/scans/{scan_id}")
async def get_scan(
    scan_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get scan details by ID
    """
    try:
        # Fetch scan from database
        scan = await db.get(Scan, scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        # Convert to dict for JSON response
        return {
            'scan_id': scan.id,
            'user_id': scan.user_id,
            'user_photo_url': scan.user_photo_url,
            'gps_lat': scan.gps_lat,
            'gps_lng': scan.gps_lng,
            'compass_bearing': scan.compass_bearing,
            'phone_pitch': scan.phone_pitch,
            'candidate_bins': scan.candidate_bins,
            'top_match_bin': scan.top_match_bin,
            'top_confidence': scan.top_confidence,
            'confirmed_bin': scan.confirmed_bin,
            'was_correct': scan.was_correct,
            'confirmation_time_ms': scan.confirmation_time_ms,
            'processing_time_ms': scan.processing_time_ms,
            'num_candidates': scan.num_candidates,
            'error_message': scan.error_message,
            'error_type': scan.error_type,
            'created_at': scan.created_at.isoformat() if scan.created_at else None,
            'confirmed_at': scan.confirmed_at.isoformat() if scan.confirmed_at else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get scan: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get scan")
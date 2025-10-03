"""
Scan API endpoints - Main building identification flow
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import uuid
import logging
import time

from models.database import Scan
from models.session import get_db
from services import geospatial, reference_images, clip_matcher
from utils.storage import upload_image
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


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

        # === STEP 1: Upload user photo ===
        photo_start = time.time()
        photo_bytes = await photo.read()
        user_photo_url = await upload_image(
            photo_bytes,
            f"scans/{scan_id}.jpg",
            create_thumbnail=True
        )
        upload_time_ms = int((time.time() - photo_start) * 1000)
        logger.info(f"[{scan_id}] Photo uploaded in {upload_time_ms}ms")

        # === STEP 2: Geospatial filtering ===
        geo_start = time.time()
        candidates = await geospatial.get_candidate_buildings(
            db, gps_lat, gps_lng, compass_bearing, phone_pitch
        )
        geo_time_ms = int((time.time() - geo_start) * 1000)
        logger.info(f"[{scan_id}] Found {len(candidates)} candidates in {geo_time_ms}ms")

        if len(candidates) == 0:
            return JSONResponse(
                status_code=200,
                content={
                    'scan_id': scan_id,
                    'error': 'no_candidates',
                    'message': 'No buildings found in your view. Try getting closer or adjusting your angle.',
                    'matches': [],
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

        if len(reference_imgs) == 0:
            logger.warning(f"[{scan_id}] No reference images available")
            return JSONResponse(
                status_code=200,
                content={
                    'scan_id': scan_id,
                    'error': 'no_reference_images',
                    'message': 'No reference images available for these buildings. Our database is still growing!',
                    'candidates': candidates[:3],
                    'processing_time_ms': int((datetime.now() - start_time).total_seconds() * 1000)
                }
            )

        # === STEP 4: CLIP comparison ===
        clip_start = time.time()
        matches = await clip_matcher.compare_images(
            user_photo_url, candidates, reference_imgs
        )
        clip_time_ms = int((time.time() - clip_start) * 1000)
        logger.info(f"[{scan_id}] CLIP comparison completed in {clip_time_ms}ms")

        # === STEP 5: Store scan for analytics ===
        total_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        # TODO: Store in database
        # scan = Scan(
        #     id=scan_id,
        #     user_id=user_id,
        #     user_photo_url=user_photo_url,
        #     gps_lat=gps_lat,
        #     gps_lng=gps_lng,
        #     gps_accuracy=gps_accuracy,
        #     compass_bearing=compass_bearing,
        #     phone_pitch=phone_pitch,
        #     phone_roll=phone_roll,
        #     candidate_bbls=[m['bbl'] for m in matches[:5]],
        #     top_match_bbl=matches[0]['bbl'] if matches else None,
        #     top_confidence=matches[0]['confidence'] if matches else 0,
        #     processing_time_ms=total_time_ms,
        #     num_candidates=len(candidates),
        #     geospatial_query_ms=geo_time_ms,
        #     image_fetch_ms=ref_time_ms,
        #     clip_comparison_ms=clip_time_ms,
        #     created_at=datetime.utcnow()
        # )
        # db.add(scan)
        # await db.commit()

        # Determine if we should show picker UI
        show_picker = True
        if matches and matches[0]['confidence'] >= settings.confidence_threshold:
            show_picker = False

        logger.info(f"[{scan_id}] Scan completed in {total_time_ms}ms")

        return {
            'scan_id': scan_id,
            'matches': matches[:3],  # Return top 3
            'show_picker': show_picker,
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

        # TODO: Store error in database
        # scan = Scan(
        #     id=scan_id,
        #     error_message=str(e),
        #     error_type=type(e).__name__,
        #     gps_lat=gps_lat,
        #     gps_lng=gps_lng,
        #     compass_bearing=compass_bearing,
        #     created_at=datetime.utcnow()
        # )
        # db.add(scan)
        # await db.commit()

        raise HTTPException(
            status_code=500,
            detail=f"Scan failed: {str(e)}" if settings.debug else "Scan failed"
        )


@router.post("/scans/{scan_id}/confirm")
async def confirm_building(
    scan_id: str,
    confirmed_bbl: str = Form(..., description="BBL of confirmed building"),
    confirmation_time_ms: int = Form(None, description="Time taken to confirm (ms)"),
    # db: AsyncSession = Depends(get_db)
):
    """
    User confirms which building they scanned
    Used for accuracy tracking and model improvement
    """
    try:
        logger.info(f"[{scan_id}] User confirmed BBL: {confirmed_bbl}")

        # TODO: Update scan record
        # result = await db.execute(
        #     update(Scan)
        #     .where(Scan.id == scan_id)
        #     .values(
        #         confirmed_bbl=confirmed_bbl,
        #         confirmed_at=datetime.utcnow(),
        #         confirmation_time_ms=confirmation_time_ms,
        #         was_correct=(Scan.top_match_bbl == confirmed_bbl)
        #     )
        # )
        # await db.commit()

        return {
            'status': 'confirmed',
            'scan_id': scan_id,
            'confirmed_bbl': confirmed_bbl
        }

    except Exception as e:
        logger.error(f"Failed to confirm scan: {e}")
        raise HTTPException(status_code=500, detail="Failed to confirm scan")


@router.post("/scans/{scan_id}/feedback")
async def submit_feedback(
    scan_id: str,
    rating: int = Form(..., ge=1, le=5, description="Rating 1-5"),
    feedback_text: str = Form(None, description="Optional feedback text"),
    feedback_type: str = Form(None, description="Feedback type: correct/incorrect/slow/no_match"),
    # db: AsyncSession = Depends(get_db)
):
    """
    Submit feedback on scan results
    """
    try:
        logger.info(f"[{scan_id}] Feedback: {rating} stars, type: {feedback_type}")

        # TODO: Store feedback in database
        # feedback = ScanFeedback(
        #     scan_id=scan_id,
        #     rating=rating,
        #     feedback_text=feedback_text,
        #     feedback_type=feedback_type,
        #     created_at=datetime.utcnow()
        # )
        # db.add(feedback)
        # await db.commit()

        return {
            'status': 'success',
            'message': 'Thank you for your feedback!'
        }

    except Exception as e:
        logger.error(f"Failed to submit feedback: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit feedback")


@router.get("/scans/{scan_id}")
async def get_scan(
    scan_id: str,
    # db: AsyncSession = Depends(get_db)
):
    """
    Get scan details by ID
    """
    try:
        # TODO: Fetch from database
        # scan = await db.get(Scan, scan_id)
        # if not scan:
        #     raise HTTPException(status_code=404, detail="Scan not found")

        return {
            'scan_id': scan_id,
            'status': 'completed',
            'message': 'Scan endpoint placeholder - database integration pending'
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get scan: {e}")
        raise HTTPException(status_code=500, detail="Failed to get scan")
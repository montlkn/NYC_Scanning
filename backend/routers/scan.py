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
import asyncio
import uuid
import logging
import time

from models.database import Scan
from models.session import get_db, AsyncSessionLocal
from models.config import get_settings
from services import geospatial, clip_disambiguation
from services.lore_generator import generate_building_lore
from services.analytics import track_scan, track_confirmation
from services.user_images import process_confirmed_scan
from services.building_contribution import reverse_geocode_google
from utils.storage import upload_image
import pipeline.match as pipeline_match
from pipeline import telemetry as pipeline_telemetry

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


def _format_match_v3(c: dict) -> dict:
    """Format a pipeline-v3 candidate for the API response."""
    return {
        "bin": c.get("bin"),
        "bbl": c.get("bbl"),
        "address": c.get("address"),
        "name": c.get("building_name") or c.get("name") or c.get("address"),
        "distance_meters": c.get("distance_meters"),
        "bearing_difference": c.get("bearing_difference"),
        "bearing_offset_deg": c.get("bearing_offset_deg"),
        # Calibrated confidence as 0-100 integer % for the client
        "confidence": round((c.get("confidence") or 0.0) * 100, 1),
        "score_breakdown": c.get("score_breakdown"),
        "thumbnail_url": c.get("thumbnail_url"),
        "evidence": c.get("evidence", []),
        "is_landmark": c.get("is_landmark", False),
        "geocoded_lat": c.get("geocoded_lat"),
        "geocoded_lng": c.get("geocoded_lng"),
        "architect": c.get("architect"),
        "style": c.get("style"),
        "year_built": c.get("year_built"),
        "use": c.get("use"),
        "materials": c.get("materials"),
        "storytelling": c.get("storytelling"),
        "primary_aesthetic": c.get("primary_aesthetic"),
        "secondary_aesthetic": c.get("secondary_aesthetic"),
        "normalized_profile": c.get("normalized_profile"),
        "clip_similarity": c.get("clip_similarity"),
        "footprint_geojson": c.get("footprint_geojson"),
    }

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
    heading_accuracy: float = Form(None, description="Heading accuracy in degrees (from CLLocation.headingAccuracy)"),
    lens_type: str = Form("standard", description="Camera lens: 'standard' or 'ultrawide'"),
    user_id: str = Form(None, description="Optional user ID for tracking"),
    use_pipeline_v3: bool = Form(False, description="Use new pipeline (feature flag, default off until validated)"),
    tap_x: float = Form(None, description="Normalised tap X (0..1) in image space"),
    tap_y: float = Form(None, description="Normalised tap Y (0..1) in image space"),
    tap_mask_b64: str = Form(None, description="Base64-encoded binary mask from Vision segmentation"),
    tap_mask_w: int = Form(0, description="Width of tap_mask_b64 in pixels"),
    tap_mask_h: int = Form(0, description="Height of tap_mask_b64 in pixels"),
    tap_depth_m: float = Form(None, description="ARKit sceneDepth at tap pixel (metres). Absent → flat-ground fallback."),
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
    user_photo_url = None  # Set after upload succeeds; guards error-store path

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

        if len(photo_bytes) < 100:
            raise HTTPException(status_code=400, detail="Photo data too small or empty")

        # Resize image for efficiency
        from PIL import Image
        from io import BytesIO

        try:
            image = Image.open(BytesIO(photo_bytes))
            image.verify()  # Validate image integrity
            image = Image.open(BytesIO(photo_bytes))  # Re-open after verify() consumes it
        except Exception:
            logger.warning(f"[{scan_id}] Invalid image data received ({len(photo_bytes)} bytes)")
            raise HTTPException(status_code=400, detail="Invalid image data received. Please try again.")

        max_size = 1024
        if max(image.size) > max_size:
            ratio = max_size / max(image.size)
            new_size = tuple(int(dim * ratio) for dim in image.size)
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        if image.mode != 'RGB':
            image = image.convert('RGB')

        buffer = BytesIO()
        image.save(buffer, format='JPEG', quality=85, optimize=True)
        photo_bytes = buffer.getvalue()

        # Widen cone for poor GPS or ultra-wide lens
        effective_cone = settings.cone_angle_degrees
        if gps_accuracy and gps_accuracy > 15:
            extra_gps = min(30, (gps_accuracy - 15) * 1.5)
            effective_cone += extra_gps
        if lens_type == "ultrawide":
            effective_cone += 20
        if effective_cone > settings.cone_angle_degrees:
            logger.info(
                f"[{scan_id}] Cone widened: {settings.cone_angle_degrees}° → {effective_cone:.1f}° "
                f"(gps_accuracy={gps_accuracy}, lens={lens_type})"
            )

        # === STEP 1+2: Upload photo AND footprint query in parallel ===
        if use_pipeline_v3:
            user_photo_url = await upload_image(photo_bytes, f"scans/{scan_id}.jpg", create_thumbnail=True)
            upload_time_ms = int((time.time() - photo_start) * 1000)

            pipeline_result = await pipeline_match.run(
                session=db,
                photo_bytes=photo_bytes,
                user_photo_url=user_photo_url,
                lat=gps_lat, lng=gps_lng,
                bearing=compass_bearing, pitch=phone_pitch,
                gps_accuracy_m=gps_accuracy,
                heading_accuracy_deg=heading_accuracy,
                lens_type=lens_type,
                scan_id=scan_id,
                tap_x=tap_x,
                tap_y=tap_y,
                tap_mask_b64=tap_mask_b64,
                tap_mask_w=tap_mask_w,
                tap_depth_m=tap_depth_m,
                tap_mask_h=tap_mask_h,
            )

            if pipeline_result.get("error") == "no_candidates":
                address_suggestions = await reverse_geocode_google(gps_lat, gps_lng)
                return JSONResponse(status_code=200, content={
                    "scan_id": scan_id,
                    "error": "no_candidates",
                    "message": "No buildings found. Try moving closer to a building.",
                    "matches": [], "can_contribute": True,
                    "address_suggestions": address_suggestions,
                    "verification_method": "none",
                    "processing_time_ms": pipeline_result["processing_time_ms"],
                })

            raw_matches = pipeline_result["matches"]
            show_picker = pipeline_result["show_picker"]
            verification_method = pipeline_result["verification_method"]
            total_time_ms = pipeline_result["processing_time_ms"]

            matches = [_format_match_v3(c) for c in raw_matches]

            # Skip lore generation when we're not confident which building it is.
            # Lore for a wrong building wastes a Grok call and confuses telemetry;
            # we'll regenerate after the user confirms via the map picker.
            bailing = verification_method == "no_confident_match"

            # Lore generation for top match if missing
            if not bailing and matches and not matches[0].get("storytelling"):
                top = matches[0]
                try:
                    lore = await generate_building_lore(
                        db, bin_val=top.get("bin", ""),
                        building_name=top.get("name"), address=top.get("address"),
                        year_built=top.get("year_built"), style=top.get("style"),
                        architect=top.get("architect"), materials=top.get("materials"),
                        cache_to_db=True,
                    )
                    if lore:
                        matches[0]["storytelling"] = lore
                except Exception as lore_err:
                    logger.warning(f"Lore generation failed: {lore_err}")

            top_confidence = matches[0]["confidence"] if matches else 0
            auto_confirmed_bin = None
            if matches and top_confidence >= settings.confidence_threshold * 100:
                auto_confirmed_bin = matches[0]["bin"]

            try:
                await db.rollback()
                scan_kwargs = dict(
                    id=scan_id, user_id=user_id, user_photo_url=user_photo_url,
                    gps_lat=gps_lat, gps_lng=gps_lng, gps_accuracy=gps_accuracy,
                    compass_bearing=compass_bearing, phone_pitch=phone_pitch, phone_roll=phone_roll,
                    candidate_bins=[m["bin"] for m in matches],
                    top_match_bin=matches[0]["bin"] if matches else None,
                    top_confidence=top_confidence,
                    confirmed_bin=auto_confirmed_bin,
                    confirmed_at=datetime.now(timezone.utc) if auto_confirmed_bin else None,
                    verification_method="auto_confirm" if auto_confirmed_bin else None,
                    processing_time_ms=total_time_ms, num_candidates=len(raw_matches),
                    created_at=datetime.now(timezone.utc),
                )
                try:
                    scan = Scan(**scan_kwargs)
                    db.add(scan)
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    if "verification_method" in str(e):
                        scan_kwargs.pop("verification_method", None)
                        scan = Scan(**scan_kwargs)
                        db.add(scan)
                        await db.commit()
                    else:
                        raise
                _scan_cache[scan_id] = {
                    "photo_bytes": photo_bytes, "user_photo_url": user_photo_url,
                    "gps_lat": gps_lat, "gps_lng": gps_lng, "compass_bearing": compass_bearing,
                    "phone_pitch": phone_pitch, "user_id": user_id, "timestamp": time.time(),
                }
            except Exception as e:
                logger.error(f"[{scan_id}] DB store failed: {e}")
                await db.rollback()

            # Surface tap outcome unconditionally — iOS uses it to know whether
            # the tap auto-confirmed (skip Grok wait UX, jump straight to the
            # confirmed view).
            rm = pipeline_result.get("retrieval_meta") or {}
            tap_outcome = {
                "winner_bin": rm.get("tap_winner"),
                "via": rm.get("tap_winner_via"),
                "facade_match": rm.get("tap_facade_match"),
                "prefilter": rm.get("tap_prefilter"),
            }
            return {
                "scan_id": scan_id, "matches": matches,
                "show_picker": show_picker, "can_contribute": True,
                "bail": pipeline_result.get("bail", False),
                "verification_method": verification_method,
                "tap_outcome": tap_outcome,
                "processing_time_ms": total_time_ms,
                "performance": {
                    "upload_ms": upload_time_ms,
                    "pipeline_ms": total_time_ms,
                    "clip_cost_usd": pipeline_result.get("clip_cost_usd"),
                },
                "debug_info": pipeline_result.get("retrieval_meta") if settings.debug else None,
            }

        # ── Legacy path (feature flag off) ─────────────────────────────────────
        (user_photo_url, footprint_result) = await asyncio.gather(
            upload_image(photo_bytes, f"scans/{scan_id}.jpg", create_thumbnail=True),
            geospatial.get_candidates_by_footprint(db, gps_lat, gps_lng, compass_bearing, phone_pitch,
                                                       cone_angle=effective_cone)
        )
        upload_time_ms = int((time.time() - photo_start) * 1000)
        geo_time_ms = upload_time_ms
        logger.info(f"[{scan_id}] Photo upload + footprint query in {upload_time_ms}ms (parallel)")

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
            expanded_result = await geospatial.expand_search_radius(
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

        # === STEP 4: Metadata enrichment + CLIP validation ===
        # Always run CLIP on top 3 candidates — footprint is a fast pre-filter,
        # CLIP is the final arbiter. In dense Manhattan blocks adjacent buildings
        # may overlap in the footprint cone; CLIP catches those mismatches.
        # disambiguate_candidates fetches cached embeddings first (free), then
        # falls back to on-demand Street View ($0.007/image) and caches the result.
        clip_time_ms = 0
        clip_cost = 0.0
        verification_method = f'footprint_{classification}'
        clip_start = time.time()

        logger.info(f"[{scan_id}] Running CLIP on top {min(len(candidates), 3)} candidates (always-on)...")

        async def _run_clip():
            async with AsyncSessionLocal() as clip_session:
                return await clip_disambiguation.disambiguate_candidates(
                    session=clip_session,
                    user_photo_url=user_photo_url,
                    candidates=candidates[:3],
                    user_lat=gps_lat,
                    user_lng=gps_lng,
                    user_bearing=compass_bearing
                )

        enriched_candidates, clip_result = await asyncio.gather(
            geospatial.enrich_candidates_with_metadata(db, candidates),
            _run_clip()
        )
        clip_time_ms = int((time.time() - clip_start) * 1000)

        # Merge CLIP re-ranking back onto enriched candidates
        clip_by_bin = {c['bin']: c for c in clip_result['matches']}
        candidates = []
        for c in enriched_candidates:
            if c['bin'] in clip_by_bin:
                c.update({k: v for k, v in clip_by_bin[c['bin']].items()
                           if k not in ('address', 'building_name', 'architect',
                                        'style', 'year_built', 'is_landmark', 'use', 'type', 'materials')})
            candidates.append(c)
        candidates.sort(key=lambda x: x.get('combined_score', x.get('score', 0)), reverse=True)

        clip_cost = clip_result.get('cost_usd', 0)
        clip_method = clip_result.get('method', 'unknown')
        if clip_method != 'failed':
            verification_method = f"clip_{clip_method}"
        logger.info(
            f"[{scan_id}] CLIP: method={clip_method}, "
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
                # Coordinates — Swift ScanMatch decodes these as geocoded_lat/geocoded_lng
                'geocoded_lat': c.get('centroid_lat') or c.get('geocoded_lat') or c.get('lat'),
                'geocoded_lng': c.get('centroid_lng') or c.get('geocoded_lng') or c.get('lng'),
                # BuildingInfo screen fields
                'architect': c.get('architect'),
                'style': c.get('style'),
                'year_built': c.get('year_built'),
                'use': c.get('use'),
                'type': c.get('type'),
                'materials': c.get('materials'),
                'storytelling': c.get('storytelling'),
                'primary_aesthetic': c.get('primary_aesthetic'),
                'secondary_aesthetic': c.get('secondary_aesthetic'),
                'normalized_profile': c.get('normalized_profile'),
            }

            # Add CLIP data if available
            if 'clip_similarity' in c:
                match['clip_similarity'] = c['clip_similarity']

            matches.append(match)

        # For the top match: if storytelling is missing, generate it on-the-fly
        if matches and not matches[0].get('storytelling'):
            top = matches[0]
            try:
                lore = await generate_building_lore(
                    db,
                    bin_val=top.get('bin', ''),
                    building_name=top.get('name'),
                    address=top.get('address'),
                    year_built=top.get('year_built'),
                    style=top.get('style'),
                    architect=top.get('architect'),
                    materials=top.get('materials'),
                    cache_to_db=True
                )
                if lore:
                    matches[0]['storytelling'] = lore
            except Exception as lore_err:
                logger.warning(f"Lore generation failed: {lore_err}")

        # Auto-confirm high-confidence scans so they appear in passport immediately.
        # The photo-save banner is only for adding training embeddings — it should
        # not gate whether a scan shows up in the user's history.
        auto_confirmed_bin = None
        if matches and top_confidence >= settings.confidence_threshold * 100:
            auto_confirmed_bin = matches[0]['bin']
            logger.info(f"[{scan_id}] Auto-confirming BIN {auto_confirmed_bin} (confidence {top_confidence:.1f}%)")

        # Store scan in database — rollback first in case any earlier
        # sub-operation (e.g. cache_embedding) left the session in a bad state
        try:
            await db.rollback()
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
                confirmed_bin=auto_confirmed_bin,
                confirmed_at=datetime.now(timezone.utc) if auto_confirmed_bin else None,
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

        # Store error in database (only if we have a photo URL — required NOT NULL)
        if user_photo_url:
            try:
                await db.rollback()  # Clear any aborted transaction state first
                scan = Scan(
                    id=scan_id,
                    user_id=user_id,
                    user_photo_url=user_photo_url,
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
    verification_method: str = Form(
        "photo_banner",
        description="How the confirmation happened: map_picker | list_picker | photo_banner | auto_confirm",
    ),
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

        # Update scan record. verification_method is captured as gold-quality
        # signal for the flywheel — map_picker rows are user-tap-accurate ground
        # truth and seed the per-NYC fine-tune dataset. Tolerates the column
        # not existing yet (during the rollout window before the ALTER TABLE).
        update_values = {
            "confirmed_bin": confirmed_bin,
            "confirmed_at": datetime.now(timezone.utc),
            "confirmation_time_ms": confirmation_time_ms,
            "was_correct": was_correct,
            "verification_method": verification_method,
        }
        try:
            await db.execute(
                update(Scan).where(Scan.id == scan_id).values(**update_values)
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            if "verification_method" in str(e):
                update_values.pop("verification_method", None)
                await db.execute(
                    update(Scan).where(Scan.id == scan_id).values(**update_values)
                )
                await db.commit()
                logger.warning(
                    f"[{scan_id}] scans.verification_method column missing — "
                    "wrote confirmation without it. Run the Phase 7 ALTER TABLE."
                )
            else:
                raise

        # Track confirmation + pipeline telemetry
        pipeline_telemetry.log_confirmation(
            scan_id=scan_id,
            top3_bins=list(scan.candidate_bins[:3]) if scan.candidate_bins else [],
            confirmed_bin=confirmed_bin,
        )

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

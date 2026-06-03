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
from sqlalchemy import update, select, text
from datetime import datetime, timezone
from typing import Optional
import asyncio
import uuid
import logging
import time
import re
import json

from models.database import Scan
from models.session import get_db, AsyncSessionLocal
from models.config import get_settings
from services import geospatial
from services.lore_generator import generate_building_lore
from services.analytics import track_scan, track_confirmation
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
    nearest_poi: str = Form(None, description="Nearest MapKit POI name within 30m, if found. Strong landmark prior."),
    gps_source: str = Form(None, description="GPS pose source: arkit_geo, live, smoothed, etc."),
    ocr_hints: str = Form(None, description="JSON array of strings extracted from the scan image by on-device Apple Vision OCR. Used to widen retrieval when compass is unreliable."),
    tap_ray_origin_lat: float = Form(None, description="ARKit-geo lat of camera at shutter. Together with hit_lat/lng forms a compass-free ray we intersect against footprints."),
    tap_ray_origin_lng: float = Form(None, description="ARKit-geo lng of camera at shutter."),
    tap_ray_hit_lat: float = Form(None, description="ARKit-geo lat of raycast hit point at the tapped pixel."),
    tap_ray_hit_lng: float = Form(None, description="ARKit-geo lng of raycast hit point at the tapped pixel."),
    tap_ray_distance_m: float = Form(None, description="Camera-to-hit distance in metres. Informational; backend extends the ray past the hit anyway."),
    ocr_poi_name: str = Form(None, description="Name of a MapKit POI matched against OCR text on iOS (e.g. 'Pret A Manger'). Informational; backend uses the lat/lng for footprint containment."),
    ocr_poi_lat: float = Form(None, description="Lat of the matched POI's pin. ST_Contains against building_footprints identifies the host building."),
    ocr_poi_lng: float = Form(None, description="Lng of the matched POI's pin."),
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
            f"bearing {compass_bearing:.1f}°, pitch {phone_pitch:.1f}°, "
            f"gps_acc={gps_accuracy}m, heading_acc={heading_accuracy}°"
        )

        # === POSE GATE ===
        # Reject scans where the user is clearly not aimed at a building.
        # 0° = phone upright pointing at building. -90° = lens at ground. +90° = lens at sky.
        # Outside |pitch| <= 60° we have no directional signal worth widening for.
        if abs(phone_pitch) > 60:
            logger.info(f"[{scan_id}] pose_rejected: pitch {phone_pitch:.1f}° outside ±60°")
            return JSONResponse(status_code=200, content={
                "scan_id": scan_id,
                "error": "pose_rejected",
                "reason": "bad_pitch",
                "message": "Hold the phone upright and point it at the building.",
                "matches": [],
                "verification_method": "pose_rejected",
                "processing_time_ms": int((datetime.now() - start_time).total_seconds() * 1000),
            })
        if gps_accuracy is not None and gps_accuracy > 30:
            logger.info(f"[{scan_id}] pose_rejected: gps_accuracy {gps_accuracy}m > 30m")
            return JSONResponse(status_code=200, content={
                "scan_id": scan_id,
                "error": "pose_rejected",
                "reason": "bad_gps",
                "message": "GPS signal is too weak. Step outside or wait a moment.",
                "matches": [],
                "verification_method": "pose_rejected",
                "processing_time_ms": int((datetime.now() - start_time).total_seconds() * 1000),
            })

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

        # Widen cone for poor GPS or ultra-wide lens. Hard cap at 110° —
        # anything wider is no longer a "cone," it's a fan that ranks by
        # noise. The pose gate above ensures pitch/GPS are good enough that
        # we don't need to fall back to a 156° city-block sweep.
        effective_cone = settings.cone_angle_degrees
        if gps_accuracy and gps_accuracy > 15:
            extra_gps = min(30, (gps_accuracy - 15) * 1.5)
            effective_cone += extra_gps
        if lens_type == "ultrawide":
            effective_cone += 20
        effective_cone = min(effective_cone, 110.0)
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
                nearest_poi=nearest_poi,
                gps_source=gps_source,
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

            # Drop candidates the client can't render at all: a label must
            # exist. Coords are nice-to-have but the map picker falls back
            # to footprint centroid when geocoded coords are missing.
            def _renderable(c):
                # Reject labels that are just "BIN <number>" — those are our
                # own ugly fallback, not real data. Also reject pure-digit
                # labels (raw BINs in the name column).
                label = (c.get("building_name") or c.get("name") or c.get("address") or "").strip()
                if not label:
                    return False
                lab = label.upper()
                if lab.startswith("BIN "):
                    # Fall through: maybe we have address too.
                    return bool((c.get("address") or "").strip()) and not (c.get("address") or "").strip().upper().startswith("BIN ")
                if label.replace(".", "").isdigit():
                    return False
                return True
            before = len(raw_matches)
            raw_matches = [c for c in raw_matches if _renderable(c)]
            if before != len(raw_matches):
                logger.info(f"[{scan_id}] renderable filter dropped {before - len(raw_matches)} of {before}")

            # === TAP RAY PROMOTION ===
            # ARKit handed us two geo-anchored points (camera origin + raycast
            # hit). Draw a line between them in plan view, intersect against
            # footprints, take the building closest to the origin.
            #
            # Confidence policy is deliberately conservative — the ray CAN be
            # wrong (mis-localized VPS, stale anchor after walking, glass
            # facade reflections). Tiered so the ray never auto-confirms
            # unless geometry OR OCR agrees:
            #   - In cone top-3:           0.90 (geometry + ray agree)
            #   - In cone (not top-3):     0.80
            #   - Not in cone at all:      0.65 (below picker threshold —
            #                                    picker will fire so user
            #                                    can sanity check)
            #   - Ray + OCR address match: 0.95 (two independent signals)
            # Also: VPS sanity-gate — if ARKit's origin is >50m from raw GPS,
            # the visual lock is suspect, skip the ray entirely.
            ray_winner_bin: Optional[str] = None
            ray_sanity_ok = True
            if (
                tap_ray_origin_lat is not None and tap_ray_origin_lng is not None
                and tap_ray_hit_lat is not None and tap_ray_hit_lng is not None
            ):
                # VPS sanity check: if ARKit's reported origin is >50m from
                # raw GPS, the visual lock is probably wrong (or stale after
                # walking). Distrust the ray entirely in that case rather than
                # confidently picking the wrong building.
                from math import radians, sin, cos, asin, sqrt
                dlat = radians(tap_ray_origin_lat - gps_lat)
                dlng = radians(tap_ray_origin_lng - gps_lng)
                a = sin(dlat/2)**2 + cos(radians(gps_lat)) * cos(radians(tap_ray_origin_lat)) * sin(dlng/2)**2
                origin_gps_drift_m = 2 * 6371000 * asin(sqrt(a))
                if origin_gps_drift_m > 50:
                    ray_sanity_ok = False
                    logger.info(
                        f"[{scan_id}] tap_ray: ignored — ARKit origin {origin_gps_drift_m:.1f}m "
                        f"from raw GPS, VPS likely mis-localized"
                    )

                if ray_sanity_ok:
                    ray_hit = await geospatial.find_building_by_ray(
                        origin_lat=tap_ray_origin_lat, origin_lng=tap_ray_origin_lng,
                        hit_lat=tap_ray_hit_lat, hit_lng=tap_ray_hit_lng,
                    )
                    if ray_hit and ray_hit.get("bin"):
                        ray_winner_bin = ray_hit["bin"]
                        # Cone top-3 = the geometry-trusted set we cross-check against.
                        top3_bins = {c.get("bin") for c in raw_matches[:3] if c.get("bin")}
                        in_top3 = ray_winner_bin in top3_bins
                        existing = next((c for c in raw_matches if c.get("bin") == ray_winner_bin), None)
                        if existing:
                            # Both signals point here — confident but not gospel.
                            existing["confidence"] = 0.90 if in_top3 else 0.80
                            existing["tap_ray_winner"] = True
                            raw_matches = [existing] + [c for c in raw_matches if c is not existing]
                            logger.info(
                                f"[{scan_id}] tap_ray: promoted BIN {ray_winner_bin} "
                                f"(in_top3={in_top3}, conf={existing['confidence']})"
                            )
                        else:
                            # Geometry didn't surface this building. Inject at
                            # 0.65 — deliberately below the 0.70 auto-confirm
                            # floor so the picker always fires when the ray
                            # disagrees with the cone. User gets to pick
                            # between the ray-suggested building and whatever
                            # the cone surfaced, which is the right UX when
                            # VPS might be subtly wrong.
                            try:
                                hydrate = await db.execute(text("""
                                    SELECT REPLACE(bin, '.0', '') AS bin, bbl, building_name, address,
                                           geocoded_lat, geocoded_lng, architect, style, year_built,
                                           mat_prim
                                    FROM buildings_full_merge_scanning
                                    WHERE REPLACE(bin, '.0', '') = :bin LIMIT 1
                                """), {"bin": ray_winner_bin})
                                row = hydrate.fetchone()
                            except Exception as e:
                                logger.warning(f"[{scan_id}] tap_ray hydrate failed: {e}")
                                await db.rollback()
                                row = None
                            if row:
                                ray_candidate = {
                                    "bin": row.bin, "bbl": str(row.bbl).replace(".0", "") if row.bbl else None,
                                    "building_name": row.building_name, "name": row.building_name or row.address,
                                    "address": row.address,
                                    "geocoded_lat": float(row.geocoded_lat) if row.geocoded_lat else None,
                                    "geocoded_lng": float(row.geocoded_lng) if row.geocoded_lng else None,
                                    "architect": row.architect, "style": row.style,
                                    "year_built": row.year_built,
                                    "materials": row.mat_prim,
                                    "footprint_geojson": ray_hit.get("footprint_geojson"),
                                    "distance_meters": ray_hit.get("distance_to_origin_m"),
                                    "confidence": 0.65,
                                    "tap_ray_winner": True,
                                }
                                raw_matches = [ray_candidate] + raw_matches
                                logger.info(
                                    f"[{scan_id}] tap_ray: injected BIN {ray_winner_bin} "
                                    f"(not in cone, conf=0.65 — picker will fire)"
                                )
                            else:
                                logger.info(f"[{scan_id}] tap_ray: hit BIN {ray_winner_bin} but no metadata in buildings DB")

            # === TENANT POI PROMOTION ===
            # iOS resolved an OCR phrase (e.g. "PRET A MANGER") against
            # MapKit and got back a POI coordinate. We find the building
            # whose footprint contains that point. Tenant signage is far
            # more readable than address plaques in NYC photos, so this
            # is the most reliable signal short of a direct address match.
            #
            # Confidence tiers — never auto-confirms alone:
            #   - 0.90 if POI building == tap_ray winner (two signals agree)
            #   - 0.90 if POI building is in cone top-3 (geometry agrees)
            #   - 0.80 otherwise (picker may fire; user disambiguates)
            poi_winner_bin: Optional[str] = None
            if ocr_poi_lat is not None and ocr_poi_lng is not None:
                poi_hit = await geospatial.find_building_containing_point(
                    lat=ocr_poi_lat, lng=ocr_poi_lng,
                )
                if poi_hit and poi_hit.get("bin"):
                    poi_winner_bin = poi_hit["bin"]
                    agrees_with_ray = (ray_winner_bin is not None and ray_winner_bin == poi_winner_bin)
                    top3_bins = {c.get("bin") for c in raw_matches[:3] if c.get("bin")}
                    in_cone = poi_winner_bin in top3_bins
                    target_conf = 0.90 if (agrees_with_ray or in_cone) else 0.80

                    existing = next((c for c in raw_matches if c.get("bin") == poi_winner_bin), None)
                    if existing:
                        # Only bump, never lower — a ray-promoted candidate
                        # at 0.95 shouldn't get demoted by a POI lookup.
                        existing["confidence"] = max(existing.get("confidence") or 0.0, target_conf)
                        existing["ocr_poi_winner"] = True
                        existing["ocr_poi_name"] = ocr_poi_name
                        # Move to front if not already.
                        raw_matches = [existing] + [c for c in raw_matches if c is not existing]
                        logger.info(
                            f"[{scan_id}] ocr_poi: promoted BIN {poi_winner_bin} "
                            f"({ocr_poi_name!r}, agrees_ray={agrees_with_ray}, in_cone={in_cone}, conf={existing['confidence']})"
                        )
                    else:
                        try:
                            hydrate = await db.execute(text("""
                                SELECT REPLACE(bin, '.0', '') AS bin, bbl, building_name, address,
                                       geocoded_lat, geocoded_lng, architect, style, year_built,
                                       mat_prim
                                FROM buildings_full_merge_scanning
                                WHERE REPLACE(bin, '.0', '') = :bin LIMIT 1
                            """), {"bin": poi_winner_bin})
                            row = hydrate.fetchone()
                        except Exception as e:
                            logger.warning(f"[{scan_id}] ocr_poi hydrate failed: {e}")
                            await db.rollback()
                            row = None
                        if row:
                            poi_candidate = {
                                "bin": row.bin, "bbl": str(row.bbl).replace(".0", "") if row.bbl else None,
                                "building_name": row.building_name, "name": row.building_name or row.address,
                                "address": row.address,
                                "geocoded_lat": float(row.geocoded_lat) if row.geocoded_lat else None,
                                "geocoded_lng": float(row.geocoded_lng) if row.geocoded_lng else None,
                                "architect": row.architect, "style": row.style,
                                "year_built": row.year_built,
                                "materials": row.mat_prim,
                                "footprint_geojson": poi_hit.get("footprint_geojson"),
                                "confidence": target_conf,
                                "ocr_poi_winner": True,
                                "ocr_poi_name": ocr_poi_name,
                            }
                            raw_matches = [poi_candidate] + raw_matches
                            logger.info(
                                f"[{scan_id}] ocr_poi: injected BIN {poi_winner_bin} "
                                f"({ocr_poi_name!r}, conf={target_conf})"
                            )
                        else:
                            logger.info(
                                f"[{scan_id}] ocr_poi: hit BIN {poi_winner_bin} ({ocr_poi_name!r}) "
                                f"but no metadata in buildings DB"
                            )

            # === OCR + GEOMETRY MERGE ===
            # Both signals run. The photo is ground truth; the compass is
            # the unreliable layer. So when OCR and geometry agree, that's
            # a strong confirm — boost the agreeing candidate to the top.
            # When they disagree, OCR wins because it's reading the actual
            # facade. When OCR returns nothing useful, geometry stands.
            ocr_tokens_num: list = []
            ocr_tokens_name: list = []
            if ocr_hints:
                try:
                    hints_list = json.loads(ocr_hints) if isinstance(ocr_hints, str) else list(ocr_hints)
                except Exception:
                    hints_list = []
                joined = " ".join(str(s) for s in hints_list if s)
                ocr_tokens_num = list({m for m in re.findall(r"\b\d{1,5}\b", joined)})
                # Building/landmark names: alpha-heavy tokens, length 4+.
                # Skip generic words so we don't widen for "ENTER" or "OPEN".
                STOP = {"THE","AVE","STREET","ROAD","BLVD","BANK","HOTEL","OPEN","ENTER","EXIT","CLOSED","PARK","WEST","EAST","NORTH","SOUTH"}
                ocr_tokens_name = list({
                    w.upper() for w in re.findall(r"[A-Za-z]{4,}", joined)
                    if w.upper() not in STOP
                })[:5]

            widened: list = []
            if ocr_tokens_num or ocr_tokens_name:
                widened = await geospatial.find_by_address_tokens(
                    db, gps_lat, gps_lng,
                    radius_m=500,
                    number_tokens=ocr_tokens_num,
                    name_tokens=ocr_tokens_name,
                    limit=10,
                )

            top_conf_before = (raw_matches[0].get("confidence") or 0.0) if raw_matches else 0.0
            compass_bad = heading_accuracy is None or float(heading_accuracy) > 20
            ocr_action = "none"

            if widened:
                geo_bins = {c.get("bin") for c in raw_matches if c.get("bin")}
                # Agreement: OCR found something that geometry also has.
                agreeing = [w for w in widened if w.get("bin") in geo_bins]
                # Disagreement: OCR found something(s) geometry didn't have.
                new_only = [w for w in widened if w.get("bin") not in geo_bins]

                if agreeing:
                    # Pull the agreed BIN(s) to the front of raw_matches.
                    agreed_bins = [w["bin"] for w in agreeing]
                    promoted = [c for c in raw_matches if c.get("bin") in agreed_bins]
                    rest = [c for c in raw_matches if c.get("bin") not in agreed_bins]
                    for c in promoted:
                        c["ocr_match"] = True
                        # Bump confidence to reflect dual-signal agreement.
                        c["confidence"] = max(c.get("confidence") or 0.0, 0.85)
                    raw_matches = promoted + rest
                    ocr_action = f"agreed:{len(agreeing)}"
                elif new_only:
                    # Geometry vs OCR disagree. Trust OCR. But only inject
                    # the top OCR candidate(s) — not all 10 — to avoid
                    # spamming the picker with every "101" in midtown.
                    # When OCR returns just 1-2, we're confident; when it
                    # returns many (e.g. "101 E 38th, 101 E 39th, 101 Park"
                    # — all match "101"), still inject all but with the
                    # closest first so the user sees ranked options.
                    inject = new_only[: min(3, len(new_only))]
                    for c in inject:
                        c["confidence"] = 0.75 if len(inject) == 1 else 0.6
                    raw_matches = inject + raw_matches
                    ocr_action = f"override:{len(inject)}"

            # === Ray + OCR cross-validation ===
            # When the ray-picked building's address starts with one of the
            # OCR number tokens, both independent signals agree — bump to
            # 0.95. This is the only path where the ray alone gets a near-
            # auto-confirm score, because the OCR address match is direct
            # evidence the user is looking at *that* address.
            if ray_winner_bin and ocr_tokens_num and raw_matches and raw_matches[0].get("bin") == ray_winner_bin:
                addr = (raw_matches[0].get("address") or "").strip()
                if addr and any(addr.startswith(num + " ") or addr.startswith(num + "-") for num in ocr_tokens_num):
                    raw_matches[0]["confidence"] = 0.95
                    raw_matches[0]["tap_ray_ocr_agreement"] = True
                    logger.info(f"[{scan_id}] tap_ray + OCR agree on BIN {ray_winner_bin} — confidence 0.95")

            # Structured one-liner for log-grep + dashboarding. Keep keys stable.
            compass_state = (
                "unknown" if heading_accuracy is None
                else ("stale" if float(heading_accuracy) > 20 else "ok")
            )
            widened_flag = bool(widened) and ocr_action != "none"
            logger.info(
                f"[{scan_id}] scan_compass_quality compass={compass_state} "
                f"heading_acc={heading_accuracy} ocr_tokens={len(ocr_tokens_num) + len(ocr_tokens_name)} "
                f"ocr_matches={len(widened)} widened={str(widened_flag).lower()} "
                f"action={ocr_action} conf_before={top_conf_before:.2f} "
                f"tap_ray_winner={ray_winner_bin or 'none'} "
                f"poi_winner={poi_winner_bin or 'none'}"
            )

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

        # === STEP 4: Metadata enrichment (CLIP fully bypassed) ===
        # CLIP was a slow, expensive, frequently-wrong final arbiter that pulled
        # Street View images and ran image embeddings. We now rank purely on
        # footprint geometry + bearing + tap overlap; the VLM (Grok) handles
        # disambiguation when the picker fires.
        clip_time_ms = 0
        clip_cost = 0.0
        verification_method = f'footprint_{classification}'

        enriched_candidates = await geospatial.enrich_candidates_with_metadata(db, candidates)
        candidates = enriched_candidates
        candidates.sort(key=lambda x: x.get('combined_score', x.get('score', 0)), reverse=True)
        logger.info(f"[{scan_id}] CLIP bypassed; ranking by footprint only")

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

        # Photo already lives in R2 from the initial scan upload (see scans/{scan_id}.jpg).
        # A separate embeddings job will read R2 and write reference_embeddings.
        if scan_id in _scan_cache:
            del _scan_cache[scan_id]

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
        if was_in_top_3:
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
            'embedding_generated': False,
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

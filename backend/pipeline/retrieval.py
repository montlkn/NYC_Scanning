"""
Retrieval — geospatial candidate generation.

Wraps the PostGIS footprint query with:
- Adaptive cone (scales with GPS accuracy, heading accuracy, lens type)
- Two-pass ring fallback (when cone returns too few or low-confidence candidates)
- Ground-plane plausibility term

All geometry decisions live here; scoring lives in scoring.py.
"""

import math
import logging
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.footprints_session import get_footprints_db
from pipeline.config import get_pipeline_config

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()


def adaptive_cone(
    gps_accuracy_m: Optional[float],
    heading_accuracy_deg: Optional[float],
    lens_type: str
) -> float:
    """
    Compute cone half-angle based on measured sensor uncertainty.
    Wider when GPS/heading is poor; tighter when sensors are good.
    """
    cone = _cfg.base_cone_deg

    if gps_accuracy_m and gps_accuracy_m > _cfg.cone_gps_threshold_m:
        cone += min(40, (gps_accuracy_m - _cfg.cone_gps_threshold_m) * _cfg.cone_gps_scale)

    if heading_accuracy_deg and heading_accuracy_deg > 10:
        cone += min(30, heading_accuracy_deg * _cfg.cone_heading_scale)

    if lens_type == "ultrawide":
        cone += _cfg.cone_ultrawide_bonus

    return min(_cfg.cone_ceiling_deg, max(_cfg.cone_floor_deg, cone))


async def cone_query(
    footprints_db: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    cone_deg: float,
    max_distance_m: float,
    max_candidates: int
) -> List[Dict[str, Any]]:
    """
    Run find_buildings_in_cone AND a proximity fallback.

    The footprint-intersection cone can silently exclude the correct building
    when GPS drifts a few metres (apex moves, footprint misses the cone edge).
    The proximity fallback adds any building whose centroid is within 50m and
    whose centroid bearing is within the cone — these are almost certainly
    visible to the user regardless of exact footprint overlap.
    """
    try:
        # Primary: footprint-intersection cone
        result = await footprints_db.execute(
            text("""
                SELECT
                    bin, bbl, name,
                    distance_meters, bearing_to_building, bearing_difference,
                    visible_area, shape_area, height_roof, visibility_score
                FROM find_buildings_in_cone(
                    :lat, :lng, :bearing, :max_distance, :cone_angle, :max_candidates
                )
            """),
            {
                "lat": lat, "lng": lng, "bearing": bearing,
                "max_distance": max_distance_m,
                "cone_angle": cone_deg,
                "max_candidates": max_candidates,
            }
        )
        rows = result.fetchall()
        primary = [_row_to_candidate(r) for r in rows]

        # Proximity fallback: centroid-bearing check for close buildings the cone may have missed
        prox_result = await footprints_db.execute(
            text("""
                WITH user_pt AS (
                    SELECT ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS geom
                )
                SELECT
                    bf.bin, bf.bbl, bf.name,
                    ST_Distance(bf.centroid::geography, u.geom::geography) AS dist_m,
                    DEGREES(ST_Azimuth(u.geom, bf.centroid)) AS bearing_to_bldg,
                    ABS(
                        MOD(
                            (DEGREES(ST_Azimuth(u.geom, bf.centroid)) - :bearing + 180 + 360)::numeric,
                            360::numeric
                        )::double precision - 180
                    ) AS bearing_diff,
                    bf.shape_area,
                    bf.shape_area AS visible_area,
                    bf.height_roof,
                    0.5 AS visibility_score
                FROM building_footprints bf, user_pt u
                WHERE
                    ST_Distance(bf.centroid::geography, u.geom::geography) < 50
                    AND ABS(
                        MOD(
                            (DEGREES(ST_Azimuth(u.geom, bf.centroid)) - :bearing + 180 + 360)::numeric,
                            360::numeric
                        )::double precision - 180
                    ) < (:cone_angle / 2.0)
                ORDER BY dist_m ASC
                LIMIT :max_candidates
            """),
            {"lat": lat, "lng": lng, "bearing": bearing,
             "cone_angle": cone_deg, "max_candidates": max_candidates}
        )
        prox_rows = prox_result.fetchall()
        prox = [_row_to_candidate(r) for r in prox_rows]

        # Merge: proximity candidates not already in primary set
        primary_bins = {c["bin"] for c in primary}
        new_from_prox = [c for c in prox if c["bin"] not in primary_bins]
        if new_from_prox:
            logger.info(f"Proximity fallback added {len(new_from_prox)} candidates inside 50m")

        return primary + new_from_prox

    except Exception as e:
        logger.error(f"Cone query failed: {e}", exc_info=True)
        return []


async def ring_query(
    footprints_db: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    max_distance_m: float,
    max_candidates: int
) -> List[Dict[str, Any]]:
    """Full-ring fallback: ignore bearing, return all buildings within radius."""
    try:
        result = await footprints_db.execute(
            text("""
                SELECT
                    bin, bbl, name,
                    distance_meters, bearing_to_building, bearing_difference,
                    visible_area, shape_area, height_roof, visibility_score
                FROM find_buildings_in_cone(
                    :lat, :lng, :bearing, :max_distance, 180, :max_candidates
                )
            """),
            {
                "lat": lat, "lng": lng, "bearing": bearing,
                "max_distance": max_distance_m,
                "max_candidates": max_candidates,
            }
        )
        rows = result.fetchall()
        return [_row_to_candidate(r) for r in rows]
    except Exception as e:
        logger.error(f"Ring fallback query failed: {e}", exc_info=True)
        return []


def ground_plane_score(
    candidate: Dict[str, Any],
    phone_pitch: float,
    user_bearing: float
) -> float:
    """
    When phone is near horizontal (|pitch| < 25°), the user is shooting a facade
    across the street.  Buildings whose centroid is roughly opposite the user's
    bearing get a small boost; those behind the user get a penalty.

    Returns [0, 1] — 0.5 is neutral.
    """
    bearing_diff = candidate.get("bearing_difference")
    if bearing_diff is None:
        return 0.5

    abs_diff = abs(bearing_diff)

    if abs(phone_pitch) > 40:
        # Looking up or down — bearing matters less; don't apply ground-plane bias
        return 0.5

    # Smooth falloff: facing = 1.0, 90° off = 0.5, 180° (behind) = 0.0
    score = 1.0 - (abs_diff / 180.0)
    return float(max(0.0, min(1.0, score)))


def _row_to_candidate(row) -> Dict[str, Any]:
    return {
        "bin": str(row[0]).replace(".0", "") if row[0] else None,
        "bbl": str(row[1]).replace(".0", "") if row[1] else None,
        "name": row[2],
        "distance_meters": round(row[3], 2) if row[3] else None,
        "bearing_to_building": round(row[4], 1) if row[4] else None,
        "bearing_difference": round(row[5], 1) if row[5] else None,
        "visible_area": round(row[6], 2) if row[6] else None,
        "shape_area": round(row[7], 2) if row[7] else None,
        "height_roof": round(row[8], 1) if row[8] else None,
        "footprint_score": round(float(row[9]), 2) if row[9] else 0.0,
    }


async def ring_query_direct(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    max_distance_m: float,
    max_candidates: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Full-ring query callable directly from match.py without going through
    the two-pass wrapper. Used when we've already scored cone candidates and
    decided the ring is needed.
    """
    async with get_footprints_db() as footprints_db:
        if footprints_db is None:
            return [], {}
        candidates = await ring_query(footprints_db, lat, lng, bearing, max_distance_m, max_candidates)
    return candidates, {}


async def get_candidates(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    pitch: float,
    gps_accuracy_m: Optional[float],
    heading_accuracy_deg: Optional[float],
    lens_type: str,
    top_clip_score: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Two-pass candidate retrieval.

    Pass 1: adaptive cone.
    Pass 2 (ring): triggered if pass-1 returns < min_candidates OR
                   top CLIP score from a previous attempt is below ring_threshold.

    Returns (candidates, retrieval_meta) where meta carries diagnostics.
    """
    cone_deg = adaptive_cone(gps_accuracy_m, heading_accuracy_deg, lens_type)
    meta = {"cone_deg": round(cone_deg, 1), "used_ring_fallback": False}

    async with get_footprints_db() as footprints_db:
        if footprints_db is None:
            logger.warning("Footprints DB unavailable")
            return [], meta

        candidates = await cone_query(
            footprints_db, lat, lng, bearing, cone_deg,
            _cfg.max_distance_m, _cfg.max_candidates
        )
        meta["pass1_count"] = len(candidates)
        logger.info(f"Pass-1 cone ({cone_deg:.0f}°): {len(candidates)} candidates")

        # Trigger ring fallback?
        low_confidence = (
            top_clip_score is not None and
            top_clip_score < _cfg.ring_fallback_clip_threshold
        )
        too_few = len(candidates) < _cfg.ring_fallback_min_candidates

        if low_confidence or too_few:
            logger.info(
                f"Ring fallback triggered "
                f"(low_confidence={low_confidence}, too_few={too_few})"
            )
            ring_candidates = await ring_query(
                footprints_db, lat, lng, bearing,
                _cfg.max_distance_m, _cfg.max_candidates
            )
            # Merge: add ring candidates not already in cone results
            existing_bins = {c["bin"] for c in candidates}
            new = [c for c in ring_candidates if c["bin"] not in existing_bins]
            candidates = candidates + new
            meta["used_ring_fallback"] = True
            meta["ring_added"] = len(new)
            logger.info(f"Ring fallback added {len(new)} new candidates")

    return candidates, meta

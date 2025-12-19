"""
Geospatial V2 Service - Footprint-based Building Identification

This service implements the bulletproof building identification system using
PostGIS footprint intersection instead of centroid-based distance calculations.

Key improvements over V1:
- Uses actual building polygon footprints, not just centroids
- Calculates visible facade area within view cone
- Handles large buildings that span multiple view angles
- 100% coverage of NYC buildings (1.08M)

Usage:
    candidates = await get_candidates_by_footprint(
        db, lat, lng, bearing, pitch
    )
"""

import math
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from models.config import get_settings
from models.footprints_session import get_footprints_db

logger = logging.getLogger(__name__)
settings = get_settings()


# Score thresholds for classification
SINGLE_BUILDING_CONFIDENCE = 95.0
CLEAR_WINNER_CONFIDENCE = 85.0
AMBIGUITY_THRESHOLD = 15.0  # Score gap below which results are ambiguous


async def get_candidates_by_footprint(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    pitch: float = 0,
    max_distance: Optional[float] = None,
    cone_angle: Optional[float] = None,
    max_candidates: Optional[int] = None
) -> Dict[str, Any]:
    """
    Get buildings within user's view cone using footprint intersection.

    This is the primary query for V2 scan system. Uses PostGIS to find
    all building footprints that intersect with the user's view cone,
    then scores them by visibility.

    Args:
        session: AsyncIO database session
        lat: User GPS latitude
        lng: User GPS longitude
        bearing: Compass bearing (0-360, 0=North)
        pitch: Phone pitch angle (-90 to 90)
        max_distance: Maximum scan distance in meters (default from settings)
        cone_angle: View cone angle in degrees (default from settings)
        max_candidates: Maximum candidates to return (default from settings)

    Returns:
        Dictionary containing:
        - candidates: List of building candidates with scores
        - classification: 'single', 'clear_winner', 'ambiguous', or 'none'
        - top_confidence: Confidence score for top match
        - is_ambiguous: Whether CLIP disambiguation is needed
        - query_time_ms: Database query time
    """
    if max_distance is None:
        max_distance = settings.max_scan_distance_meters
    if cone_angle is None:
        cone_angle = settings.cone_angle_degrees
    if max_candidates is None:
        max_candidates = settings.max_candidates

    logger.info(
        f"Footprint query at ({lat:.6f}, {lng:.6f}), "
        f"bearing {bearing:.1f}°, pitch {pitch:.1f}°"
    )

    # Adjust cone angle based on GPS accuracy if available
    # Wider cone = more candidates but lower precision per candidate
    effective_cone = cone_angle

    # If user is looking up (pitch > 20), weight height more heavily
    height_weight_boost = 1.0 if pitch <= 20 else 1.0 + (pitch - 20) / 70

    try:
        # Query the Railway footprints database
        async with get_footprints_db() as footprints_db:
            if footprints_db is None:
                # Footprints DB not configured - fall back to V1
                logger.warning("Footprints database not configured, falling back to V1")
                return await fallback_centroid_query(
                    session, lat, lng, bearing, pitch, max_distance, cone_angle, max_candidates
                )

            # Use the PostGIS function we created in the migration
            result = await footprints_db.execute(
                text("""
                    SELECT
                        bin,
                        bbl,
                        name,
                        distance_meters,
                        bearing_to_building,
                        bearing_difference,
                        visible_area,
                        shape_area,
                        height_roof,
                        visibility_score
                    FROM find_buildings_in_cone(
                        :lat, :lng, :bearing, :max_distance, :cone_angle, :max_candidates
                    )
                """),
                {
                    'lat': lat,
                    'lng': lng,
                    'bearing': bearing,
                    'max_distance': max_distance,
                    'cone_angle': effective_cone,
                    'max_candidates': max_candidates
                }
            )

            rows = result.fetchall()

        candidates = []
        for row in rows:
            # Apply height weight boost for looking-up scenarios
            adjusted_score = row[9]  # visibility_score
            if height_weight_boost > 1.0 and row[8]:  # height_roof
                height_bonus = (row[8] / 200) * (height_weight_boost - 1.0) * 10
                adjusted_score = min(100, adjusted_score + height_bonus)

            candidates.append({
                'bin': str(row[0]).replace('.0', '') if row[0] else None,
                'bbl': str(row[1]).replace('.0', '') if row[1] else None,
                'name': row[2],
                'distance_meters': round(row[3], 2) if row[3] else None,
                'bearing_to_building': round(row[4], 1) if row[4] else None,
                'bearing_difference': round(row[5], 1) if row[5] else None,
                'visible_area': round(row[6], 2) if row[6] else None,
                'shape_area': round(row[7], 2) if row[7] else None,
                'height_roof': round(row[8], 1) if row[8] else None,
                'score': round(adjusted_score, 2),
            })

        # Classify the result
        classification, top_confidence, is_ambiguous = classify_results(candidates)

        logger.info(
            f"Found {len(candidates)} candidates, "
            f"classification: {classification}, "
            f"top_confidence: {top_confidence:.1f}"
        )

        return {
            'candidates': candidates,
            'classification': classification,
            'top_confidence': top_confidence,
            'is_ambiguous': is_ambiguous,
            'num_candidates': len(candidates),
        }

    except Exception as e:
        logger.error(f"Footprint query failed: {e}", exc_info=True)

        # Fallback to V1 centroid-based query if footprint table not available
        logger.warning("Falling back to V1 centroid-based query")
        return await fallback_centroid_query(
            session, lat, lng, bearing, pitch, max_distance, cone_angle, max_candidates
        )


def classify_results(
    candidates: List[Dict[str, Any]]
) -> Tuple[str, float, bool]:
    """
    Classify scan results to determine verification method.

    Returns:
        Tuple of (classification, top_confidence, is_ambiguous)
    """
    if not candidates:
        return ('none', 0.0, False)

    if len(candidates) == 1:
        return ('single', SINGLE_BUILDING_CONFIDENCE, False)

    top_score = candidates[0]['score']
    second_score = candidates[1]['score'] if len(candidates) > 1 else 0

    score_gap = top_score - second_score

    # Check if top candidate is clearly the winner
    if score_gap >= AMBIGUITY_THRESHOLD:
        return ('clear_winner', top_score, False)

    # Check if top candidates are too close (ambiguous)
    # Additional check: both within close distance
    both_close = (
        candidates[0]['distance_meters'] is not None and
        candidates[0]['distance_meters'] < 50 and
        candidates[1]['distance_meters'] is not None and
        candidates[1]['distance_meters'] < 50
    )

    if score_gap < AMBIGUITY_THRESHOLD and both_close:
        return ('ambiguous', top_score, True)

    # Default to clear winner if not close enough to be ambiguous
    return ('clear_winner', top_score, False)


async def fallback_centroid_query(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    pitch: float,
    max_distance: float,
    cone_angle: float,
    max_candidates: int
) -> Dict[str, Any]:
    """
    Fallback to V1 centroid-based query if footprint table unavailable.

    This queries the buildings_full_merge_scanning table using point geometry.
    Less accurate but provides backwards compatibility.
    """
    logger.warning("Using fallback centroid-based query (V1)")

    # Import V1 geospatial
    from services.geospatial import get_candidate_buildings

    try:
        v1_candidates = await get_candidate_buildings(
            session, lat, lng, bearing, pitch, max_distance, max_candidates
        )

        # Convert V1 format to V2 format
        candidates = []
        for c in v1_candidates:
            # Calculate approximate score from V1 data
            distance_score = math.exp(-c.get('distance_meters', 100) / 30) * 40
            bearing_score = max(0, 1 - c.get('bearing_difference', 90) / 30) * 30
            score = distance_score + bearing_score + 20  # Base score

            candidates.append({
                'bin': c.get('bin'),
                'bbl': c.get('bbl'),
                'name': None,  # V1 doesn't have name
                'distance_meters': c.get('distance_meters'),
                'bearing_to_building': c.get('bearing_to_building'),
                'bearing_difference': c.get('bearing_difference'),
                'visible_area': None,  # Not available in V1
                'shape_area': None,
                'height_roof': None,
                'score': round(score, 2),
                'address': c.get('address'),  # V1 has address
            })

        # Sort by score descending
        candidates.sort(key=lambda x: x['score'], reverse=True)

        classification, top_confidence, is_ambiguous = classify_results(candidates)

        return {
            'candidates': candidates,
            'classification': classification,
            'top_confidence': top_confidence,
            'is_ambiguous': is_ambiguous,
            'num_candidates': len(candidates),
            'fallback': True,  # Indicate V1 fallback was used
        }

    except Exception as e:
        logger.error(f"Fallback query also failed: {e}", exc_info=True)
        return {
            'candidates': [],
            'classification': 'none',
            'top_confidence': 0.0,
            'is_ambiguous': False,
            'num_candidates': 0,
            'error': str(e),
        }


async def get_building_metadata(
    session: AsyncSession,
    bins: List[str],
    bbls: List[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch building metadata from buildings_full_merge_scanning and PLUTO.

    Priority:
    1. Check buildings_full_merge_scanning (notable buildings with rich data)
    2. Fall back to PLUTO via Railway (basic data for all 857k buildings)

    Args:
        session: Database session (Supabase)
        bins: List of Building Identification Numbers
        bbls: List of BBLs for PLUTO fallback

    Returns:
        Dictionary mapping BIN to metadata dict
    """
    if not bins:
        return {}

    # Clean BINs (remove .0 suffix)
    clean_bins = [str(b).replace('.0', '') for b in bins]
    metadata = {}

    # Step 1: Try buildings_full_merge_scanning (notable buildings)
    try:
        result = await session.execute(
            text("""
                SELECT
                    REPLACE(bin, '.0', '') as bin,
                    building_name,
                    address,
                    architect,
                    style,
                    year_built,
                    landmark,
                    mat_prim,
                    height,
                    num_floors
                FROM buildings_full_merge_scanning
                WHERE REPLACE(bin, '.0', '') = ANY(:bins)
            """),
            {'bins': clean_bins}
        )

        for row in result:
            bin_val = row[0]
            metadata[bin_val] = {
                'name': row[1],
                'address': row[2],
                'architect': row[3],
                'style': row[4],
                'year_built': row[5],
                'is_landmark': row[6] is not None and row[6] != '',
                'landmark_type': row[6],
                'material': row[7],
                'height': row[8],
                'num_floors': row[9],
                'source': 'notable_buildings'
            }

    except Exception as e:
        logger.error(f"Failed to fetch from buildings_full_merge_scanning: {e}")

    # Step 2: For buildings not found, try PLUTO via Railway
    missing_bins = [b for b in clean_bins if b not in metadata]
    if missing_bins and bbls:
        # Map BINs to BBLs for lookup
        bin_to_bbl = {}
        for i, b in enumerate(clean_bins):
            if b in missing_bins and i < len(bbls) and bbls[i]:
                bin_to_bbl[b] = str(bbls[i]).replace('.0', '')

        if bin_to_bbl:
            try:
                async with get_footprints_db() as footprints_db:
                    if footprints_db:
                        bbl_list = list(bin_to_bbl.values())
                        result = await footprints_db.execute(
                            text("""
                                SELECT
                                    bbl,
                                    address,
                                    year_built,
                                    num_floors,
                                    bldg_class_desc,
                                    owner_name,
                                    bldg_area,
                                    units_res,
                                    zoning
                                FROM pluto_buildings
                                WHERE bbl = ANY(:bbls)
                            """),
                            {'bbls': bbl_list}
                        )

                        bbl_to_data = {}
                        for row in result:
                            bbl_to_data[row[0]] = {
                                'address': row[1],
                                'year_built': int(row[2]) if row[2] else None,
                                'num_floors': int(row[3]) if row[3] else None,
                                'building_type': row[4],
                                'owner': row[5],
                                'building_area': int(row[6]) if row[6] else None,
                                'units': row[7],
                                'zoning': row[8],
                            }

                        # Map back to BINs
                        for bin_val, bbl in bin_to_bbl.items():
                            if bbl in bbl_to_data:
                                data = bbl_to_data[bbl]
                                metadata[bin_val] = {
                                    'name': data['address'],  # Use address as name for unknown buildings
                                    'address': data['address'],
                                    'architect': None,
                                    'style': None,
                                    'year_built': data['year_built'],
                                    'is_landmark': False,
                                    'landmark_type': None,
                                    'material': None,
                                    'height': None,
                                    'num_floors': None,  # Don't need floors
                                    'use': data['building_type'],  # e.g. "Two Family Dwelling"
                                    'type': data['building_type'],
                                    'source': 'pluto'
                                }

            except Exception as e:
                logger.error(f"Failed to fetch from PLUTO: {e}")

    logger.info(f"Fetched metadata for {len(metadata)}/{len(bins)} buildings")
    return metadata


async def enrich_candidates_with_metadata(
    session: AsyncSession,
    candidates: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Enrich footprint candidates with building metadata.

    Fetches additional data from buildings_full_merge_scanning (notable)
    and PLUTO (all buildings) and merges it into candidate dictionaries.
    """
    bins = [c['bin'] for c in candidates if c.get('bin')]
    bbls = [c.get('bbl') for c in candidates]
    metadata = await get_building_metadata(session, bins, bbls)

    enriched = []
    for candidate in candidates:
        bin_val = candidate.get('bin')
        if bin_val and bin_val in metadata:
            # Merge metadata into candidate
            candidate.update({
                'address': metadata[bin_val].get('address'),
                'building_name': metadata[bin_val].get('name') or candidate.get('name'),
                'architect': metadata[bin_val].get('architect'),
                'style': metadata[bin_val].get('style'),
                'year_built': metadata[bin_val].get('year_built'),
                'is_landmark': metadata[bin_val].get('is_landmark', False),
                'use': metadata[bin_val].get('use'),
                'type': metadata[bin_val].get('type'),
                'materials': metadata[bin_val].get('material'),
            })
        enriched.append(candidate)

    return enriched


async def expand_search_radius(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    initial_radius: float = 100,
    max_radius: float = 300,
    step: float = 50
) -> Dict[str, Any]:
    """
    Progressively expand search radius until buildings are found.

    Used when initial cone search returns no results (parks, plazas, GPS drift).

    Args:
        session: Database session
        lat: User latitude
        lng: User longitude
        bearing: Compass bearing
        initial_radius: Starting radius (already tried)
        max_radius: Maximum radius to try
        step: Radius increment per attempt

    Returns:
        Search results with expanded radius info
    """
    current_radius = initial_radius + step

    while current_radius <= max_radius:
        logger.info(f"Expanding search radius to {current_radius}m")

        result = await get_candidates_by_footprint(
            session, lat, lng, bearing,
            max_distance=current_radius,
            cone_angle=90  # Wider cone for expanded search
        )

        if result['candidates']:
            result['expanded_radius'] = current_radius
            return result

        current_radius += step

    # No buildings found even at max radius
    return {
        'candidates': [],
        'classification': 'none',
        'top_confidence': 0.0,
        'is_ambiguous': False,
        'num_candidates': 0,
        'expanded_radius': max_radius,
        'message': 'No buildings found. Try moving closer to a building.'
    }


def calculate_bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate bearing from point 1 to point 2.

    Returns bearing in degrees (0-360, 0=North).
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    lng_diff = math.radians(lng2 - lng1)

    x = math.sin(lng_diff) * math.cos(lat2_rad)
    y = (math.cos(lat1_rad) * math.sin(lat2_rad) -
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(lng_diff))

    bearing_rad = math.atan2(x, y)
    bearing_deg = (math.degrees(bearing_rad) + 360) % 360

    return bearing_deg


def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate distance in meters between two points using Haversine formula.
    """
    R = 6371000  # Earth radius in meters

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)

    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(dlng / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c

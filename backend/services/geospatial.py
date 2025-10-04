"""
Geospatial service for building candidate filtering
Implements cone-of-vision logic using PostGIS
"""

import math
from typing import List, Dict, Any, Optional
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from geoalchemy2.functions import ST_GeomFromText, ST_Intersects, ST_Distance
from geoalchemy2 import Geography
import logging

from models.database import Building
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def create_view_cone_wkt(
    lat: float,
    lng: float,
    bearing: float,
    distance: float,
    cone_angle: float
) -> str:
    """
    Generate WKT (Well-Known Text) polygon representing user's view cone

    Args:
        lat: User latitude
        lng: User longitude
        bearing: Compass bearing (0-360, 0=North)
        distance: Max distance in meters
        cone_angle: Total cone angle in degrees

    Returns:
        WKT string for the view cone polygon
    """

    def destination_point(lat_deg: float, lng_deg: float, bearing_deg: float, dist_m: float) -> tuple:
        """
        Calculate destination point given start point, bearing, and distance
        Using Haversine formula
        """
        R = 6371000  # Earth radius in meters

        lat_rad = math.radians(lat_deg)
        lng_rad = math.radians(lng_deg)
        brng_rad = math.radians(bearing_deg)

        lat2 = math.asin(
            math.sin(lat_rad) * math.cos(dist_m / R) +
            math.cos(lat_rad) * math.sin(dist_m / R) * math.cos(brng_rad)
        )

        lng2 = lng_rad + math.atan2(
            math.sin(brng_rad) * math.sin(dist_m / R) * math.cos(lat_rad),
            math.cos(dist_m / R) - math.sin(lat_rad) * math.sin(lat2)
        )

        return (math.degrees(lat2), math.degrees(lng2))

    # Start point (user location)
    points = [f"{lng} {lat}"]

    # Calculate left and right edges of cone
    left_bearing = bearing - (cone_angle / 2)
    right_bearing = bearing + (cone_angle / 2)

    # Generate arc points (more points = smoother cone)
    num_arc_points = 12
    for i in range(num_arc_points + 1):
        angle = left_bearing + (cone_angle * i / num_arc_points)
        pt = destination_point(lat, lng, angle, distance)
        points.append(f"{pt[1]} {pt[0]}")

    # Close the polygon back to start
    points.append(f"{lng} {lat}")

    return f"POLYGON(({', '.join(points)}))"


async def get_candidate_buildings(
    session: AsyncSession,
    lat: float,
    lng: float,
    bearing: float,
    pitch: float = 0,
    max_distance: Optional[float] = None,
    max_candidates: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Get buildings within user's view cone, sorted by relevance

    Args:
        session: Database session
        lat: User latitude
        lng: User longitude
        bearing: Compass bearing (0-360)
        pitch: Phone pitch angle (-90 to 90)
        max_distance: Max distance in meters (default from settings)
        max_candidates: Max number to return (default from settings)

    Returns:
        List of candidate building dictionaries with metadata
    """
    if max_distance is None:
        max_distance = settings.max_scan_distance_meters
    if max_candidates is None:
        max_candidates = settings.max_candidates

    # Generate view cone WKT
    cone_wkt = create_view_cone_wkt(
        lat, lng, bearing, max_distance, settings.cone_angle_degrees
    )

    logger.info(f"Searching for buildings at ({lat}, {lng}), bearing {bearing}°, pitch {pitch}°")

    # Build spatial query
    query = (
        select(Building)
        .where(
            ST_Intersects(
                Building.geom,
                ST_GeomFromText(cone_wkt, 4326)
            )
        )
        .where(Building.scan_enabled == True)
    )

    # Priority boosting based on pitch
    if pitch > 15:
        # Looking up - prioritize tall buildings
        logger.info("Looking up - prioritizing tall buildings")
        query = query.order_by(Building.num_floors.desc().nulls_last())
    elif pitch < -15:
        # Looking down - prioritize nearby/shorter buildings
        logger.info("Looking down - prioritizing nearby buildings")
        pass

    # Always prioritize landmarks
    query = query.order_by(
        Building.is_landmark.desc(),
        Building.walk_score.desc().nulls_last()
    )

    # Limit results
    query = query.limit(max_candidates)

    # Execute query
    result = await session.execute(query)
    buildings = result.scalars().all()

    logger.info(f"Found {len(buildings)} candidate buildings")

    # Calculate distances and prepare output
    candidates = []
    user_point_wkt = f"POINT({lng} {lat})"

    for building in buildings:
        # Calculate distance
        dist_query = select(
            ST_Distance(
                ST_GeomFromText(user_point_wkt, 4326).cast(Geography),
                Building.geom.cast(Geography)
            )
        ).where(Building.bbl == building.bbl)

        distance_result = await session.execute(dist_query)
        distance = distance_result.scalar()

        # Calculate bearing from user to building
        building_bearing = calculate_bearing(
            lat, lng,
            building.latitude, building.longitude
        )

        # Calculate bearing difference (0-180)
        bearing_diff = abs(((building_bearing - bearing + 180) % 360) - 180)

        candidates.append({
            'bbl': building.bbl,
            'bin': building.bin,
            'address': building.address,
            'borough': building.borough,
            'latitude': building.latitude,
            'longitude': building.longitude,
            'distance_meters': round(distance, 2),
            'bearing_to_building': round(building_bearing, 1),
            'bearing_difference': round(bearing_diff, 1),
            'num_floors': building.num_floors,
            'year_built': building.year_built,
            'is_landmark': building.is_landmark,
            'landmark_name': building.landmark_name,
            'architect': building.architect,
            'architectural_style': building.architectural_style,
            'walk_score': building.walk_score,
        })

    # Sort by combined relevance score
    candidates.sort(key=lambda x: calculate_relevance_score(x), reverse=True)

    return candidates[:max_candidates]


def calculate_bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate bearing from point 1 to point 2
    Returns bearing in degrees (0-360, 0=North)
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    lng_diff = math.radians(lng2 - lng1)

    x = math.sin(lng_diff) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - \
        math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(lng_diff)

    bearing_rad = math.atan2(x, y)
    bearing_deg = (math.degrees(bearing_rad) + 360) % 360

    return bearing_deg


def calculate_relevance_score(candidate: Dict[str, Any]) -> float:
    """
    Calculate relevance score for sorting candidates
    Combines distance, bearing alignment, landmark status, and score
    """
    score = 0.0

    # Distance score (closer = better)
    # 0-30m: 1.0, 30-60m: 0.5, 60+m: 0.2
    distance = candidate['distance_meters']
    if distance < 30:
        score += 1.0
    elif distance < 60:
        score += 0.5
    else:
        score += 0.2

    # Bearing alignment score (more aligned = better)
    # 0-15°: 1.0, 15-30°: 0.5, 30+°: 0.2
    bearing_diff = candidate['bearing_difference']
    if bearing_diff < 15:
        score += 1.0
    elif bearing_diff < 30:
        score += 0.5
    else:
        score += 0.2

    # Landmark bonus
    if candidate['is_landmark']:
        score += 0.5

    # Final score bonus (normalized 0-1)
    if candidate.get('walk_score'):
        score += min(candidate['walk_score'] / 100, 1.0)

    return score


async def get_buildings_in_radius(
    session: AsyncSession,
    lat: float,
    lng: float,
    radius_meters: float = 50
) -> List[Dict[str, Any]]:
    """
    Simple radius-based search (fallback if cone search fails)
    """
    user_point_wkt = f"POINT({lng} {lat})"

    query = (
        select(
            Building,
            ST_Distance(
                ST_GeomFromText(user_point_wkt, 4326).cast(Geography),
                Building.geom.cast(Geography)
            ).label('distance')
        )
        .where(
            ST_Distance(
                ST_GeomFromText(user_point_wkt, 4326).cast(Geography),
                Building.geom.cast(Geography)
            ) <= radius_meters
        )
        .where(Building.scan_enabled == True)
        .order_by('distance')
        .limit(settings.max_candidates)
    )

    result = await session.execute(query)
    rows = result.all()

    candidates = []
    for building, distance in rows:
        candidates.append({
            'bbl': building.bbl,
            'address': building.address,
            'latitude': building.latitude,
            'longitude': building.longitude,
            'distance_meters': round(distance, 2),
            'is_landmark': building.is_landmark,
            'final_score': building.final_score,
        })

    return candidates
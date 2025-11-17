"""
Geospatial service for building candidate filtering
Implements cone-of-vision logic using lat/lng bounding box calculations
"""

import math
from typing import List, Dict, Any, Optional
from sqlalchemy import select, func, text, cast, Float
from sqlalchemy.ext.asyncio import AsyncSession
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


def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate distance in meters between two points using Haversine formula
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


def is_point_in_cone(
    user_lat: float, user_lng: float,
    point_lat: float, point_lng: float,
    bearing: float, cone_angle: float, max_distance: float
) -> bool:
    """
    Check if a point is within the user's view cone
    """
    distance = calculate_distance(user_lat, user_lng, point_lat, point_lng)
    if distance > max_distance:
        return False

    point_bearing = calculate_bearing(user_lat, user_lng, point_lat, point_lng)
    bearing_diff = abs(((point_bearing - bearing + 180) % 360) - 180)

    return bearing_diff <= (cone_angle / 2)


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

    logger.info(f"Searching for buildings at ({lat}, {lng}), bearing {bearing}°, pitch {pitch}°")

    # Calculate bounding box for initial filter (square around user)
    # Approximate: 1 degree latitude ≈ 111km, 1 degree longitude ≈ 111km * cos(lat)
    lat_delta = (max_distance / 111000) * 1.5  # Add 50% buffer
    lng_delta = (max_distance / (111000 * math.cos(math.radians(lat)))) * 1.5

    min_lat = lat - lat_delta
    max_lat = lat + lat_delta
    min_lng = lng - lng_delta
    max_lng = lng + lng_delta

    # Build query with bounding box filter
    # Cast text columns to float for comparison
    query = (
        select(Building)
        .where(cast(Building.latitude, Float) >= min_lat)
        .where(cast(Building.latitude, Float) <= max_lat)
        .where(cast(Building.longitude, Float) >= min_lng)
        .where(cast(Building.longitude, Float) <= max_lng)
        .where(Building.latitude != None)
        .where(Building.longitude != None)
        .where(Building.latitude != '')
        .where(Building.longitude != '')
    )

    # Execute query
    result = await session.execute(query)
    buildings = result.scalars().all()

    logger.info(f"Found {len(buildings)} buildings in bounding box")

    # Filter buildings in view cone and calculate metadata
    candidates = []

    for building in buildings:
        try:
            # Parse lat/lng from text
            b_lat = float(building.latitude) if building.latitude else None
            b_lng = float(building.longitude) if building.longitude else None

            if b_lat is None or b_lng is None:
                continue

            # Calculate distance
            distance = calculate_distance(lat, lng, b_lat, b_lng)

            # Check if in view cone
            if not is_point_in_cone(lat, lng, b_lat, b_lng, bearing, settings.cone_angle_degrees, max_distance):
                continue

            # Calculate bearing from user to building
            building_bearing = calculate_bearing(lat, lng, b_lat, b_lng)

            # Calculate bearing difference (0-180)
            bearing_diff = abs(((building_bearing - bearing + 180) % 360) - 180)

            # Parse other fields
            bin_val = str(building.bin).replace('.0', '') if building.bin else 'N/A'

            # Skip public spaces
            if bin_val == 'N/A':
                continue

            candidates.append({
                'bin': bin_val,
                'bbl': str(building.bbl).replace('.0', '') if building.bbl else None,
                'address': building.address,
                'borough': building.borough,
                'latitude': b_lat,
                'longitude': b_lng,
                'distance_meters': round(distance, 2),
                'bearing_to_building': round(building_bearing, 1),
                'bearing_difference': round(bearing_diff, 1),
            })
        except (ValueError, TypeError) as e:
            logger.warning(f"Skipping building due to parse error: {e}")
            continue

    logger.info(f"Found {len(candidates)} candidate buildings in view cone")

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
    Combines distance and bearing alignment
    """
    score = 0.0

    # Distance score (closer = better)
    # 0-30m: 1.0, 30-60m: 0.5, 60+m: 0.2
    distance = candidate.get('distance_meters', float('inf'))
    if distance < 30:
        score += 1.0
    elif distance < 60:
        score += 0.5
    else:
        score += 0.2

    # Bearing alignment score (more aligned = better)
    # 0-15°: 1.0, 15-30°: 0.5, 30+°: 0.2
    bearing_diff = candidate.get('bearing_difference', 180)
    if bearing_diff < 15:
        score += 1.0
    elif bearing_diff < 30:
        score += 0.5
    else:
        score += 0.2

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
    # Calculate bounding box
    lat_delta = (radius_meters / 111000) * 1.5
    lng_delta = (radius_meters / (111000 * math.cos(math.radians(lat)))) * 1.5

    min_lat = lat - lat_delta
    max_lat = lat + lat_delta
    min_lng = lng - lng_delta
    max_lng = lng + lng_delta

    # Query buildings in bounding box
    query = (
        select(Building)
        .where(cast(Building.latitude, Float) >= min_lat)
        .where(cast(Building.latitude, Float) <= max_lat)
        .where(cast(Building.longitude, Float) >= min_lng)
        .where(cast(Building.longitude, Float) <= max_lng)
        .where(Building.latitude != None)
        .where(Building.longitude != None)
        .where(Building.latitude != '')
        .where(Building.longitude != '')
    )

    result = await session.execute(query)
    buildings = result.scalars().all()

    # Filter by actual distance and prepare output
    candidates = []
    for building in buildings:
        try:
            b_lat = float(building.latitude) if building.latitude else None
            b_lng = float(building.longitude) if building.longitude else None

            if b_lat is None or b_lng is None:
                continue

            distance = calculate_distance(lat, lng, b_lat, b_lng)

            if distance <= radius_meters:
                bin_val = str(building.bin).replace('.0', '') if building.bin else 'N/A'

                if bin_val == 'N/A':
                    continue

                candidates.append({
                    'bin': bin_val,
                    'bbl': str(building.bbl).replace('.0', '') if building.bbl else None,
                    'address': building.address,
                    'latitude': b_lat,
                    'longitude': b_lng,
                    'distance_meters': round(distance, 2),
                })
        except (ValueError, TypeError):
            continue

    # Sort by distance
    candidates.sort(key=lambda x: x['distance_meters'])

    return candidates[:settings.max_candidates]
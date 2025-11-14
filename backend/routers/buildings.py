"""
Buildings API endpoints - Building details and metadata
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional, List
import logging

from models.database import Building
from services import reference_images

logger = logging.getLogger(__name__)
router = APIRouter()


# TODO: Add database session dependency
async def get_db():
    pass


@router.get("/buildings/{bin}")
async def get_building_detail(
    bin: str,
    # db: AsyncSession = Depends(get_db)
):
    """
    Get detailed information about a building by BIN (Building Identification Number)

    Returns building metadata, landmark status, architectural details
    Now uses BIN instead of BBL as the primary identifier
    """
    try:
        # TODO: Query database
        # result = await db.execute(
        #     select(Building).where(Building.bin == bin)
        # )
        # building = result.scalar_one_or_none()
        #
        # if not building:
        #     raise HTTPException(status_code=404, detail="Building not found")

        # Mock data for now
        return {
            'bin': bin,
            'address': '1 Wall Street, New York, NY',
            'borough': 'Manhattan',
            'latitude': 40.7074,
            'longitude': -74.0113,
            'year_built': 1930,
            'num_floors': 50,
            'height_ft': 654,
            'building_class': 'O1',
            'is_landmark': True,
            'landmark_name': 'Bank of New York Building',
            'architect': 'Benjamin Wistar Morris',
            'style_primary': 'Art Deco',
            'style_secondary': 'Neo-Gothic',
            'materials': ['Limestone', 'Granite', 'Bronze'],
            'final_score': 85.0,
            'historical_score': 90.0,
            'visual_score': 82.0,
            'cultural_score': 88.0,
            'description': 'The Bank of New York Building is a historic Art Deco skyscraper...',
            'description_sources': ['Wikipedia', 'NYC Landmarks'],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get building {bin}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get building details")


@router.get("/buildings/{bin}/images")
async def get_building_images(
    bin: str,
    # db: AsyncSession = Depends(get_db)
):
    """
    Get all reference images for a building by BIN
    Now uses BIN instead of BBL as the primary identifier
    """
    try:
        # TODO: Query database
        # images = await reference_images.get_all_reference_images_for_building(db, bin)

        # Mock data
        return {
            'bin': bin,
            'images': [],
            'count': 0
        }

    except Exception as e:
        logger.error(f"Failed to get building images: {e}")
        raise HTTPException(status_code=500, detail="Failed to get building images")


@router.get("/buildings/nearby")
async def get_nearby_buildings(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_meters: float = Query(100, ge=10, le=500, description="Search radius in meters"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    landmarks_only: bool = Query(False, description="Return only landmarks"),
    # db: AsyncSession = Depends(get_db)
):
    """
    Get buildings near a location (simple radius search)
    """
    try:
        # TODO: Query database
        # from services.geospatial import get_buildings_in_radius
        # buildings = await get_buildings_in_radius(db, lat, lng, radius_meters)

        # Mock data
        return {
            'location': {'lat': lat, 'lng': lng},
            'radius_meters': radius_meters,
            'buildings': [],
            'count': 0
        }

    except Exception as e:
        logger.error(f"Failed to get nearby buildings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get nearby buildings")


@router.get("/buildings/search")
async def search_buildings(
    q: str = Query(..., min_length=2, description="Search query"),
    borough: Optional[str] = Query(None, description="Filter by borough"),
    landmarks_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    # db: AsyncSession = Depends(get_db)
):
    """
    Search buildings by address, landmark name, or architect
    """
    try:
        # TODO: Implement full-text search
        # query = select(Building).where(
        #     or_(
        #         Building.address.ilike(f"%{q}%"),
        #         Building.landmark_name.ilike(f"%{q}%"),
        #         Building.architect.ilike(f"%{q}%")
        #     )
        # )
        #
        # if borough:
        #     query = query.where(Building.borough == borough)
        #
        # if landmarks_only:
        #     query = query.where(Building.is_landmark == True)
        #
        # query = query.limit(limit)
        # result = await db.execute(query)
        # buildings = result.scalars().all()

        return {
            'query': q,
            'buildings': [],
            'count': 0
        }

    except Exception as e:
        logger.error(f"Failed to search buildings: {e}")
        raise HTTPException(status_code=500, detail="Failed to search buildings")


@router.get("/buildings/top-landmarks")
async def get_top_landmarks(
    limit: int = Query(100, ge=1, le=500),
    borough: Optional[str] = Query(None),
    # db: AsyncSession = Depends(get_db)
):
    """
    Get top-rated landmark buildings
    """
    try:
        # TODO: Query database
        # query = (
        #     select(Building)
        #     .where(Building.is_landmark == True)
        #     .order_by(Building.final_score.desc())
        # )
        #
        # if borough:
        #     query = query.where(Building.borough == borough)
        #
        # query = query.limit(limit)
        # result = await db.execute(query)
        # landmarks = result.scalars().all()

        return {
            'landmarks': [],
            'count': 0
        }

    except Exception as e:
        logger.error(f"Failed to get top landmarks: {e}")
        raise HTTPException(status_code=500, detail="Failed to get top landmarks")


@router.get("/stats")
async def get_database_stats(
    # db: AsyncSession = Depends(get_db)
):
    """
    Get database statistics
    """
    try:
        # TODO: Query database stats
        # total_buildings = await db.scalar(select(func.count(Building.bbl)))
        # total_landmarks = await db.scalar(
        #     select(func.count(Building.bbl)).where(Building.is_landmark == True)
        # )
        # total_reference_images = await db.scalar(select(func.count(ReferenceImage.id)))

        return {
            'total_buildings': 0,
            'total_landmarks': 0,
            'total_reference_images': 0,
            'buildings_with_reference_images': 0,
            'avg_images_per_building': 0.0,
            'cache_coverage_percent': 0.0
        }

    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get stats")
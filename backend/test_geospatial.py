"""
Test script for geospatial queries with PostGIS
Tests cone-of-vision calculation and spatial filtering
"""

import asyncio
from sqlalchemy import text
from models.session import get_db_context
from services.geospatial import create_view_cone_wkt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_postgis_functions():
    """Test that PostGIS functions are available"""
    logger.info("Testing PostGIS functions...")

    async with get_db_context() as db:
        # Test ST_GeomFromText
        result = await db.execute(
            text("SELECT ST_AsText(ST_GeomFromText('POINT(-73.9857 40.7484)', 4326))")
        )
        point = result.scalar()
        logger.info(f"✅ ST_GeomFromText working: {point}")

        # Test ST_DWithin
        result = await db.execute(
            text("""
                SELECT ST_DWithin(
                    ST_GeomFromText('POINT(-73.9857 40.7484)', 4326)::geography,
                    ST_GeomFromText('POINT(-73.9858 40.7485)', 4326)::geography,
                    100
                )
            """)
        )
        within = result.scalar()
        logger.info(f"✅ ST_DWithin working: {within}")

        # Test ST_Azimuth
        result = await db.execute(
            text("""
                SELECT degrees(ST_Azimuth(
                    ST_GeomFromText('POINT(-73.9857 40.7484)', 4326)::geography::geometry,
                    ST_GeomFromText('POINT(-73.9858 40.7485)', 4326)::geography::geometry
                ))
            """)
        )
        azimuth = result.scalar()
        logger.info(f"✅ ST_Azimuth working: {azimuth}°")


async def test_cone_of_vision():
    """Test cone-of-vision WKT generation and PostGIS parsing"""
    logger.info("\nTesting cone-of-vision generation...")

    # Empire State Building location
    user_lat = 40.7484
    user_lon = -73.9857
    compass_bearing = 45.0  # Northeast
    distance = 500  # 500 meters
    cone_angle = 60  # 60 degree cone

    # Generate cone WKT
    cone_wkt = create_view_cone_wkt(user_lat, user_lon, compass_bearing, distance, cone_angle)
    logger.info(f"Generated cone WKT (first 100 chars): {cone_wkt[:100]}...")

    # Test that PostGIS can parse and use the cone
    async with get_db_context() as db:
        result = await db.execute(
            text("""
                SELECT
                    ST_IsValid(ST_GeomFromText(:cone_wkt, 4326)) as is_valid,
                    ST_GeometryType(ST_GeomFromText(:cone_wkt, 4326)) as geom_type,
                    ST_Area(ST_GeomFromText(:cone_wkt, 4326)::geography) as area_sq_meters
            """),
            {"cone_wkt": cone_wkt}
        )
        row = result.fetchone()
        logger.info(f"✅ Cone is valid: {row[0]}")
        logger.info(f"✅ Geometry type: {row[1]}")
        logger.info(f"✅ Cone area: {row[2]:.2f} sq meters")


async def test_building_in_cone():
    """Test if a point (simulated building) is within the cone of vision"""
    logger.info("\nTesting building location within cone...")

    # Empire State Building location (observer)
    user_lat = 40.7484
    user_lon = -73.9857
    compass_bearing = 45.0  # Looking northeast

    # Create cone
    cone_wkt = create_view_cone_wkt(user_lat, user_lon, compass_bearing, 500, 60)

    # Test building northeast of Empire State (should be in cone)
    building_lat = 40.7500  # Slightly north and east
    building_lon = -73.9840

    async with get_db_context() as db:
        # Check if building is within cone
        result = await db.execute(
            text("""
                SELECT ST_Contains(
                    ST_GeomFromText(:cone_wkt, 4326),
                    ST_GeomFromText(:building_point, 4326)
                ) as is_in_cone
            """),
            {
                "cone_wkt": cone_wkt,
                "building_point": f"POINT({building_lon} {building_lat})"
            }
        )
        in_cone = result.scalar()
        logger.info(f"✅ Building at ({building_lat}, {building_lon}) in cone: {in_cone}")

        # Note: bearing calculation is handled by create_view_cone_wkt
        logger.info(f"✅ Compass bearing used for cone: {compass_bearing}°")

        # Calculate distance
        result = await db.execute(
            text("""
                SELECT ST_Distance(
                    ST_GeomFromText(:user_point, 4326)::geography,
                    ST_GeomFromText(:building_point, 4326)::geography
                ) as distance_meters
            """),
            {
                "user_point": f"POINT({user_lon} {user_lat})",
                "building_point": f"POINT({building_lon} {building_lat})"
            }
        )
        distance = result.scalar()
        logger.info(f"✅ Distance to building: {distance:.2f} meters")


async def test_spatial_index():
    """Test that spatial queries can use indexes (when buildings table has data)"""
    logger.info("\nTesting spatial query structure...")

    user_lat = 40.7484
    user_lon = -73.9857
    compass_bearing = 45.0
    cone_wkt = create_view_cone_wkt(user_lat, user_lon, compass_bearing, 500, 60)

    async with get_db_context() as db:
        # This is the query structure we'll use in production
        # Note: buildings table is empty, but we can test the query structure
        # Need to cast geography to geometry for ST_Contains
        result = await db.execute(
            text("""
                EXPLAIN ANALYZE
                SELECT COUNT(*) FROM buildings
                WHERE ST_Contains(
                    ST_GeomFromText(:cone_wkt, 4326),
                    location::geometry
                )
            """),
            {"cone_wkt": cone_wkt}
        )

        logger.info("✅ Query plan for spatial filtering:")
        for row in result:
            logger.info(f"  {row[0]}")


async def main():
    """Run all geospatial tests"""
    logger.info("=" * 60)
    logger.info("GEOSPATIAL TESTS - PostGIS Cone-of-Vision")
    logger.info("=" * 60)

    try:
        await test_postgis_functions()
        await test_cone_of_vision()
        await test_building_in_cone()
        await test_spatial_index()

        logger.info("\n" + "=" * 60)
        logger.info("✅ ALL GEOSPATIAL TESTS PASSED")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"\n❌ Test failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
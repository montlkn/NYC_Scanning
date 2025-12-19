#!/usr/bin/env python3
"""
Test Script for Scan V2 System

Tests the building footprint-based identification system with real NYC coordinates.

Usage:
    python scripts/test_scan_v2.py
    python scripts/test_scan_v2.py --load-footprints  # Load footprints first
    python scripts/test_scan_v2.py --test-only        # Skip footprint check

Test locations:
- Empire State Building: 40.748817, -73.985428
- Chrysler Building: 40.751621, -73.975502
- Flatiron Building: 40.741061, -73.989699
- One World Trade Center: 40.712742, -74.013382
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


# Test locations (lat, lng, bearing, expected_name)
TEST_LOCATIONS = [
    {
        'name': 'Empire State Building',
        'lat': 40.748817,
        'lng': -73.985428,
        'bearing': 0,  # Looking north
        'expected_bin': '1015100001',  # Approximate
    },
    {
        'name': 'Chrysler Building',
        'lat': 40.751621,
        'lng': -73.975502,
        'bearing': 45,
        'expected_bin': '1012870037',
    },
    {
        'name': 'Flatiron Building',
        'lat': 40.741061,
        'lng': -73.989699,
        'bearing': 90,
        'expected_bin': '1008260053',
    },
    {
        'name': 'One World Trade Center',
        'lat': 40.712742,
        'lng': -74.013382,
        'bearing': 180,
        'expected_bin': '1000010001',
    },
    {
        'name': 'Random residential Brooklyn',
        'lat': 40.6892,
        'lng': -73.9842,
        'bearing': 270,
        'expected_bin': None,  # Any result is fine
    },
]


async def check_footprints_table():
    """Check if building_footprints table exists and has data."""
    # Use FOOTPRINTS_DB_URL for Railway database
    database_url = os.getenv('FOOTPRINTS_DB_URL') or os.getenv('DATABASE_URL')
    if not database_url:
        print("ERROR: FOOTPRINTS_DB_URL not set")
        return False

    # Convert to async URL
    if database_url.startswith('postgresql://'):
        async_url = database_url.replace('postgresql://', 'postgresql+asyncpg://')
    else:
        async_url = database_url

    engine = create_async_engine(async_url)

    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT COUNT(*) FROM building_footprints"))
            count = result.scalar()
            print(f"✅ building_footprints table has {count:,} rows (Railway)")
            return count > 0
        except Exception as e:
            print(f"❌ building_footprints table not found or empty: {e}")
            return False
        finally:
            await engine.dispose()


async def test_find_buildings_in_cone():
    """Test the find_buildings_in_cone PostGIS function."""
    # Use FOOTPRINTS_DB_URL for Railway database
    database_url = os.getenv('FOOTPRINTS_DB_URL') or os.getenv('DATABASE_URL')
    if database_url.startswith('postgresql://'):
        async_url = database_url.replace('postgresql://', 'postgresql+asyncpg://')
    else:
        async_url = database_url

    engine = create_async_engine(async_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("\n" + "=" * 70)
    print("TESTING find_buildings_in_cone() FUNCTION")
    print("=" * 70)

    async with async_session() as session:
        for loc in TEST_LOCATIONS:
            print(f"\n📍 {loc['name']}")
            print(f"   Location: ({loc['lat']}, {loc['lng']}), bearing {loc['bearing']}°")

            try:
                result = await session.execute(
                    text("""
                        SELECT bin, name, distance_meters, bearing_difference, visibility_score
                        FROM find_buildings_in_cone(:lat, :lng, :bearing, 100, 60, 5)
                    """),
                    {
                        'lat': loc['lat'],
                        'lng': loc['lng'],
                        'bearing': loc['bearing']
                    }
                )

                rows = result.fetchall()

                if rows:
                    print(f"   Found {len(rows)} candidates:")
                    for i, row in enumerate(rows[:3]):
                        print(f"   {i+1}. BIN {row[0]}: {row[1] or 'N/A'}")
                        print(f"      Distance: {row[2]:.1f}m, Bearing diff: {row[3]:.1f}°, Score: {row[4]:.1f}")
                else:
                    print("   ⚠️ No buildings found in cone")

            except Exception as e:
                print(f"   ❌ Query failed: {e}")

    await engine.dispose()


async def test_geospatial_v2():
    """Test the geospatial_v2 Python service."""
    print("\n" + "=" * 70)
    print("TESTING geospatial_v2.py SERVICE")
    print("=" * 70)

    database_url = os.getenv('DATABASE_URL')
    if database_url.startswith('postgresql://'):
        async_url = database_url.replace('postgresql://', 'postgresql+asyncpg://')
    else:
        async_url = database_url

    engine = create_async_engine(async_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from services import geospatial_v2

    async with async_session() as session:
        for loc in TEST_LOCATIONS[:2]:  # Just test first 2
            print(f"\n📍 {loc['name']}")

            try:
                result = await geospatial_v2.get_candidates_by_footprint(
                    session,
                    loc['lat'],
                    loc['lng'],
                    loc['bearing'],
                    pitch=0
                )

                print(f"   Classification: {result['classification']}")
                print(f"   Is Ambiguous: {result['is_ambiguous']}")
                print(f"   Top Confidence: {result['top_confidence']:.1f}")
                print(f"   Candidates: {result['num_candidates']}")

                if result['candidates']:
                    top = result['candidates'][0]
                    print(f"   Top Match: BIN {top['bin']}, Score {top['score']:.1f}")

            except Exception as e:
                print(f"   ❌ Service failed: {e}")
                import traceback
                traceback.print_exc()

    await engine.dispose()


async def test_full_scan_flow():
    """Test the complete scan V2 flow (without actual photo)."""
    print("\n" + "=" * 70)
    print("TESTING FULL SCAN V2 FLOW (Mock)")
    print("=" * 70)

    # This would require setting up the full FastAPI test client
    # For now, just validate the imports work
    try:
        from routers import scan_v2
        print("✅ scan_v2 router imported successfully")

        from services import clip_disambiguation
        print("✅ clip_disambiguation service imported successfully")

        print("\n📋 To test the full flow:")
        print("   1. Run the migration: psql $DATABASE_URL < migrations/20251213_building_footprints.sql")
        print("   2. Load footprints: python scripts/load_building_footprints.py")
        print("   3. Start server: USE_SCAN_V2=true python main.py")
        print("   4. Test endpoint: curl -X POST localhost:8000/api/scan ...")

    except Exception as e:
        print(f"❌ Import failed: {e}")


def main():
    parser = argparse.ArgumentParser(description='Test Scan V2 System')
    parser.add_argument('--load-footprints', action='store_true',
                       help='Load footprints before testing')
    parser.add_argument('--test-only', action='store_true',
                       help='Skip footprint check, run tests only')
    args = parser.parse_args()

    print("=" * 70)
    print("NYC SCAN V2 - System Test")
    print("=" * 70)

    # Check database
    if not args.test_only:
        has_footprints = asyncio.run(check_footprints_table())

        if not has_footprints:
            print("\n⚠️  Footprints table is empty or missing!")
            print("\nTo load footprints:")
            print("  1. psql $DATABASE_URL < migrations/20251213_building_footprints.sql")
            print("  2. python scripts/load_building_footprints.py")

            if not args.load_footprints:
                print("\nRun with --load-footprints to load now, or --test-only to skip")
                return

    # Run tests
    print("\n" + "=" * 70)
    print("RUNNING TESTS")
    print("=" * 70)

    try:
        asyncio.run(test_find_buildings_in_cone())
    except Exception as e:
        print(f"\n❌ PostGIS function test failed: {e}")
        print("   This is expected if footprints aren't loaded yet.")

    try:
        asyncio.run(test_geospatial_v2())
    except Exception as e:
        print(f"\n❌ Geospatial V2 test failed: {e}")

    asyncio.run(test_full_scan_flow())

    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()

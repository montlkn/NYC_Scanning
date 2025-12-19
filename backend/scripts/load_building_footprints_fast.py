#!/usr/bin/env python3
"""
Fast Building Footprints Loader using PostgreSQL COPY

This script is 100x faster than the row-by-row insert version.
Uses COPY FROM STDIN with a temporary staging table.

Expected time: ~2-3 minutes for 1.08M buildings (vs 10 hours with inserts)

Usage:
    python scripts/load_building_footprints_fast.py
"""

import os
import sys
import csv
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
from psycopg2 import sql

CSV_PATH = Path(__file__).parent.parent / "data" / "BUILDING_20251104.csv"


def load_footprints_fast():
    """Load footprints using PostgreSQL COPY command (bulk insert)."""

    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}")
        sys.exit(1)

    print("=" * 70)
    print("NYC SCAN - Fast Building Footprints Loader")
    print("=" * 70)
    print(f"CSV: {CSV_PATH}")
    print(f"Size: {CSV_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print("=" * 70)
    print()

    # Connect with psycopg2 (supports COPY)
    conn = psycopg2.connect(database_url)
    cursor = conn.cursor()

    try:
        # Disable statement timeout for bulk load
        print("Disabling statement timeout for bulk load...")
        cursor.execute("SET statement_timeout = 0")
        print("✅ Timeout disabled")

        print("\nStep 1: Creating temporary staging table...")
        cursor.execute("""
            CREATE TEMP TABLE footprints_staging (
                bin TEXT,
                bbl TEXT,
                name TEXT,
                footprint_wkt TEXT,
                height_roof FLOAT,
                ground_elevation FLOAT,
                shape_area FLOAT,
                construction_year INT,
                feature_code INT,
                geometry_source TEXT,
                last_edited_date TEXT
            );
        """)
        print("✅ Staging table created")

        print("\nStep 2: Bulk loading CSV into staging table...")
        start_time = time.time()

        # Read CSV and prepare data
        with open(CSV_PATH, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # Use COPY FROM STDIN for bulk insert
            copy_sql = """
                COPY footprints_staging (
                    bin, bbl, name, footprint_wkt,
                    height_roof, ground_elevation, shape_area,
                    construction_year, feature_code, geometry_source,
                    last_edited_date
                ) FROM STDIN WITH CSV
            """

            # Create a temporary CSV file with just the columns we need
            temp_csv = "/tmp/footprints_staging.csv"
            with open(temp_csv, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)

                count = 0
                for row in reader:
                    # Parse and clean data
                    bin_val = row.get('BIN', '').strip() or None
                    if not bin_val:
                        continue

                    footprint_wkt = row.get('the_geom', '').strip()
                    if not footprint_wkt:
                        continue

                    # Helper to clean numeric values
                    def clean_num(val):
                        if not val or val.strip() == '':
                            return None
                        return val.strip().replace(',', '')

                    writer.writerow([
                        bin_val,
                        row.get('BASE_BBL', '').strip() or None,
                        row.get('NAME', '').strip() or None,
                        footprint_wkt,
                        clean_num(row.get('Height Roof')),
                        clean_num(row.get('Ground Elevation')),
                        clean_num(row.get('SHAPE_AREA')),
                        clean_num(row.get('Construction Year')),
                        clean_num(row.get('Feature Code')),
                        row.get('Geometry Source', '').strip() or None,
                        row.get('LAST_EDITED_DATE', '').strip() or None,
                    ])

                    count += 1
                    if count % 50000 == 0:
                        print(f"  Prepared {count:,} rows...", end='\r')

            print(f"\n  Prepared {count:,} rows total")

        # Now COPY from the temp file
        print("  Copying to staging table...")
        with open(temp_csv, 'r') as f:
            cursor.copy_expert(copy_sql, f)

        os.remove(temp_csv)

        rows_loaded = cursor.rowcount
        load_time = time.time() - start_time
        print(f"✅ Loaded {rows_loaded:,} rows in {load_time:.1f}s ({rows_loaded/load_time:.0f} rows/sec)")

        print("\nStep 3: Transforming geometries and inserting into building_footprints...")
        transform_start = time.time()

        cursor.execute("""
            INSERT INTO building_footprints (
                bin, bbl, name,
                footprint, centroid,
                height_roof, ground_elevation, shape_area,
                construction_year, feature_code, geometry_source,
                last_edited_date
            )
            SELECT
                bin, bbl, name,
                ST_GeomFromText(footprint_wkt, 4326),
                ST_Centroid(ST_GeomFromText(footprint_wkt, 4326)),
                height_roof, ground_elevation, shape_area,
                construction_year, feature_code, geometry_source,
                CASE
                    WHEN last_edited_date IS NOT NULL AND last_edited_date != ''
                    THEN TO_TIMESTAMP(last_edited_date, 'YYYY Mon DD HH12:MI:SS AM')
                    ELSE NULL
                END
            FROM footprints_staging
            ON CONFLICT (bin) DO UPDATE SET
                bbl = EXCLUDED.bbl,
                name = COALESCE(EXCLUDED.name, building_footprints.name),
                footprint = EXCLUDED.footprint,
                centroid = EXCLUDED.centroid,
                height_roof = COALESCE(EXCLUDED.height_roof, building_footprints.height_roof),
                ground_elevation = COALESCE(EXCLUDED.ground_elevation, building_footprints.ground_elevation),
                shape_area = COALESCE(EXCLUDED.shape_area, building_footprints.shape_area),
                construction_year = COALESCE(EXCLUDED.construction_year, building_footprints.construction_year),
                updated_at = NOW()
        """)

        rows_inserted = cursor.rowcount
        transform_time = time.time() - transform_start
        print(f"✅ Inserted/updated {rows_inserted:,} rows in {transform_time:.1f}s")

        print("\nStep 4: Analyzing table for query optimization...")
        cursor.execute("ANALYZE building_footprints")
        print("✅ Analysis complete")

        # Commit transaction
        conn.commit()

        total_time = time.time() - start_time
        print("\n" + "=" * 70)
        print("LOAD COMPLETE")
        print("=" * 70)
        print(f"Total rows: {rows_inserted:,}")
        print(f"Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
        print(f"Average rate: {rows_inserted/total_time:.0f} rows/second")
        print("=" * 70)

        # Verify
        print("\nVerifying data...")
        cursor.execute("SELECT COUNT(*) FROM building_footprints")
        final_count = cursor.fetchone()[0]
        print(f"✅ Final count: {final_count:,} buildings in database")

        # Check spatial indexes
        print("\nSpatial indexes:")
        cursor.execute("""
            SELECT
                indexname,
                pg_size_pretty(pg_relation_size(indexrelid)) as size
            FROM pg_indexes
            JOIN pg_class ON indexname = relname
            WHERE tablename = 'building_footprints'
        """)
        for row in cursor.fetchall():
            print(f"  {row[0]}: {row[1]}")

        # Test query
        print("\nTesting query (Empire State Building area)...")
        cursor.execute("""
            SELECT COUNT(*) FROM find_buildings_in_cone(
                40.748817, -73.985428, 0, 100, 60, 10
            )
        """)
        test_count = cursor.fetchone()[0]
        print(f"✅ Found {test_count} buildings in test cone")

        print("\n" + "=" * 70)
        print("NEXT STEPS")
        print("=" * 70)
        print("1. Test the system:")
        print("   python scripts/test_scan_v2.py")
        print()
        print("2. Start the server with V2:")
        print("   USE_SCAN_V2=true python main.py")
        print()
        print("3. Test scan endpoint:")
        print("   curl -X POST http://localhost:8000/api/scan \\")
        print("     -F 'photo=@test.jpg' \\")
        print("     -F 'gps_lat=40.748817' \\")
        print("     -F 'gps_lng=-73.985428' \\")
        print("     -F 'compass_bearing=45'")
        print("=" * 70)

    except Exception as e:
        conn.rollback()
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    load_footprints_fast()

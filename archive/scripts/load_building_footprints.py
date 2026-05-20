#!/usr/bin/env python3
"""
Load NYC Building Footprints into PostGIS

Loads 1.08M building footprints from NYC Open Data CSV into the building_footprints table.
This is a one-time data loading script that should be run after the migration.

Source: NYC Open Data Building Footprints
File: BUILDING_20251104.csv

Usage:
    python scripts/load_building_footprints.py
    python scripts/load_building_footprints.py --dry-run
    python scripts/load_building_footprints.py --limit 1000
    python scripts/load_building_footprints.py --batch-size 5000

Requirements:
    - PostGIS extension enabled
    - building_footprints table created (run migration first)
    - CSV file at data/BUILDING_20251104.csv
"""

import os
import sys
import csv
import argparse
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Generator, Dict, Any

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

# CSV file path
CSV_PATH = Path(__file__).parent.parent / "data" / "BUILDING_20251104.csv"

# Column mapping from CSV to database
COLUMN_MAP = {
    'the_geom': 'footprint_wkt',
    'BIN': 'bin',
    'BASE_BBL': 'bbl',
    'NAME': 'name',
    'Height Roof': 'height_roof',
    'Ground Elevation': 'ground_elevation',
    'SHAPE_AREA': 'shape_area',
    'Construction Year': 'construction_year',
    'Feature Code': 'feature_code',
    'Geometry Source': 'geometry_source',
    'LAST_EDITED_DATE': 'last_edited_date',
}


def parse_float(value: str) -> Optional[float]:
    """Safely parse float from string"""
    if not value or value.strip() == '':
        return None
    try:
        # Handle comma-formatted numbers
        cleaned = value.replace(',', '')
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_int(value: str) -> Optional[int]:
    """Safely parse integer from string"""
    if not value or value.strip() == '':
        return None
    try:
        cleaned = value.replace(',', '')
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def parse_datetime(value: str) -> Optional[datetime]:
    """Parse datetime from NYC Open Data format"""
    if not value or value.strip() == '':
        return None
    try:
        # Format: "2017 Aug 22 07:18:38 PM"
        return datetime.strptime(value, "%Y %b %d %I:%M:%S %p")
    except (ValueError, TypeError):
        return None


def read_csv_batches(
    csv_path: Path,
    batch_size: int = 1000,
    limit: Optional[int] = None
) -> Generator[list, None, None]:
    """
    Read CSV in batches for memory-efficient processing.

    Yields batches of parsed rows.
    """
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)

        batch = []
        count = 0

        for row in reader:
            if limit and count >= limit:
                break

            # Parse row
            parsed = {
                'bin': row.get('BIN', '').strip() or None,
                'bbl': row.get('BASE_BBL', '').strip() or None,
                'name': row.get('NAME', '').strip() or None,
                'footprint_wkt': row.get('the_geom', '').strip() or None,
                'height_roof': parse_float(row.get('Height Roof', '')),
                'ground_elevation': parse_float(row.get('Ground Elevation', '')),
                'shape_area': parse_float(row.get('SHAPE_AREA', '')),
                'construction_year': parse_int(row.get('Construction Year', '')),
                'feature_code': parse_int(row.get('Feature Code', '')),
                'geometry_source': row.get('Geometry Source', '').strip() or None,
                'last_edited_date': parse_datetime(row.get('LAST_EDITED_DATE', '')),
            }

            # Skip rows without BIN or geometry
            if not parsed['bin'] or not parsed['footprint_wkt']:
                continue

            batch.append(parsed)
            count += 1

            if len(batch) >= batch_size:
                yield batch
                batch = []

        # Yield remaining
        if batch:
            yield batch


def load_batch(session, batch: list) -> Dict[str, int]:
    """
    Load a batch of buildings into the database.

    Returns dict with counts of inserted, skipped, errors.
    """
    inserted = 0
    skipped = 0
    errors = 0

    for row in batch:
        try:
            # Use ON CONFLICT to handle duplicates
            session.execute(text("""
                INSERT INTO building_footprints (
                    bin, bbl, name,
                    footprint, centroid,
                    height_roof, ground_elevation, shape_area,
                    construction_year, feature_code, geometry_source,
                    last_edited_date
                )
                VALUES (
                    :bin, :bbl, :name,
                    ST_GeomFromText(:footprint_wkt, 4326),
                    ST_Centroid(ST_GeomFromText(:footprint_wkt, 4326)),
                    :height_roof, :ground_elevation, :shape_area,
                    :construction_year, :feature_code, :geometry_source,
                    :last_edited_date
                )
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
            """), row)
            inserted += 1

        except Exception as e:
            errors += 1
            if errors <= 10:  # Only log first 10 errors
                print(f"  Error on BIN {row.get('bin')}: {str(e)[:100]}")

    return {'inserted': inserted, 'skipped': skipped, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(
        description='Load NYC building footprints into PostGIS'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Parse CSV but do not insert into database'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of rows to process'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=2000,
        help='Number of rows per batch (default: 2000)'
    )
    parser.add_argument(
        '--csv-path',
        type=str,
        default=str(CSV_PATH),
        help='Path to CSV file'
    )

    args = parser.parse_args()

    print("=" * 70)
    print("NYC SCAN - Building Footprints Loader")
    print("=" * 70)
    print(f"CSV File: {args.csv_path}")
    print(f"Batch Size: {args.batch_size}")
    print(f"Limit: {args.limit or 'None'}")
    print(f"Dry Run: {args.dry_run}")
    print("=" * 70)
    print()

    # Check CSV exists
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    # Get file size for progress estimation
    file_size = csv_path.stat().st_size
    print(f"CSV file size: {file_size / 1024 / 1024:.1f} MB")

    # Count lines for progress
    print("Counting rows...")
    with open(csv_path, 'r') as f:
        total_lines = sum(1 for _ in f) - 1  # Subtract header
    print(f"Total rows in CSV: {total_lines:,}")

    if args.limit:
        total_lines = min(total_lines, args.limit)
        print(f"Processing limit: {total_lines:,}")

    print()

    # Connect to database
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    if not args.dry_run:
        print("Connecting to database...")
        engine = create_engine(database_url)
        Session = sessionmaker(bind=engine)
        session = Session()

        # Check if table exists
        try:
            result = session.execute(text(
                "SELECT COUNT(*) FROM building_footprints"
            ))
            existing = result.scalar()
            print(f"Existing rows in building_footprints: {existing:,}")
        except Exception as e:
            print(f"ERROR: building_footprints table not found. Run migration first.")
            print(f"  {e}")
            sys.exit(1)

    # Process CSV
    print()
    print("Processing CSV...")
    start_time = time.time()

    total_inserted = 0
    total_skipped = 0
    total_errors = 0
    batches_processed = 0

    try:
        for batch in read_csv_batches(csv_path, args.batch_size, args.limit):
            batches_processed += 1

            if args.dry_run:
                total_inserted += len(batch)
                # Show sample on first batch
                if batches_processed == 1:
                    print("\nSample row:")
                    sample = batch[0]
                    for k, v in sample.items():
                        if k == 'footprint_wkt':
                            v = v[:100] + '...' if v and len(v) > 100 else v
                        print(f"  {k}: {v}")
            else:
                result = load_batch(session, batch)
                total_inserted += result['inserted']
                total_skipped += result['skipped']
                total_errors += result['errors']

                # Commit every batch
                session.commit()

            # Progress update
            processed = batches_processed * args.batch_size
            if processed > total_lines:
                processed = total_lines

            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (total_lines - processed) / rate if rate > 0 else 0

            print(
                f"\r  Processed: {processed:,}/{total_lines:,} "
                f"({processed/total_lines*100:.1f}%) | "
                f"Rate: {rate:.0f}/sec | "
                f"ETA: {eta/60:.1f} min",
                end='', flush=True
            )

    except KeyboardInterrupt:
        print("\n\nInterrupted by user!")
        if not args.dry_run:
            print("Committing partial progress...")
            session.commit()

    finally:
        if not args.dry_run:
            session.close()

    elapsed = time.time() - start_time

    print()
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total processed: {total_inserted + total_skipped + total_errors:,}")
    print(f"Inserted/Updated: {total_inserted:,}")
    print(f"Skipped: {total_skipped:,}")
    print(f"Errors: {total_errors:,}")
    print(f"Time elapsed: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
    print(f"Rate: {total_inserted / elapsed:.0f} rows/second")
    print("=" * 70)

    if not args.dry_run:
        print()
        print("Running ANALYZE on building_footprints...")
        session = Session()
        session.execute(text("ANALYZE building_footprints"))
        session.commit()
        session.close()
        print("Done!")

        print()
        print("Verifying spatial index...")
        session = Session()
        result = session.execute(text("""
            SELECT
                indexname,
                pg_size_pretty(pg_relation_size(indexrelid)) as size
            FROM pg_indexes
            JOIN pg_class ON indexname = relname
            WHERE tablename = 'building_footprints'
        """))
        print("Indexes:")
        for row in result:
            print(f"  {row[0]}: {row[1]}")
        session.close()

        print()
        print("Next steps:")
        print("  1. Test: SELECT * FROM find_buildings_in_cone(40.7128, -74.0060, 45, 100, 60, 10);")
        print("  2. Run backend tests")
        print("  3. Deploy to production")


if __name__ == '__main__':
    main()

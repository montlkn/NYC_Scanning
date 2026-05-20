#!/usr/bin/env python3
"""
PLUTO Data Ingestion Script

Loads NYC MapPLUTO data into the unified buildings table.

Usage:
    python scripts/ingest_pluto.py --csv data/pluto.csv
    python scripts/ingest_pluto.py --csv data/pluto.csv --dry-run
    python scripts/ingest_pluto.py --csv data/pluto.csv --limit 1000

PLUTO CSV columns needed:
    - BBL (Borough-Block-Lot)
    - Address
    - Borough
    - ZipCode
    - Latitude
    - Longitude
    - NumFloors
    - YearBuilt
    - BldgClass
    - LandUse
    - LotArea
    - BldgArea
"""

import asyncio
import csv
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
import argparse
from datetime import datetime

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Borough code mapping
BOROUGH_MAP = {
    "1": "Manhattan",
    "2": "Bronx",
    "3": "Brooklyn",
    "4": "Queens",
    "5": "Staten Island",
    "MN": "Manhattan",
    "BX": "Bronx",
    "BK": "Brooklyn",
    "QN": "Queens",
    "SI": "Staten Island",
}


def parse_bbl(bbl: str) -> Optional[str]:
    """Normalize BBL format to 10 digits (Borough-Block-Lot)"""
    bbl = str(bbl).strip().replace("-", "").replace(" ", "")
    if len(bbl) == 10 and bbl.isdigit():
        return bbl
    return None


def parse_float(value: str) -> Optional[float]:
    """Safely parse float from string"""
    try:
        val = float(str(value).strip())
        return val if val != 0 else None
    except (ValueError, AttributeError):
        return None


def parse_int(value: str) -> Optional[int]:
    """Safely parse integer from string"""
    try:
        val = int(float(str(value).strip()))
        return val if val != 0 else None
    except (ValueError, AttributeError):
        return None


def normalize_address(address: str) -> str:
    """Clean and normalize address"""
    return " ".join(str(address).strip().split())


def get_borough_name(borough_code: str) -> str:
    """Convert borough code to full name"""
    return BOROUGH_MAP.get(str(borough_code).strip().upper(), "Unknown")


async def ingest_pluto_csv(
    csv_path: str,
    dry_run: bool = False,
    limit: Optional[int] = None,
    skip_existing: bool = True
) -> Dict:
    """
    Ingest PLUTO CSV into buildings table

    Args:
        csv_path: Path to PLUTO CSV file
        dry_run: If True, don't actually insert data
        limit: Max number of rows to process
        skip_existing: Skip buildings that already exist (by BBL)

    Returns:
        Dict with statistics
    """

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    print(f"📂 Reading PLUTO CSV: {csv_path}")

    # Read CSV
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            rows.append(row)

    print(f"📊 Found {len(rows)} rows in CSV")

    # Parse rows
    buildings = []
    skipped = 0

    for row in rows:
        # Required fields
        bbl = parse_bbl(row.get('BBL', ''))
        if not bbl:
            skipped += 1
            continue

        lat = parse_float(row.get('latitude') or row.get('Latitude') or row.get('lat'))
        lng = parse_float(row.get('longitude') or row.get('Longitude') or row.get('lon') or row.get('lng'))
        if not lat or not lng or lat == 0 or lng == 0:
            skipped += 1
            continue

        address = normalize_address(row.get('address') or row.get('Address', ''))
        if not address:
            skipped += 1
            continue

        borough = get_borough_name(row.get('borough') or row.get('Borough', ''))

        # Optional fields
        num_floors = parse_int(row.get('numfloors') or row.get('NumFloors'))
        year_built = parse_int(row.get('yearbuilt') or row.get('YearBuilt'))
        building_class = (row.get('bldgclass') or row.get('BldgClass') or '').strip() or None
        land_use = (row.get('landuse') or row.get('LandUse') or '').strip() or None
        lot_area = parse_float(row.get('lotarea') or row.get('LotArea'))
        building_area = parse_float(row.get('bldgarea') or row.get('BldgArea'))
        zip_code = (row.get('postcode') or row.get('ZipCode') or row.get('zip_code') or '').strip()[:5] or None
        bin_code = (row.get('BIN') or row.get('bin') or '').strip() or None

        buildings.append({
            'bbl': bbl,
            'bin': bin_code,
            'address': address,
            'borough': borough,
            'zip_code': zip_code,
            'latitude': lat,
            'longitude': lng,
            'num_floors': num_floors,
            'year_built': year_built,
            'building_class': building_class,
            'land_use': land_use,
            'lot_area': lot_area,
            'building_area': building_area,
            'is_landmark': False,  # PLUTO data doesn't include landmarks
            'data_source': ['pluto'],
            'scan_enabled': True,
        })

    print(f"✅ Parsed {len(buildings)} valid buildings")
    print(f"⚠️  Skipped {skipped} rows (missing BBL/lat/lng/address)")

    if dry_run:
        print("🔍 DRY RUN - Not inserting data")
        print("\nSample building:")
        if buildings:
            import json
            print(json.dumps(buildings[0], indent=2))
        return {
            'total_rows': len(rows),
            'parsed': len(buildings),
            'skipped': skipped,
            'inserted': 0,
            'updated': 0,
            'dry_run': True
        }

    # Insert into database
    print(f"💾 Inserting into database...")

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    inserted = 0
    updated = 0
    errors = 0

    try:
        for i, building in enumerate(buildings):
            try:
                # Check if building exists
                if skip_existing:
                    existing = session.execute(
                        text("SELECT id FROM buildings_full_merge_scanning WHERE bbl = :bbl"),
                        {'bbl': building['bbl']}
                    ).fetchone()

                    if existing:
                        # Update existing building with PLUTO data (only if fields are NULL)
                        session.execute(text("""
                            UPDATE buildings_full_merge_scanning SET
                                bin = COALESCE(bin, :bin),
                                zip_code = COALESCE(zip_code, :zip_code),
                                num_floors = COALESCE(num_floors, :num_floors),
                                year_built = COALESCE(year_built, :year_built),
                                building_class = COALESCE(building_class, :building_class),
                                land_use = COALESCE(land_use, :land_use),
                                lot_area = COALESCE(lot_area, :lot_area),
                                building_area = COALESCE(building_area, :building_area),
                                data_source = array_append(data_source, 'pluto'),
                                updated_at = NOW()
                            WHERE bbl = :bbl
                              AND NOT ('pluto' = ANY(data_source))
                        """), building)
                        updated += 1
                        continue

                # Insert new building
                session.execute(text("""
                    INSERT INTO buildings_full_merge_scanning (
                        bbl, bin, address, borough, zip_code,
                        latitude, longitude,
                        num_floors, year_built, building_class, land_use,
                        lot_area, building_area,
                        is_landmark, data_source, scan_enabled
                    ) VALUES (
                        :bbl, :bin, :address, :borough, :zip_code,
                        :latitude, :longitude,
                        :num_floors, :year_built, :building_class, :land_use,
                        :lot_area, :building_area,
                        :is_landmark, :data_source, :scan_enabled
                    )
                    ON CONFLICT (bbl) DO NOTHING
                """), building)

                inserted += 1

                # Progress
                if (i + 1) % 1000 == 0:
                    session.commit()
                    print(f"  Processed {i + 1}/{len(buildings)} buildings...")

            except Exception as e:
                print(f"⚠️  Error on BBL {building['bbl']}: {e}")
                errors += 1

        # Final commit
        session.commit()
        print(f"✅ Database commit successful!")

    except Exception as e:
        session.rollback()
        print(f"❌ Database error: {e}")
        raise

    finally:
        session.close()

    return {
        'total_rows': len(rows),
        'parsed': len(buildings),
        'skipped': skipped,
        'inserted': inserted,
        'updated': updated,
        'errors': errors,
        'dry_run': False
    }


def main():
    parser = argparse.ArgumentParser(description='Ingest PLUTO CSV data into buildings table')
    parser.add_argument('--csv', required=True, help='Path to PLUTO CSV file')
    parser.add_argument('--dry-run', action='store_true', help='Preview without inserting')
    parser.add_argument('--limit', type=int, help='Limit number of rows to process')
    parser.add_argument('--no-skip-existing', action='store_true', help='Try to insert all buildings (may cause conflicts)')

    args = parser.parse_args()

    print("=" * 60)
    print("NYC SCAN - PLUTO Data Ingestion")
    print("=" * 60)
    print(f"CSV File: {args.csv}")
    print(f"Dry Run: {args.dry_run}")
    print(f"Limit: {args.limit or 'None'}")
    print(f"Skip Existing: {not args.no_skip_existing}")
    print("=" * 60)
    print()

    start_time = datetime.now()

    stats = asyncio.run(ingest_pluto_csv(
        csv_path=args.csv,
        dry_run=args.dry_run,
        limit=args.limit,
        skip_existing=not args.no_skip_existing
    ))

    elapsed = (datetime.now() - start_time).total_seconds()

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total CSV rows:     {stats['total_rows']}")
    print(f"Parsed buildings:   {stats['parsed']}")
    print(f"Skipped rows:       {stats['skipped']}")
    print(f"Inserted:           {stats['inserted']}")
    print(f"Updated:            {stats['updated']}")
    print(f"Errors:             {stats.get('errors', 0)}")
    print(f"Time elapsed:       {elapsed:.1f}s")
    print("=" * 60)

    if not stats['dry_run']:
        print("\n✅ PLUTO data ingestion complete!")
        print("\n📊 Next steps:")
        print("   1. Run: python scripts/enrich_landmarks.py --csv data/landmarks.csv")
        print("   2. Check Supabase: SELECT COUNT(*) FROM buildings;")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Landmarks Data Enrichment Script

Enriches the buildings table with landmark data from your pruned NYC landmarks CSV.

Usage:
    python scripts/enrich_landmarks.py --csv data/landmarks.csv
    python scripts/enrich_landmarks.py --csv data/landmarks.csv --dry-run
    python scripts/enrich_landmarks.py --csv data/landmarks.csv --create-missing

Expected CSV columns:
    - BBL (required)
    - landmark_name or LandmarkName
    - lpc_number or LPCNumber
    - architect or Architect
    - style or ArchitecturalStyle
    - designation_date or DesignationDate
    - landmark_score (your custom score)
    - short_bio or Description
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


def parse_bbl(bbl: str) -> Optional[str]:
    """Normalize BBL format"""
    bbl = str(bbl).strip().replace("-", "").replace(" ", "")
    if len(bbl) == 10 and bbl.isdigit():
        return bbl
    return None


def parse_float(value: str) -> Optional[float]:
    """Safely parse float"""
    try:
        val = float(str(value).strip())
        return val if val != 0 else None
    except (ValueError, AttributeError):
        return None


def parse_date(date_str: str) -> Optional[str]:
    """Parse date string to YYYY-MM-DD format"""
    if not date_str:
        return None

    date_str = str(date_str).strip()

    # Try common formats
    formats = [
        '%Y-%m-%d',
        '%m/%d/%Y',
        '%m-%d-%Y',
        '%Y/%m/%d',
        '%B %d, %Y',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def get_csv_value(row: Dict, *keys: str) -> Optional[str]:
    """Get value from row using multiple possible key names"""
    for key in keys:
        if key in row and row[key]:
            return str(row[key]).strip()
    return None


async def enrich_landmarks_csv(
    csv_path: str,
    dry_run: bool = False,
    create_missing: bool = False
) -> Dict:
    """
    Enrich buildings table with landmark data

    Args:
        csv_path: Path to landmarks CSV
        dry_run: If True, don't actually update data
        create_missing: If True, create buildings that don't exist in PLUTO

    Returns:
        Dict with statistics
    """

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    print(f"📂 Reading landmarks CSV: {csv_path}")

    # Read CSV
    rows = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"📊 Found {len(rows)} rows in CSV")

    # Parse rows
    landmarks = []
    skipped = 0

    for row in rows:
        # Required: BBL
        bbl = parse_bbl(get_csv_value(row, 'BBL', 'bbl'))
        if not bbl:
            skipped += 1
            continue

        # Landmark fields
        landmark_name = get_csv_value(row, 'landmark_name', 'LandmarkName', 'name')
        lpc_number = get_csv_value(row, 'lpc_number', 'LPCNumber', 'LPC_Number')
        architect = get_csv_value(row, 'architect', 'Architect')
        style = get_csv_value(row, 'style', 'ArchitecturalStyle', 'architectural_style')
        historic_period = get_csv_value(row, 'historic_period', 'HistoricPeriod', 'period')
        short_bio = get_csv_value(row, 'short_bio', 'Description', 'description', 'bio')
        designation_date = parse_date(get_csv_value(row, 'designation_date', 'DesignationDate', 'date'))

        # Scoring
        landmark_score = parse_float(get_csv_value(row, 'landmark_score', 'score'))
        final_score = parse_float(get_csv_value(row, 'final_score', 'FinalScore'))

        # Optional location data (for create_missing mode)
        address = get_csv_value(row, 'address', 'Address')
        borough = get_csv_value(row, 'borough', 'Borough')
        lat = parse_float(get_csv_value(row, 'latitude', 'Latitude', 'lat'))
        lng = parse_float(get_csv_value(row, 'longitude', 'Longitude', 'lon', 'lng'))

        landmarks.append({
            'bbl': bbl,
            'landmark_name': landmark_name,
            'lpc_number': lpc_number,
            'architect': architect,
            'architectural_style': style,
            'historic_period': historic_period,
            'short_bio': short_bio,
            'designation_date': designation_date,
            'landmark_score': landmark_score,
            'final_score': final_score,
            # For create_missing
            'address': address,
            'borough': borough,
            'latitude': lat,
            'longitude': lng,
        })

    print(f"✅ Parsed {len(landmarks)} landmarks")
    print(f"⚠️  Skipped {skipped} rows (missing BBL)")

    if dry_run:
        print("🔍 DRY RUN - Not updating data")
        print("\nSample landmark:")
        if landmarks:
            import json
            print(json.dumps(landmarks[0], indent=2, default=str))
        return {
            'total_rows': len(rows),
            'parsed': len(landmarks),
            'skipped': skipped,
            'updated': 0,
            'created': 0,
            'not_found': 0,
            'dry_run': True
        }

    # Update database
    print(f"💾 Updating database...")

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    updated = 0
    created = 0
    not_found = 0
    errors = 0

    try:
        for i, landmark in enumerate(landmarks):
            try:
                # Check if building exists
                existing = session.execute(
                    text("SELECT id, address, latitude, longitude FROM buildings_full_merge_scanning WHERE bbl = :bbl"),
                    {'bbl': landmark['bbl']}
                ).fetchone()

                if existing:
                    # Update existing building
                    session.execute(text("""
                        UPDATE buildings_full_merge_scanning SET
                            is_landmark = TRUE,
                            landmark_name = COALESCE(:landmark_name, landmark_name),
                            lpc_number = COALESCE(:lpc_number, lpc_number),
                            designation_date = COALESCE(:designation_date::date, designation_date),
                            architect = COALESCE(:architect, architect),
                            architectural_style = COALESCE(:architectural_style, architectural_style),
                            historic_period = COALESCE(:historic_period, historic_period),
                            short_bio = COALESCE(:short_bio, short_bio),
                            landmark_score = COALESCE(:landmark_score, landmark_score),
                            final_score = COALESCE(:final_score, final_score),
                            data_source = CASE
                                WHEN 'landmarks' = ANY(data_source) THEN data_source
                                ELSE array_append(data_source, 'landmarks')
                            END,
                            updated_at = NOW()
                        WHERE bbl = :bbl
                    """), landmark)
                    updated += 1

                elif create_missing and landmark['address'] and landmark['latitude'] and landmark['longitude']:
                    # Create new building from landmark data
                    session.execute(text("""
                        INSERT INTO buildings_full_merge_scanning (
                            bbl, address, borough, latitude, longitude,
                            is_landmark, landmark_name, lpc_number, designation_date,
                            architect, architectural_style, historic_period, short_bio,
                            landmark_score, final_score,
                            data_source, scan_enabled
                        ) VALUES (
                            :bbl, :address, :borough, :latitude, :longitude,
                            TRUE, :landmark_name, :lpc_number, :designation_date::date,
                            :architect, :architectural_style, :historic_period, :short_bio,
                            :landmark_score, :final_score,
                            ARRAY['landmarks'], TRUE
                        )
                        ON CONFLICT (bbl) DO NOTHING
                    """), landmark)
                    created += 1

                else:
                    not_found += 1
                    if not_found <= 10:  # Only print first 10
                        print(f"  ⚠️  BBL {landmark['bbl']} not found in buildings table")

                # Progress
                if (i + 1) % 100 == 0:
                    session.commit()
                    print(f"  Processed {i + 1}/{len(landmarks)} landmarks...")

            except Exception as e:
                print(f"⚠️  Error on BBL {landmark['bbl']}: {e}")
                errors += 1

        # Final commit
        session.commit()
        print(f"✅ Database commit successful!")

        # Update final_score for buildings without one
        print("📊 Calculating final_score for buildings...")
        session.execute(text("""
            UPDATE buildings
            SET final_score = COALESCE(
                landmark_score * 1.0,
                CASE WHEN is_landmark THEN 5.0 ELSE 1.0 END
            )
            WHERE final_score IS NULL
        """))
        session.commit()

    except Exception as e:
        session.rollback()
        print(f"❌ Database error: {e}")
        raise

    finally:
        session.close()

    return {
        'total_rows': len(rows),
        'parsed': len(landmarks),
        'skipped': skipped,
        'updated': updated,
        'created': created,
        'not_found': not_found,
        'errors': errors,
        'dry_run': False
    }


def main():
    parser = argparse.ArgumentParser(description='Enrich buildings with landmark data')
    parser.add_argument('--csv', required=True, help='Path to landmarks CSV file')
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    parser.add_argument('--create-missing', action='store_true', help='Create buildings not in PLUTO')

    args = parser.parse_args()

    print("=" * 60)
    print("NYC SCAN - Landmarks Data Enrichment")
    print("=" * 60)
    print(f"CSV File: {args.csv}")
    print(f"Dry Run: {args.dry_run}")
    print(f"Create Missing: {args.create_missing}")
    print("=" * 60)
    print()

    start_time = datetime.now()

    stats = asyncio.run(enrich_landmarks_csv(
        csv_path=args.csv,
        dry_run=args.dry_run,
        create_missing=args.create_missing
    ))

    elapsed = (datetime.now() - start_time).total_seconds()

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total CSV rows:     {stats['total_rows']}")
    print(f"Parsed landmarks:   {stats['parsed']}")
    print(f"Skipped rows:       {stats['skipped']}")
    print(f"Updated buildings:  {stats['updated']}")
    print(f"Created buildings:  {stats['created']}")
    print(f"Not found:          {stats['not_found']}")
    print(f"Errors:             {stats.get('errors', 0)}")
    print(f"Time elapsed:       {elapsed:.1f}s")
    print("=" * 60)

    if not stats['dry_run']:
        print("\n✅ Landmarks enrichment complete!")
        print("\n📊 Next steps:")
        print("   1. Check Supabase: SELECT COUNT(*) FROM buildings_full_merge_scanning WHERE is_landmark = TRUE;")
        print("   2. Run: python scripts/precache_landmarks.py --top-n 100")


if __name__ == '__main__':
    main()

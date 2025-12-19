#!/usr/bin/env python3
"""
Load PLUTO data into Railway for building metadata enrichment.

This creates a lightweight lookup table with essential building info
that can be joined with footprints via BBL.

Usage:
    python scripts/load_pluto_to_railway.py
"""

import csv
import os
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg

PLUTO_CSV = Path(__file__).parent.parent / "data" / "Primary_Land_Use_Tax_Lot_Output__PLUTO__20251001.csv"

# Building class descriptions
BLDG_CLASS_DESCRIPTIONS = {
    'A': 'One Family Dwelling',
    'B': 'Two Family Dwelling',
    'C': 'Walk-up Apartment',
    'D': 'Elevator Apartment',
    'E': 'Warehouse',
    'F': 'Factory/Industrial',
    'G': 'Garage',
    'H': 'Hotel',
    'I': 'Hospital/Health',
    'J': 'Theater',
    'K': 'Store Building',
    'L': 'Loft',
    'M': 'Church/Religious',
    'N': 'Asylum/Home',
    'O': 'Office',
    'P': 'Indoor Recreation',
    'Q': 'Outdoor Recreation',
    'R': 'Condo',
    'S': 'Mixed Residential/Commercial',
    'T': 'Transportation',
    'U': 'Utility',
    'V': 'Vacant Land',
    'W': 'Educational',
    'Y': 'Government',
    'Z': 'Miscellaneous',
}


def main():
    db_url = os.getenv('FOOTPRINTS_DB_URL')
    if not db_url:
        print("ERROR: FOOTPRINTS_DB_URL not set")
        sys.exit(1)

    print("=" * 70)
    print("Loading PLUTO data into Railway")
    print("=" * 70)

    conn = psycopg.connect(db_url)
    cur = conn.cursor()

    # Create table
    print("\nStep 1: Creating pluto_buildings table...")
    cur.execute("""
        DROP TABLE IF EXISTS pluto_buildings;

        CREATE TABLE pluto_buildings (
            bbl TEXT PRIMARY KEY,
            address TEXT,
            postcode TEXT,
            borough TEXT,
            year_built INT,
            num_floors FLOAT,
            bldg_class TEXT,
            bldg_class_desc TEXT,
            land_use TEXT,
            owner_name TEXT,
            bldg_area FLOAT,
            lot_area FLOAT,
            units_res INT,
            units_total INT,
            zoning TEXT
        );
    """)
    conn.commit()
    print("   Table created")

    # Load data
    print("\nStep 2: Loading PLUTO data...")

    with open(PLUTO_CSV, 'r') as f:
        reader = csv.DictReader(f)

        batch = []
        batch_size = 5000
        total = 0

        for row in reader:
            # Use BBL column directly
            bbl = row.get('BBL', '').strip()
            borough = row.get('borough', '').strip()

            if not bbl:
                continue

            # Get building class description
            bldg_class = row.get('bldgclass', '').strip()
            bldg_class_desc = BLDG_CLASS_DESCRIPTIONS.get(bldg_class[:1] if bldg_class else '', '')

            # Parse numeric fields
            def safe_int(val):
                try:
                    return int(float(val)) if val and val.strip() else None
                except:
                    return None

            def safe_float(val):
                try:
                    return float(val) if val and val.strip() else None
                except:
                    return None

            batch.append((
                bbl,
                row.get('address', '').strip() or None,
                row.get('postcode', '').strip() or None,
                borough,
                safe_int(row.get('yearbuilt')),
                safe_float(row.get('numfloors')),
                bldg_class or None,
                bldg_class_desc or None,
                row.get('landuse', '').strip() or None,
                row.get('ownername', '').strip() or None,
                safe_float(row.get('bldgarea')),
                safe_float(row.get('lotarea')),
                safe_int(row.get('unitsres')),
                safe_int(row.get('unitstotal')),
                row.get('zonedist1', '').strip() or None,
            ))

            if len(batch) >= batch_size:
                cur.executemany("""
                    INSERT INTO pluto_buildings
                    (bbl, address, postcode, borough, year_built, num_floors,
                     bldg_class, bldg_class_desc, land_use, owner_name,
                     bldg_area, lot_area, units_res, units_total, zoning)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bbl) DO NOTHING
                """, batch)
                conn.commit()
                total += len(batch)
                print(f"   Loaded {total:,} rows...")
                batch = []

        # Final batch
        if batch:
            cur.executemany("""
                INSERT INTO pluto_buildings
                (bbl, address, postcode, borough, year_built, num_floors,
                 bldg_class, bldg_class_desc, land_use, owner_name,
                 bldg_area, lot_area, units_res, units_total, zoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bbl) DO NOTHING
            """, batch)
            conn.commit()
            total += len(batch)

    print(f"   ✅ Loaded {total:,} PLUTO records")

    # Create index
    print("\nStep 3: Creating index...")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pluto_bbl ON pluto_buildings(bbl);")
    cur.execute("ANALYZE pluto_buildings;")
    conn.commit()
    print("   ✅ Index created")

    # Test query
    print("\nStep 4: Testing lookup...")
    cur.execute("""
        SELECT bbl, address, year_built, num_floors, bldg_class_desc
        FROM pluto_buildings
        WHERE bbl = '4001990045'
    """)
    row = cur.fetchone()
    if row:
        print(f"   BBL 4001990045: {row[1]}, {row[2]}, {row[3]} floors, {row[4]}")
    else:
        print("   BBL not found (expected for some BBLs)")

    # Check size
    cur.execute("SELECT COUNT(*) FROM pluto_buildings")
    count = cur.fetchone()[0]
    print(f"\n✅ Total: {count:,} buildings in pluto_buildings table")

    cur.close()
    conn.close()

    print("\n" + "=" * 70)
    print("PLUTO LOAD COMPLETE")
    print("=" * 70)


if __name__ == '__main__':
    main()

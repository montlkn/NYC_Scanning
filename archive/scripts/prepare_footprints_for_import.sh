#!/bin/bash
# Prepare and import building footprints using psql COPY
# This is the fastest method - bypasses Python entirely

set -e

# Load environment variables
if [ -f "../.env" ]; then
    export $(cat ../.env | grep -v '^#' | grep -v '^$' | xargs)
else
    echo "❌ Error: .env file not found in backend directory"
    exit 1
fi

# Verify DATABASE_URL is set
if [ -z "$DATABASE_URL" ]; then
    echo "❌ Error: DATABASE_URL not set in .env"
    exit 1
fi

CSV_PATH="../data/BUILDING_20251104.csv"
TEMP_CSV="/tmp/footprints_clean.csv"

echo "========================================================================"
echo "NYC SCAN - Fast Footprint Import (Direct psql)"
echo "========================================================================"
echo ""

# Step 1: Clean the CSV (remove commas from numbers)
echo "Step 1: Cleaning CSV and extracting needed columns..."
python3 << 'PYTHON_SCRIPT'
import csv
import sys

input_csv = "../data/BUILDING_20251104.csv"
output_csv = "/tmp/footprints_clean.csv"

def clean_num(val):
    if not val or val.strip() == '':
        return ''
    return val.strip().replace(',', '')

with open(input_csv, 'r') as infile, open(output_csv, 'w', newline='') as outfile:
    reader = csv.DictReader(infile)
    writer = csv.writer(outfile)

    # Write header
    writer.writerow(['bin', 'bbl', 'name', 'footprint_wkt', 'height_roof', 'ground_elevation',
                     'shape_area', 'construction_year', 'feature_code', 'geometry_source', 'last_edited_date'])

    count = 0
    for row in reader:
        bin_val = row.get('BIN', '').strip()
        footprint_wkt = row.get('the_geom', '').strip()

        if not bin_val or not footprint_wkt:
            continue

        writer.writerow([
            bin_val,
            row.get('BASE_BBL', '').strip(),
            row.get('NAME', '').strip(),
            footprint_wkt,
            clean_num(row.get('Height Roof')),
            clean_num(row.get('Ground Elevation')),
            clean_num(row.get('SHAPE_AREA')),
            clean_num(row.get('Construction Year')),
            clean_num(row.get('Feature Code')),
            row.get('Geometry Source', '').strip(),
            row.get('LAST_EDITED_DATE', '').strip(),
        ])

        count += 1
        if count % 100000 == 0:
            print(f"  Processed {count:,} rows...", file=sys.stderr)

    print(f"✅ Cleaned {count:,} rows", file=sys.stderr)

PYTHON_SCRIPT

echo ""
echo "Step 2: Importing into PostgreSQL..."
echo ""

# Use psql COPY - this is as fast as it gets
psql $DATABASE_URL << 'SQL'
-- Disable timeout
SET statement_timeout = 0;

-- Create temp staging table
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

-- COPY from file (fastest method)
\COPY footprints_staging FROM '/tmp/footprints_clean.csv' WITH CSV HEADER

-- Show progress
SELECT COUNT(*) || ' rows loaded into staging' FROM footprints_staging;

-- Transform and insert (this is the slow part - geometry parsing)
-- Use DISTINCT ON to handle duplicate BINs (keep first occurrence)
\timing on
INSERT INTO building_footprints (
    bin, bbl, name,
    footprint, centroid,
    height_roof, ground_elevation, shape_area,
    construction_year, feature_code, geometry_source,
    last_edited_date
)
SELECT DISTINCT ON (bin)
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
ORDER BY bin, last_edited_date DESC NULLS LAST
ON CONFLICT (bin) DO UPDATE SET
    bbl = EXCLUDED.bbl,
    name = COALESCE(EXCLUDED.name, building_footprints.name),
    footprint = EXCLUDED.footprint,
    centroid = EXCLUDED.centroid,
    height_roof = COALESCE(EXCLUDED.height_roof, building_footprints.height_roof),
    ground_elevation = COALESCE(EXCLUDED.ground_elevation, building_footprints.ground_elevation),
    shape_area = COALESCE(EXCLUDED.shape_area, building_footprints.shape_area),
    construction_year = COALESCE(EXCLUDED.construction_year, building_footprints.construction_year),
    updated_at = NOW();

-- Analyze for query optimization
ANALYZE building_footprints;

-- Verify
SELECT COUNT(*) || ' buildings in building_footprints' FROM building_footprints;

-- Test query
SELECT COUNT(*) || ' buildings found in Empire State Building test cone'
FROM find_buildings_in_cone(40.748817, -73.985428, 0, 100, 60, 10);

SQL

echo ""
echo "========================================================================"
echo "✅ IMPORT COMPLETE"
echo "========================================================================"
echo "Next: python scripts/test_scan_v2.py"
echo ""

# Cleanup
rm -f /tmp/footprints_clean.csv

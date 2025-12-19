#!/bin/bash
# Import building footprints to Railway PostGIS database
set -e

# Load environment variables
if [ -f "../.env" ]; then
    export $(cat ../.env | grep -v '^#' | grep -v '^$' | xargs)
else
    echo "❌ Error: .env file not found in backend directory"
    exit 1
fi

# Verify FOOTPRINTS_DB_URL is set
if [ -z "$FOOTPRINTS_DB_URL" ]; then
    echo "❌ Error: FOOTPRINTS_DB_URL not set in .env"
    exit 1
fi

CSV_PATH="../data/BUILDING_20251104.csv"
TEMP_CSV="/tmp/footprints_clean.csv"

echo "========================================================================"
echo "NYC SCAN - Import to Railway PostGIS"
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
    writer.writerow(['bin', 'bbl', 'footprint_wkt', 'height_roof', 'ground_elevation', 'shape_area'])

    count = 0
    for row in reader:
        bin_val = row.get('BIN', '').strip()
        footprint_wkt = row.get('the_geom', '').strip()

        if not bin_val or not footprint_wkt:
            continue

        writer.writerow([
            bin_val,
            row.get('BASE_BBL', '').strip(),
            footprint_wkt,
            clean_num(row.get('Height Roof')),
            clean_num(row.get('Ground Elevation')),
            clean_num(row.get('SHAPE_AREA')),
        ])

        count += 1
        if count % 100000 == 0:
            print(f"  Processed {count:,} rows...", file=sys.stderr)

    print(f"✅ Cleaned {count:,} rows", file=sys.stderr)

PYTHON_SCRIPT

echo ""
echo "Step 2: Importing into Railway PostgreSQL..."
echo ""

# Use psql COPY - this is as fast as it gets
psql $FOOTPRINTS_DB_URL << 'SQL'
-- Disable timeout
SET statement_timeout = 0;

-- Create temp staging table
CREATE TEMP TABLE footprints_staging (
    bin TEXT,
    bbl TEXT,
    footprint_wkt TEXT,
    height_roof FLOAT,
    ground_elevation FLOAT,
    shape_area FLOAT
);

-- COPY from file (fastest method)
\COPY footprints_staging FROM '/tmp/footprints_clean.csv' WITH CSV HEADER

-- Show progress
SELECT COUNT(*) || ' rows loaded into staging' FROM footprints_staging;

-- Transform and insert (this is the slow part - geometry parsing)
-- Use DISTINCT ON to handle duplicate BINs (keep first occurrence)
\timing on
INSERT INTO building_footprints (
    bin, bbl,
    footprint, centroid,
    height_roof, ground_elevation, shape_area
)
SELECT DISTINCT ON (bin)
    bin, bbl,
    ST_GeomFromText(footprint_wkt, 4326),
    ST_Centroid(ST_GeomFromText(footprint_wkt, 4326)),
    height_roof, ground_elevation, shape_area
FROM footprints_staging
ORDER BY bin
ON CONFLICT (bin) DO UPDATE SET
    bbl = EXCLUDED.bbl,
    footprint = EXCLUDED.footprint,
    centroid = EXCLUDED.centroid,
    height_roof = COALESCE(EXCLUDED.height_roof, building_footprints.height_roof),
    ground_elevation = COALESCE(EXCLUDED.ground_elevation, building_footprints.ground_elevation),
    shape_area = COALESCE(EXCLUDED.shape_area, building_footprints.shape_area),
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
echo ""

# Cleanup
rm -f /tmp/footprints_clean.csv

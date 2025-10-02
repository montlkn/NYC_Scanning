# NYC Scan - Data Ingestion Guide

Complete workflow for loading your PLUTO and landmarks data.

---

## Overview

You have:
- ✅ **PLUTO CSV** - Full NYC building footprints with floor counts
- ✅ **Landmarks CSV** - Your pruned landmark dataset with scores

We're creating:
- 🗄️ **Unified `buildings` table** - Single source of truth combining both datasets
- 📊 **Key fields**: `num_floors` (for barometer), `landmark_score`, BBL, location

---

## Quick Start (30 minutes)

### 1. Place Your CSVs

```bash
cp /path/to/your/pluto.csv /Users/lucienmount/coding/nyc_scan/backend/data/
cp /path/to/your/landmarks.csv /Users/lucienmount/coding/nyc_scan/backend/data/
```

### 2. Run Database Migration

```bash
cd /Users/lucienmount/coding/nyc_scan

# Run migration to create unified buildings table
psql $DATABASE_URL < backend/migrations/002_create_unified_buildings_table.sql
```

### 3. Ingest PLUTO Data

```bash
cd backend
source venv/bin/activate

# Test with dry-run first
python scripts/ingest_pluto.py --csv data/pluto.csv --dry-run --limit 100

# Run full ingest (takes ~5-10 minutes for 860k buildings)
python scripts/ingest_pluto.py --csv data/pluto.csv
```

### 4. Enrich with Landmarks

```bash
# Test with dry-run
python scripts/enrich_landmarks.py --csv data/landmarks.csv --dry-run

# Run full enrichment
python scripts/enrich_landmarks.py --csv data/landmarks.csv
```

### 5. Verify Data

```bash
psql $DATABASE_URL -c "
SELECT
  COUNT(*) as total_buildings,
  COUNT(*) FILTER (WHERE is_landmark) as landmarks,
  COUNT(*) FILTER (WHERE num_floors IS NOT NULL) as with_floors
FROM buildings;
"
```

Expected output:
```
 total_buildings | landmarks | with_floors
-----------------+-----------+-------------
          860000 |      5000 |      750000
```

---

## Database Schema

### Unified Buildings Table

```sql
CREATE TABLE buildings (
    -- Identifiers
    bbl VARCHAR(10) PRIMARY KEY,
    bin VARCHAR(7),

    -- Location
    address TEXT NOT NULL,
    borough VARCHAR(20),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    geom GEOMETRY(POINT, 4326),  -- PostGIS

    -- Physical (from PLUTO)
    num_floors INTEGER,           -- KEY: For barometer validation!
    year_built INTEGER,
    building_class VARCHAR(10),
    land_use VARCHAR(10),
    lot_area DOUBLE PRECISION,
    building_area DOUBLE PRECISION,

    -- Landmark Data
    is_landmark BOOLEAN,
    landmark_name TEXT,
    lpc_number VARCHAR(20),
    designation_date DATE,
    architect TEXT,
    architectural_style TEXT,
    historic_period TEXT,
    short_bio TEXT,

    -- Scoring
    landmark_score DOUBLE PRECISION,  -- Your custom score
    final_score DOUBLE PRECISION,     -- Combined priority

    -- Metadata
    scan_enabled BOOLEAN,
    has_reference_images BOOLEAN,
    data_source TEXT[],               -- ['pluto', 'landmarks']
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);
```

---

## Data Flow

```
Step 1: PLUTO CSV (860k rows)
        ↓
    ingest_pluto.py
        ↓
    buildings table (base data)
        - BBL, address, lat/lng
        - num_floors ← CRITICAL
        - year_built, building_class
        - is_landmark = FALSE
        - data_source = ['pluto']

Step 2: Landmarks CSV (5k rows)
        ↓
    enrich_landmarks.py
        ↓
    buildings table (enriched)
        - is_landmark = TRUE
        - landmark_name, architect, style
        - landmark_score, final_score
        - data_source = ['pluto', 'landmarks']
```

---

## CSV Requirements

### PLUTO CSV

**Required columns:**
- `BBL` - 10-digit Borough-Block-Lot
- `Address` - Street address
- `Borough` - Code (1-5) or name
- `Latitude` - Decimal degrees
- `Longitude` - Decimal degrees

**Important optional:**
- `NumFloors` - **Critical for barometer floor detection!**
- `YearBuilt` - Building age
- `BldgClass` - PLUTO class code
- `ZipCode` - Zip code

**Example row:**
```csv
BBL,Address,Borough,Latitude,Longitude,NumFloors,YearBuilt,BldgClass
1000010101,"100 BROADWAY","Manhattan",40.7074,-74.0113,15,1902,"D1"
```

### Landmarks CSV

**Required columns:**
- `BBL` - Must match PLUTO BBL

**Optional columns:**
- `landmark_name` or `LandmarkName`
- `architect` or `Architect`
- `style` or `ArchitecturalStyle`
- `landmark_score` - Your custom score (0-100)
- `final_score` - Overall priority
- `short_bio` or `Description`
- `designation_date` - Any common date format

**If using --create-missing:**
- `address` - Street address
- `borough` - Borough name
- `latitude` - Decimal degrees
- `longitude` - Decimal degrees

**Example row:**
```csv
BBL,landmark_name,architect,style,landmark_score,short_bio
1000010101,"Woolworth Building","Cass Gilbert","Gothic Revival",95,"Iconic NYC skyscraper, tallest in world 1913-1930"
```

---

## Verification Queries

### Check Total Buildings
```sql
SELECT COUNT(*) FROM buildings;
-- Expected: ~860k
```

### Check Landmarks
```sql
SELECT COUNT(*) FROM buildings WHERE is_landmark = TRUE;
-- Expected: Matches your landmarks CSV row count
```

### Check Floor Data (Critical!)
```sql
SELECT
  COUNT(*) as total,
  COUNT(*) FILTER (WHERE num_floors IS NOT NULL) as with_floors,
  ROUND(100.0 * COUNT(*) FILTER (WHERE num_floors IS NOT NULL) / COUNT(*), 1) as pct
FROM buildings;
```

Expected:
```
 total  | with_floors | pct
--------+-------------+-----
 860000 |      750000 | 87.2
```

⚠️ **If `pct < 80%`, check your PLUTO CSV has `NumFloors` column!**

### Check Top Scored Buildings
```sql
SELECT
  bbl,
  address,
  borough,
  num_floors,
  is_landmark,
  landmark_score,
  final_score
FROM buildings
WHERE scan_enabled = TRUE
ORDER BY final_score DESC NULLS LAST
LIMIT 20;
```

### Check Data Sources
```sql
SELECT
  is_landmark,
  data_source,
  COUNT(*)
FROM buildings
GROUP BY is_landmark, data_source;
```

Expected:
```
 is_landmark |     data_source      | count
-------------+----------------------+--------
 false       | {pluto}              | 855000
 true        | {pluto,landmarks}    |   5000
```

---

## Troubleshooting

### Issue: "BBL not found" during landmarks enrichment

**Cause:** Landmark BBL doesn't exist in PLUTO dataset

**Solution 1:** Use `--create-missing` (if your landmarks CSV has location data):
```bash
python scripts/enrich_landmarks.py --csv data/landmarks.csv --create-missing
```

**Solution 2:** Manually check which BBLs are missing:
```bash
# Create temp table with landmark BBLs
psql $DATABASE_URL << EOF
CREATE TEMP TABLE landmark_bbls (bbl VARCHAR(10));
\copy landmark_bbls FROM 'data/landmarks.csv' WITH (FORMAT csv, HEADER);

-- Find missing BBLs
SELECT bbl FROM landmark_bbls
WHERE bbl NOT IN (SELECT bbl FROM buildings);
EOF
```

### Issue: Most buildings have `num_floors = NULL`

**Cause:** PLUTO CSV missing `NumFloors` column

**Impact:** ⚠️ **Critical!** Barometer floor detection won't work

**Solution:**
1. Download correct PLUTO CSV with floor data
2. Re-run ingestion
3. Or manually update from NYC Open Data

### Issue: Duplicate key violation on BBL

**Cause:** BBL appears multiple times in CSV

**Solution:** Find duplicates:
```bash
python -c "
import csv
from collections import Counter
with open('data/pluto.csv') as f:
    bbls = [row['BBL'] for row in csv.DictReader(f)]
    dupes = [bbl for bbl, count in Counter(bbls).items() if count > 1]
    print('\n'.join(dupes[:10]))
"
```

### Issue: Coordinates are (0, 0) or NULL

**Cause:** Invalid lat/lng in CSV

**Solution:** Scripts skip these automatically. Check skipped count:
```
Parsed 860000 valid buildings
⚠️  Skipped 5000 rows (missing BBL/lat/lng/address)
```

---

## Next Steps

After successful ingestion:

### 1. Update Backend Code

Update any references from `buildings_duplicate` → `buildings`:

```bash
cd backend
grep -r "buildings_duplicate" --include="*.py" .
# Should return no results (code already uses correct table)
```

### 2. Test Locally

```bash
cd backend
source venv/bin/activate
python main.py

# In another terminal:
curl "http://localhost:8000/api/debug/test-geospatial?lat=40.7074&lng=-74.0113&bearing=180"
```

### 3. Pre-cache Top Landmarks

```bash
python scripts/precache_landmarks.py --top-n 100
```

### 4. Deploy to Render

Follow [RENDER_DEPLOY.md](RENDER_DEPLOY.md)

---

## Data Maintenance

### Re-running Ingestion

To completely refresh data:

```sql
-- Backup first!
CREATE TABLE buildings_backup AS SELECT * FROM buildings;

-- Delete all buildings
TRUNCATE TABLE buildings CASCADE;

-- Re-run scripts
-- (same steps as above)
```

### Updating Landmark Scores

```bash
# Update scores in your landmarks CSV
# Then re-run enrichment (won't create duplicates)
python scripts/enrich_landmarks.py --csv data/landmarks.csv
```

### Adding New Buildings

```sql
INSERT INTO buildings (
    bbl, address, borough, latitude, longitude,
    is_landmark, landmark_name, scan_enabled, data_source
) VALUES (
    '1000010102',
    '102 Broadway',
    'Manhattan',
    40.7075,
    -74.0114,
    FALSE,
    NULL,
    TRUE,
    ARRAY['manual']
);
```

---

## Summary Checklist

- [ ] CSVs placed in `backend/data/`
- [ ] Migration 002 run in Supabase
- [ ] PLUTO ingestion complete (~860k buildings)
- [ ] Landmarks enrichment complete (~5k landmarks)
- [ ] Verified `num_floors` data exists (>80%)
- [ ] Tested geospatial queries locally
- [ ] Ready to deploy to Render

---

**Next:** Follow [RENDER_DEPLOY.md](RENDER_DEPLOY.md) to deploy! 🚀

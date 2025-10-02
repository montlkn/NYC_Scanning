# Data Ingestion Scripts

Scripts for loading NYC building data into the unified `buildings` table.

## Quick Start

### 1. Prepare Your CSVs

Place your CSV files in `backend/data/`:

```bash
backend/data/
  ├── pluto.csv         # MapPLUTO dataset
  └── landmarks.csv     # Your pruned NYC landmarks
```

### 2. Run Database Migration

Run the migration to create the unified buildings table:

```bash
psql $DATABASE_URL < backend/migrations/002_create_unified_buildings_table.sql
```

### 3. Ingest PLUTO Data

Load base building data from PLUTO:

```bash
cd backend
source venv/bin/activate

# Test first with dry-run
python scripts/ingest_pluto.py --csv data/pluto.csv --dry-run --limit 100

# Then run full ingest
python scripts/ingest_pluto.py --csv data/pluto.csv
```

### 4. Enrich with Landmarks

Add landmark metadata to buildings:

```bash
# Test first
python scripts/enrich_landmarks.py --csv data/landmarks.csv --dry-run

# Then run full enrichment
python scripts/enrich_landmarks.py --csv data/landmarks.csv

# If landmarks CSV has location data and you want to create missing buildings:
python scripts/enrich_landmarks.py --csv data/landmarks.csv --create-missing
```

---

## Script Details

### `ingest_pluto.py`

Loads MapPLUTO data into `buildings` table.

**Required CSV columns:**
- `BBL` - Borough-Block-Lot (10 digits)
- `Address` - Street address
- `Borough` - Borough code (1-5) or name
- `Latitude` - Latitude coordinate
- `Longitude` - Longitude coordinate

**Optional CSV columns:**
- `NumFloors` - Number of floors (**important for barometer validation**)
- `YearBuilt` - Year constructed
- `BldgClass` - PLUTO building class
- `LandUse` - Land use code
- `LotArea` - Lot square footage
- `BldgArea` - Building square footage
- `ZipCode` - Zip code
- `BIN` - Building Identification Number

**Options:**
```bash
--csv PATH            Path to PLUTO CSV (required)
--dry-run             Preview without inserting
--limit N             Process only N rows
--no-skip-existing    Try to insert all (may cause conflicts)
```

**Example:**
```bash
# Process first 1000 rows only
python scripts/ingest_pluto.py --csv data/pluto.csv --limit 1000

# Full run
python scripts/ingest_pluto.py --csv data/pluto.csv
```

---

### `enrich_landmarks.py`

Enriches `buildings` table with landmark metadata from your pruned CSV.

**Required CSV columns:**
- `BBL` - Must match existing building

**Optional CSV columns:**
- `landmark_name` or `LandmarkName` - Official name
- `lpc_number` or `LPCNumber` - Landmarks Preservation Commission number
- `architect` or `Architect` - Architect name
- `style` or `ArchitecturalStyle` - Architectural style
- `historic_period` or `HistoricPeriod` - Historic period
- `short_bio` or `Description` - Short description for UI
- `designation_date` or `DesignationDate` - When designated (any common date format)
- `landmark_score` - Your custom importance score
- `final_score` - Overall priority score

**For --create-missing mode (creates buildings not in PLUTO):**
- `address` - Street address
- `borough` - Borough name
- `latitude` - Latitude coordinate
- `longitude` - Longitude coordinate

**Options:**
```bash
--csv PATH          Path to landmarks CSV (required)
--dry-run           Preview without updating
--create-missing    Create buildings not found in PLUTO
```

**Example:**
```bash
# Update existing buildings only
python scripts/enrich_landmarks.py --csv data/landmarks.csv

# Create missing landmarks too
python scripts/enrich_landmarks.py --csv data/landmarks.csv --create-missing
```

---

## Data Flow

```
1. PLUTO CSV → buildings table (base data)
   ├─ BBL, address, lat/lng
   ├─ num_floors (KEY for barometer)
   ├─ year_built, building_class
   └─ data_source: ['pluto']

2. Landmarks CSV → buildings table (enrichment)
   ├─ is_landmark = TRUE
   ├─ landmark_name, architect, style
   ├─ landmark_score, final_score
   └─ data_source: ['pluto', 'landmarks']
```

---

## Expected Results

After running both scripts:

```sql
-- Check total buildings
SELECT COUNT(*) FROM buildings;
-- Should be ~860k (all PLUTO parcels)

-- Check landmarks
SELECT COUNT(*) FROM buildings WHERE is_landmark = TRUE;
-- Should match your landmarks CSV count

-- Check buildings with num_floors (important!)
SELECT COUNT(*) FROM buildings WHERE num_floors IS NOT NULL;
-- Should be majority of buildings

-- Check top scored buildings
SELECT bbl, address, final_score, is_landmark, num_floors
FROM buildings
WHERE scan_enabled = TRUE
ORDER BY final_score DESC NULLS LAST
LIMIT 20;
```

---

## Troubleshooting

### Issue: "CSV not found"

**Solution:** Check path is relative to `backend/` directory:

```bash
# Correct
python scripts/ingest_pluto.py --csv data/pluto.csv

# Wrong
python scripts/ingest_pluto.py --csv /full/path/to/pluto.csv
```

### Issue: "BBL not found" during landmarks enrichment

**Solution:** Run PLUTO ingestion first, or use `--create-missing`:

```bash
python scripts/enrich_landmarks.py --csv data/landmarks.csv --create-missing
```

### Issue: Duplicate key violation

**Solution:** Buildings table uses BBL as unique key. If you need to re-run:

```sql
-- Delete all buildings
TRUNCATE TABLE buildings CASCADE;

-- Then re-run scripts
```

### Issue: num_floors is NULL for most buildings

**Solution:** Check your PLUTO CSV has `NumFloors` column. This is critical for barometer validation!

---

## CSV Column Mappings

The scripts are flexible with column names:

### PLUTO Script
- BBL: `BBL`, `bbl`
- Address: `Address`, `address`
- Borough: `Borough`, `borough`
- Latitude: `Latitude`, `lat`
- Longitude: `Longitude`, `lon`, `lng`
- NumFloors: `NumFloors`, `num_floors`
- YearBuilt: `YearBuilt`, `year_built`

### Landmarks Script
- BBL: `BBL`, `bbl`
- Landmark Name: `landmark_name`, `LandmarkName`, `name`
- LPC Number: `lpc_number`, `LPCNumber`, `LPC_Number`
- Architect: `architect`, `Architect`
- Style: `style`, `ArchitecturalStyle`, `architectural_style`
- Score: `landmark_score`, `score`
- Bio: `short_bio`, `Description`, `description`, `bio`

---

## Next Steps

After ingestion:

1. **Verify data:**
   ```bash
   psql $DATABASE_URL -c "SELECT COUNT(*) FROM buildings;"
   psql $DATABASE_URL -c "SELECT COUNT(*) FROM buildings WHERE num_floors IS NOT NULL;"
   ```

2. **Update image flags:**
   ```sql
   SELECT public.update_building_reference_flags();
   ```

3. **Pre-cache Street View images:**
   ```bash
   python scripts/precache_landmarks.py --top-n 100
   ```

4. **Deploy backend:**
   - Continue to Render deployment
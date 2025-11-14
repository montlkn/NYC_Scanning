# Data Pipeline Documentation

Complete guide to the NYC Scan data ingestion and processing pipeline.

## Table of Contents

- [Overview](#overview)
- [Data Sources](#data-sources)
- [Pipeline Stages](#pipeline-stages)
- [Running the Pipeline](#running-the-pipeline)
- [Data Quality](#data-quality)
- [Troubleshooting](#troubleshooting)

---

## Overview

The NYC Scan data pipeline transforms raw NYC building data into a production-ready system for building identification. The process involves:

1. **Data Ingestion** - Load raw datasets
2. **Enrichment** - Add landmark metadata
3. **Geocoding** - Convert coordinates
4. **Scoring** - Calculate building importance
5. **Image Caching** - Fetch Street View panoramas
6. **Embedding Generation** - Pre-compute CLIP vectors
7. **Database Loading** - Import into production DB

**Timeline:** ~6-8 hours for full pipeline (mostly Street View API calls)
**Cost:** ~$5-10 for Street View images (top 100 buildings)

---

## Data Sources

### 1. NYC PLUTO Dataset

**Source:** [NYC Open Data - Primary Land Use Tax Lot Output](https://data.cityofnewyork.us/City-Government/Primary-Land-Use-Tax-Lot-Output-PLUTO/64uk-42ks)

**Size:** 306MB CSV (~860,000 buildings)
**Update Frequency:** Annual

**Key Fields:**
- `BBL`: Borough-Block-Lot identifier
- `Address`: Street address
- `BldgArea`: Building area in sq ft
- `NumFloors`: Number of floors
- `YearBuilt`: Year of construction
- `OwnerName`: Property owner
- `LandUse`: Zoning classification
- `Coordinates`: State Plane projection

**Download:**
```bash
# Manual download from NYC Open Data portal
# Save to: backend/data/pluto/pluto_24v1.csv
```

### 2. NYC Landmarks Database

**Source:** [NYC Landmarks Preservation Commission](https://data.cityofnewyork.us/Housing-Development/LPC-Individual-Landmarks/ch5p-r223)

**Size:** 3MB CSV (~5,000 landmarks)
**Update Frequency:** Quarterly

**Key Fields:**
- `LM_NAME`: Landmark name
- `DESIG_ADDR`: Designated address
- `ARCHITECT`: Architect/designer
- `STYLE`: Architectural style
- `DATE_LOW`/`DATE_HIGH`: Construction date range
- `HIST_DIST`: Historic district
- `Coordinates`: Lat/Lng

**Download:**
```bash
# API endpoint
curl -o backend/data/landmarks/landmarks.csv \
  "https://data.cityofnewyork.us/resource/ch5p-r223.csv?$limit=10000"
```

### 3. Final Dataset (New - November 2024)

**Source:** Internal analysis combining PLUTO + Landmarks + Walk Scores + ML Scoring

**Location:** `/backend/data/final/`

**Files:**
- `full_dataset.csv` - 37,296 buildings
- `top_100.csv` - Top 100 buildings by `final_score`
- `top_500.csv` - Top 500
- `top_1000.csv` - Top 1,000
- `top_4000.csv` - Top 4,000
- `full_dataset.geojson` - GeoJSON format
- `summary_stats.json` - Dataset statistics

**Key Fields** (161 total columns):
- **Identifiers**: `building_name`, `address`, `bbl`, `bin`
- **Location**: `geocoded_lat`, `geocoded_lng`, `geometry` (WKT)
- **Physical**: `height`, `year_built`, `num_floors`
- **Architectural**: `architect`, `style`, `style_prim`, `style_family`
- **Scoring**: `final_score` (0-100), `ml_score`, `walk_score`
- **Aesthetic**: `classicist_score`, `modernist_score`, `visionary_score`, etc.
- **Rarity**: `arch_rarity`, `style_rarity`, `material_rarity`
- **Contextual**: `contextual_surprise`, `local_era_contrast`

**Sample Row:**
```csv
Lever House,390 Park Avenue,Manhattan,1012890036.0,1035732.0,269.43,1950.0,
"skidmore, owings & merrill [gordon bunshaft]",international style,22.0,
LPC_LANDMARK,40.759607,-73.972872,...,100.0
```

### 4. Google Street View (On-Demand)

**Source:** Google Maps Street View Static API

**Endpoint:** `https://maps.googleapis.com/maps/api/streetview`

**Cost:** $0.007 per image
**Usage:** 8 angles × 100 buildings = 800 images = $5.60

**Request Parameters:**
```
location: 40.7484,-73.9857
heading: 0-360 (compass bearing)
size: 640x640
fov: 90 (field of view)
pitch: 10 (slight upward tilt)
key: YOUR_API_KEY
```

---

## Pipeline Stages

### Stage 1: Preprocess Landmarks

**Script:** `backend/scripts/preprocess_landmarks.py`

**Purpose:** Convert State Plane coordinates to WGS84 (lat/lng)

**Input:** `data/landmarks/landmarks_raw.csv`
**Output:** `data/landmarks/landmarks_geocoded.csv`

**Process:**
1. Read raw landmark CSV
2. Convert NY State Plane (EPSG:2263) → WGS84 (EPSG:4326)
3. Validate coordinates (within NYC bounds)
4. Add geocoding metadata

**Run:**
```bash
cd backend
python scripts/preprocess_landmarks.py

# Expected output:
# ✓ Processed 5,342 landmarks
# ✓ 5,340 successfully geocoded (99.96%)
# ✓ 2 failed (invalid coordinates)
```

---

### Stage 2: Ingest PLUTO Buildings

**Script:** `backend/scripts/ingest_pluto.py`

**Purpose:** Load 860k NYC buildings into Main Supabase DB

**Input:** `data/pluto/pluto_24v1.csv`
**Output:** Main DB `buildings_full_merge_scanning` table

**Process:**
1. Read PLUTO CSV in chunks (10k rows)
2. Normalize addresses
3. Convert coordinates
4. Create PostGIS geometry
5. Batch insert to database

**Run:**
```bash
cd backend
export DATABASE_URL="postgresql://..."
python scripts/ingest_pluto.py

# Expected output:
# Processing chunk 1/86...
# Processing chunk 2/86...
# ...
# ✓ Inserted 860,245 buildings
# ⏱ Duration: 45 minutes
```

---

### Stage 3: Enrich with Landmarks

**Script:** `backend/scripts/enrich_landmarks.py`

**Purpose:** Merge landmark metadata with PLUTO buildings

**Input:**
- Main DB `buildings_full_merge_scanning`
- `data/landmarks/landmarks_geocoded.csv`

**Output:** Updated `buildings_full_merge_scanning` with landmark data

**Process:**
1. Fuzzy match addresses (Levenshtein distance)
2. Update `is_landmark`, `landmark_name`, `architect`, `style`
3. Log unmatched landmarks

**Run:**
```bash
cd backend
python scripts/enrich_landmarks.py

# Expected output:
# ✓ Matched 4,987 landmarks (93.3%)
# ⚠ Unmatched: 355 (see unmatched_landmarks.csv)
```

---

### Stage 4: Import Final Dataset (Top 100)

**Script:** `backend/scripts/import_top100_from_csv.py` (or create new one for final dataset)

**Purpose:** Import top 100 buildings from new `final/top_100.csv` into Phase 1 DB

**Input:** `data/final/top_100.csv`
**Output:** Phase 1 DB `buildings` table

**Process:**
1. Read top_100.csv
2. Map columns:
   - `building_name` → `build_nme`
   - `address` → `des_addres`
   - `style` → `style_prim`
   - `geometry` (WKT) → PostGIS MULTIPOLYGON
3. Set `tier = 1`
4. Insert into Phase 1 DB

**Column Mapping:**

| CSV Column | DB Column | Transformation |
|------------|-----------|----------------|
| `bbl` | `bbl` | Cast to TEXT |
| `building_name` | `build_nme` | Direct |
| `address` | `des_addres` | Direct |
| `style_prim` | `style_prim` | Direct |
| `num_floors` or `NumFloors` | `num_floors` | Take first non-null |
| `final_score` | `final_score` | Direct (0-100 scale) |
| `geometry` | `geom` | ST_GeomFromText(geometry, 4326) |
| - | `center` | ST_Centroid(geom) |
| - | `tier` | Hardcode: 1 |

**Run:**
```bash
cd backend
export SCAN_DB_URL="postgresql://..."
python scripts/import_top100_from_csv.py

# Expected output:
# Reading data/final/top_100.csv...
# ✓ Found 100 buildings
# Processing: Lever House (BBL: 1012890036.0)
# Processing: Seagram Building (BBL: 1013070001.0)
# ...
# ✓ Inserted 100 buildings into Phase 1 DB
```

---

### Stage 5: Cache Reference Images

**Script:** `backend/scripts/cache_panoramas_v2.py`

**Purpose:** Fetch Street View images for each building (8 angles)

**Input:** Phase 1 DB `buildings` table
**Output:**
- Cloudflare R2 images
- Database metadata (future: `reference_images` table)

**Process:**
1. For each building in Phase 1 DB:
   2. Calculate building centroid
   3. For each angle (0°, 45°, 90°, ..., 315°):
      4. Fetch Street View image
      5. Upload to R2: `reference/buildings/{slug}/{angle}deg.jpg`
      6. Store metadata.json

**Angles:**
- 0° - North-facing
- 45° - Northeast
- 90° - East-facing
- 135° - Southeast
- 180° - South-facing
- 225° - Southwest
- 270° - West-facing
- 315° - Northwest

**Run:**
```bash
cd backend
export GOOGLE_MAPS_API_KEY="AIza..."
export R2_ACCESS_KEY_ID="..."
export R2_SECRET_ACCESS_KEY="..."
python scripts/cache_panoramas_v2.py

# Expected output:
# Caching: Lever House (BBL: 1012890036.0)
#   ✓ Fetched 0° image (640×640, 85KB)
#   ✓ Uploaded to R2: reference/buildings/lever-house/0deg.jpg
#   ✓ Fetched 45° image (640×640, 82KB)
#   ...
# ✓ Cached 800 images for 100 buildings
# 💰 Cost: $5.60 (800 × $0.007)
# ⏱ Duration: 45 minutes
```

---

### Stage 6: Generate CLIP Embeddings

**Script:** `backend/scripts/generate_embeddings_local.py`

**Purpose:** Pre-compute CLIP embeddings for all cached images

**Input:** R2 images from previous stage
**Output:** Phase 1 DB `reference_embeddings` table

**Process:**
1. For each building in Phase 1 DB:
   2. For each cached image (8 angles):
      3. Download from R2
      4. Encode with CLIP (ViT-B-32)
      5. Store 512-dim vector in DB
      6. Create HNSW index (once at end)

**Model:**
- **Name**: OpenCLIP ViT-B-32
- **Input**: 224×224 RGB
- **Output**: 512-dimensional embedding
- **Performance**: ~50ms per image (CPU)

**Run:**
```bash
cd backend
export SCAN_DB_URL="postgresql://..."
python scripts/generate_embeddings_local.py

# Expected output:
# Loading CLIP model...
# ✓ Model loaded: ViT-B-32
#
# Processing: Lever House
#   ✓ Encoded 0° (embedding: 512-dim)
#   ✓ Encoded 45° (embedding: 512-dim)
#   ...
# ✓ Generated 800 embeddings for 100 buildings
#
# Creating HNSW index...
# ✓ Index created: idx_embeddings_vector
#
# ⏱ Duration: 30 minutes (CPU)
```

---

### Stage 7: Validate Data

**Script:** `backend/scripts/validate_metadata.py`

**Purpose:** Check data quality and completeness

**Checks:**
1. All buildings have coordinates
2. All buildings have at least 4 reference images
3. All images have embeddings
4. BBL uniqueness
5. Coordinate validity (within NYC bounds)
6. No null critical fields

**Run:**
```bash
cd backend
python scripts/validate_metadata.py

# Expected output:
# Running validation checks...
#
# ✓ Buildings table:
#   - 100 buildings
#   - 100 with coordinates (100%)
#   - 0 duplicates
#
# ✓ Reference embeddings:
#   - 800 embeddings
#   - 100 buildings covered (100%)
#   - Avg: 8.0 embeddings per building
#
# ✓ HNSW index exists
#
# ⚠ Warnings:
#   - 3 buildings missing architect field
#   - 1 building has only 6 angles (missing 2)
#
# ✓ Overall: PASS
```

---

## Running the Pipeline

### Full Pipeline (From Scratch)

```bash
# 1. Setup environment
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Set environment variables
export DATABASE_URL="postgresql://..."
export SCAN_DB_URL="postgresql://..."
export GOOGLE_MAPS_API_KEY="AIza..."
export R2_ACCESS_KEY_ID="..."
export R2_SECRET_ACCESS_KEY="..."
export R2_BUCKET="building-images"

# 3. Run migrations
psql $DATABASE_URL < migrations/001_add_scan_tables.sql
psql $DATABASE_URL < migrations/002_create_unified_buildings_table.sql
psql $SCAN_DB_URL < migrations/003_scan_tables.sql

# 4. Import Phase 1 buildings (new dataset)
python scripts/import_top100_from_csv.py

# 5. Cache Street View images
python scripts/cache_panoramas_v2.py

# 6. Generate embeddings
python scripts/generate_embeddings_local.py

# 7. Validate
python scripts/validate_metadata.py

# 8. Test
python scripts/test_phase1_scan.py
```

### Incremental Updates

**Add new buildings:**
```bash
# Edit data/final/top_500.csv to include new buildings
# Re-run import (script handles duplicates)
python scripts/import_top100_from_csv.py --file=data/final/top_500.csv

# Cache images for new buildings only
python scripts/cache_panoramas_v2.py --new-only

# Generate embeddings for new images
python scripts/generate_embeddings_local.py --new-only
```

**Update existing building data:**
```bash
# Update metadata in CSV
# Re-import (use --update flag)
python scripts/import_top100_from_csv.py --update

# No need to regenerate images/embeddings unless coordinates changed
```

**Refresh Street View images:**
```bash
# If Street View imagery updated (every 1-2 years)
python scripts/cache_panoramas_v2.py --force-refresh

# Regenerate embeddings for updated images
python scripts/generate_embeddings_local.py --force-refresh
```

---

## Data Quality

### Metrics

**Coverage:**
- Phase 1 DB: 100 buildings (tier 1)
- Main DB: 860,245 buildings
- Landmarks: 5,342 (0.62% of total)

**Image Coverage:**
- Tier 1: 8 angles per building (100%)
- Tier 2 (future): 4 angles per building
- Tier 3: On-demand only

**Geocoding Accuracy:**
- PLUTO buildings: Parcel centroid (high accuracy)
- Landmarks: Address-based geocoding (95%+ accuracy)
- Final dataset: Pre-geocoded (manual validation)

### Quality Checks

**Automated:**
- BBL uniqueness
- Coordinate validity
- Image availability
- Embedding generation success
- Index integrity

**Manual:**
- Visual inspection of top 20 buildings
- Spot-check address matching
- Verify landmark designation accuracy
- Test scanning with known buildings

---

## Troubleshooting

### Issue: "Street View API quota exceeded"

```bash
# Check quota usage:
# Google Cloud Console → APIs → Quotas
# Street View Static API: 25,000/day

# Solution 1: Request quota increase
# Solution 2: Batch process over multiple days
python scripts/cache_panoramas_v2.py --batch-size=200 --delay=1

# Solution 3: Use existing Street View Alternative (Mapillary - free)
python scripts/cache_panoramas_mapillary.py
```

### Issue: "R2 upload failed"

```bash
# Check R2 credentials
aws s3 ls s3://building-images \
  --endpoint-url https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com \
  --profile r2

# Verify bucket exists
# Cloudflare Dashboard → R2 → Buckets

# Check R2 storage limit (10GB free tier)
```

### Issue: "CLIP model out of memory"

```bash
# Reduce batch size
python scripts/generate_embeddings_local.py --batch-size=1

# Or use smaller model
python scripts/generate_embeddings_local.py --model=ViT-B-16  # Smaller

# Or rent GPU (much faster)
# Colab, Paperspace, Lambda Labs, etc.
```

### Issue: "pgvector extension not found"

```sql
-- Install extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Verify
SELECT * FROM pg_extension WHERE extname = 'vector';

-- If not available, database doesn't support pgvector
-- Switch to different DB provider (see DEPLOYMENT.md)
```

### Issue: "Address fuzzy matching poor"

```bash
# Adjust matching threshold
python scripts/enrich_landmarks.py --threshold=0.85  # Default: 0.9

# Or use manual mapping file
# Create: data/manual_address_mappings.csv
# Format: pluto_address,landmark_address,bbl
python scripts/enrich_landmarks.py --manual-mappings=data/manual_address_mappings.csv
```

---

## Data Schema

### Final Dataset Schema (161 columns)

See sample row in [top_100.csv](../backend/data/final/top_100.csv)

**Critical Fields:**
- `bbl` (Primary Key)
- `building_name`, `address`
- `geocoded_lat`, `geocoded_lng`
- `final_score` (0-100)
- `geometry` (WKT MULTIPOLYGON)
- `style_prim`, `architect`
- `year_built`, `num_floors`

**Scoring Fields:**
- `final_score`: Composite importance (0-100)
- `ml_score`: Machine learning prediction
- `walk_score`: Pedestrian accessibility
- `architectural_merit`: Expert rating
- `visual_impact`: Visual prominence

**Aesthetic Categories:**
- `classicist_score`: Classical/Beaux-Arts
- `romantic_score`: Gothic Revival, Romanesque
- `stylist_score`: Art Deco, Art Nouveau
- `modernist_score`: International Style, Brutalist
- `visionary_score`: Deconstructivism, Parametric

---

## Cost Breakdown

**One-Time (Top 100):**
- Street View API: 800 images × $0.007 = $5.60
- R2 Storage: ~70MB × $0.015/GB = $0.001/mo
- Compute: Free (CPU, ~2 hours)
- **Total: $5.60 initial**

**Scaling to Top 1,000:**
- Street View API: 8,000 images × $0.007 = $56.00
- R2 Storage: ~700MB × $0.015/GB = $0.01/mo
- Compute: Free (CPU, ~20 hours)
- **Total: $56.00 initial**

**Scaling to Top 5,000:**
- Street View API: 40,000 images × $0.007 = $280.00
- R2 Storage: ~3.5GB × $0.015/GB = $0.05/mo
- Compute: GPU recommended (Colab Pro $10/mo)
- **Total: $290.00 initial**

---

## Next Steps

1. Review [Scripts Reference](SCRIPTS.md) for detailed script documentation
2. Review [Architecture](ARCHITECTURE.md) for system design
3. Review [Deployment](DEPLOYMENT.md) for production setup
4. Run validation after each stage
5. Monitor data quality metrics

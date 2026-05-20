# Scripts Reference

Complete reference for all backend scripts.

## Table of Contents

- [Data Ingestion](#data-ingestion)
- [Image Caching](#image-caching)
- [Embeddings](#embeddings)
- [Data Maintenance](#data-maintenance)
- [Testing](#testing)
- [Deprecated](#deprecated)

---

## Data Ingestion

### `ingest_pluto.py`

**Purpose:** Load 860k NYC buildings from PLUTO dataset into Main Supabase DB

**Usage:**
```bash
export DATABASE_URL="postgresql://..."
python scripts/ingest_pluto.py

# Options:
python scripts/ingest_pluto.py --file=data/pluto/pluto_24v2.csv --batch-size=5000
```

**Input:** `data/pluto/pluto_24v1.csv` (306MB)
**Output:** Main DB `buildings_full_merge_scanning` table
**Duration:** ~45 minutes

---

### `enrich_landmarks.py`

**Purpose:** Merge landmark metadata with PLUTO buildings

**Usage:**
```bash
python scripts/enrich_landmarks.py

# Options:
python scripts/enrich_landmarks.py --threshold=0.85 --manual-mappings=data/manual.csv
```

**Input:** Landmarks CSV + Main DB
**Output:** Updated `buildings_full_merge_scanning` with landmark data
**Duration:** ~10 minutes

---

### `preprocess_landmarks.py`

**Purpose:** Convert State Plane coordinates to WGS84 lat/lng

**Usage:**
```bash
python scripts/preprocess_landmarks.py
```

**Input:** `data/landmarks/landmarks_raw.csv`
**Output:** `data/landmarks/landmarks_geocoded.csv`
**Duration:** ~2 minutes

---

### `import_top100.py`

**Purpose:** Import top 100 buildings from old walk_optimized dataset

**Usage:**
```bash
export SCAN_DB_URL="postgresql://..."
python scripts/import_top100.py
```

**Input:** `data/walk_optimized_landmarks.csv`
**Output:** Phase 1 DB `buildings` table
**Duration:** ~1 minute
**Status:** Deprecated (use `import_top100_from_csv.py`)

---

### `import_top100_from_csv.py`

**Purpose:** Import top 100 buildings from new final dataset

**Usage:**
```bash
export SCAN_DB_URL="postgresql://..."
python scripts/import_top100_from_csv.py

# For different tiers:
python scripts/import_top100_from_csv.py --file=data/final/top_500.csv --tier=2
```

**Input:** `data/final/top_100.csv`
**Output:** Phase 1 DB `buildings` table
**Duration:** ~1 minute

---

## Image Caching

### `cache_panoramas_v2.py` ⭐ (Latest)

**Purpose:** Fetch Street View images for buildings (8 angles)

**Usage:**
```bash
export GOOGLE_MAPS_API_KEY="AIza..."
export R2_ACCESS_KEY_ID="..."
export R2_SECRET_ACCESS_KEY="..."
python scripts/cache_panoramas_v2.py

# Options:
python scripts/cache_panoramas_v2.py --new-only --batch-size=10 --delay=0.5
```

**Process:**
1. For each building in Phase 1 DB
2. Fetch 8 angles (0°, 45°, 90°, 135°, 180°, 225°, 270°, 315°)
3. Upload to R2: `reference/buildings/{slug}/{angle}deg.jpg`
4. Store metadata.json

**Cost:** 800 images × $0.007 = $5.60 (top 100)
**Duration:** ~45 minutes

---

### `cache_panoramas.py`

**Purpose:** Old version of panorama caching

**Status:** Deprecated (use `cache_panoramas_v2.py`)

---

## Embeddings

### `generate_embeddings_local.py`

**Purpose:** Pre-compute CLIP embeddings for all cached images

**Usage:**
```bash
export SCAN_DB_URL="postgresql://..."
python scripts/generate_embeddings_local.py

# Options:
python scripts/generate_embeddings_local.py --batch-size=8 --model=ViT-B-32
```

**Process:**
1. Load CLIP model (ViT-B-32)
2. For each cached image in R2
3. Download, resize to 224×224
4. Encode to 512-dim vector
5. Store in `reference_embeddings` table
6. Create HNSW index

**Duration:** ~30 minutes (CPU), ~5 minutes (GPU)
**Model:** OpenCLIP ViT-B-32 (~400MB)

---

## Data Maintenance

### `update_with_full_metadata.py`

**Purpose:** Enrich buildings with comprehensive metadata from final dataset

**Usage:**
```bash
python scripts/update_with_full_metadata.py
```

**Updates:** Architect scores, style significance, aesthetic categories, etc.

---

### `update_with_real_bbls.py`

**Purpose:** Fix BBL mappings (correct malformed BBLs)

**Usage:**
```bash
python scripts/update_with_real_bbls.py
```

---

### `fuzzy_match_remaining2.py` ⭐ (Latest)

**Purpose:** Fuzzy address matching for unmatched landmarks

**Usage:**
```bash
python scripts/fuzzy_match_remaining2.py --threshold=0.85
```

**Algorithm:** Levenshtein distance

---

### `fuzzy_match_remaining.py`

**Status:** Deprecated (use `fuzzy_match_remaining2.py`)

---

### `manual_fix_issues.py`

**Purpose:** One-off manual corrections to data

**Usage:**
```bash
python scripts/manual_fix_issues.py
```

**Note:** Script contents vary based on issues found

---

### `validate_metadata.py`

**Purpose:** Data quality validation

**Usage:**
```bash
python scripts/validate_metadata.py

# Outputs:
# ✓ Buildings: 100
# ✓ Embeddings: 800
# ✓ HNSW index: EXISTS
# ⚠ Warnings: 3 buildings missing architect
```

**Checks:**
- BBL uniqueness
- Coordinate validity
- Image coverage
- Embedding completeness
- Index integrity

---

### `preview_tier2.py`

**Purpose:** Preview buildings eligible for tier 2

**Usage:**
```bash
python scripts/preview_tier2.py --score-threshold=50 --limit=500
```

**Output:** List of buildings with scores 50-85

---

### `rename_r2_folders.py`

**Purpose:** Reorganize R2 storage structure

**Usage:**
```bash
python scripts/rename_r2_folders.py --dry-run
python scripts/rename_r2_folders.py  # Execute
```

**Changes:** Old structure → New structure

---

## Testing

### `test_batch.py`

**Purpose:** Batch processing validation

**Usage:**
```bash
python scripts/test_batch.py
```

---

### `test_streetview.py`

**Purpose:** Test Street View API connectivity

**Usage:**
```bash
export GOOGLE_MAPS_API_KEY="AIza..."
python scripts/test_streetview.py --lat=40.7484 --lng=-73.9857
```

**Outputs:** Sample Street View image

---

## Deprecated

Scripts no longer in active use:

- `cache_panoramas.py` → Use `cache_panoramas_v2.py`
- `fuzzy_match_remaining.py` → Use `fuzzy_match_remaining2.py`
- `import_top100.py` → Use `import_top100_from_csv.py`

---

## Common Patterns

### Error Handling

All scripts follow this pattern:

```python
try:
    # Main logic
    process_data()
except Exception as e:
    logger.error(f"Failed: {e}")
    sys.exit(1)
finally:
    # Cleanup
    db.close()
```

### Progress Logging

```python
from tqdm import tqdm

for building in tqdm(buildings, desc="Processing"):
    process(building)
```

### Database Connections

```python
# Main DB
from models.session import get_db
db = get_db()

# Phase 1 DB
from models.scan_db import SessionLocal
db = SessionLocal()
```

---

## Best Practices

1. **Always use environment variables** for credentials
2. **Test with small batches** before full run (`--limit=10`)
3. **Use `--dry-run`** for destructive operations
4. **Check logs** in `backend/logs/`
5. **Validate after each stage** with `validate_metadata.py`

---

## Troubleshooting

### Script won't run

```bash
# Check Python version
python3 --version  # Should be 3.11+

# Activate venv
cd backend
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Missing environment variable

```bash
# Check .env file exists
ls -la backend/.env

# Source it
set -a; source backend/.env; set +a

# Verify
echo $DATABASE_URL
```

### Database connection failed

```bash
# Test connection
psql $DATABASE_URL -c "SELECT 1;"

# Check firewall, network, credentials
```

---

## Next Steps

1. Review [Data Pipeline](DATA_PIPELINE.md) for end-to-end workflow
2. Review [Development Guide](DEVELOPMENT.md) for local setup
3. Run scripts in order (see DATA_PIPELINE.md)

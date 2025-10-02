# NYC Scan - Directory Structure

Clean, organized project structure.

```
nyc_scan/
├── .env                           # Local environment variables (gitignored)
├── .gitignore                     # Git ignore patterns
├── render.yaml                    # Render deployment config
│
├── README.md                      # Project overview
├── RUN_THIS_FIRST.md             # ⭐ START HERE - Complete setup guide
├── DATA_INGESTION_GUIDE.md       # Detailed data loading workflow
├── RENDER_DEPLOY.md              # Render deployment guide
├── GETTING_STARTED.md            # Quick start for development
├── PROJECT_STATUS.md             # Current progress & roadmap
├── SECURITY.md                   # Security best practices
├── PLAN OF ACTION.txt            # Original 3-week plan
│
└── backend/
    ├── main.py                   # FastAPI application entry point
    ├── requirements.txt          # Python dependencies
    ├── .env.example             # Environment variables template
    │
    ├── config/                   # Configuration
    │   └── settings.py          # App settings
    │
    ├── routers/                  # API endpoints
    │   ├── scan.py              # POST /scan - Main scan endpoint
    │   ├── buildings.py         # Building queries
    │   └── debug.py             # Debug/test endpoints
    │
    ├── services/                 # Business logic
    │   ├── clip_service.py      # CLIP model embedding
    │   ├── geospatial_service.py # PostGIS cone-of-vision queries
    │   ├── streetview_service.py # Google Street View fetching
    │   └── r2_service.py        # Cloudflare R2 storage
    │
    ├── models/                   # Data models
    │   └── building.py          # Building Pydantic models
    │
    ├── migrations/               # Database migrations
    │   ├── 001_add_scan_tables.sql
    │   └── 002_create_unified_buildings_table.sql  # ⭐ Main schema
    │
    ├── scripts/                  # Data management scripts
    │   ├── README.md            # Script documentation
    │   ├── preprocess_landmarks.py    # Extract lat/lng from geometry
    │   ├── ingest_pluto.py           # Load PLUTO data (860k buildings)
    │   ├── enrich_landmarks.py       # Add landmark metadata
    │   └── precache_landmarks.py     # Pre-cache Street View images
    │
    └── data/                     # Data files (gitignored)
        ├── .gitkeep
        ├── Primary_Land_Use_Tax_Lot_Output__PLUTO__20251001.csv  # 292MB
        ├── walk_optimized_landmarks.csv                          # 3MB
        └── landmarks_processed.csv                               # Generated
```

---

## Key Files

### Entry Points
- **[RUN_THIS_FIRST.md](RUN_THIS_FIRST.md)** - Complete setup workflow
- **[backend/main.py](backend/main.py)** - FastAPI app

### Database
- **[migrations/002_create_unified_buildings_table.sql](backend/migrations/002_create_unified_buildings_table.sql)** - Main schema
  - `buildings` table (PLUTO + landmarks merged)
  - `num_floors` for barometer validation
  - Spatial indexes for cone-of-vision

### Data Ingestion
- **[scripts/ingest_pluto.py](backend/scripts/ingest_pluto.py)** - Load 860k NYC buildings
- **[scripts/enrich_landmarks.py](backend/scripts/enrich_landmarks.py)** - Add landmark scores

### Deployment
- **[render.yaml](render.yaml)** - Render service config
- **[RENDER_DEPLOY.md](RENDER_DEPLOY.md)** - Deployment guide

---

## What's Gitignored

```gitignore
# Environment
.env
*.key

# Data (too large)
backend/data/*.csv
backend/data/*.json

# Python
__pycache__/
venv/
*.pyc

# Model cache
clip_model/
```

---

## Clean vs Messy

### ✅ Kept (Clean)
- All markdown docs (organized by purpose)
- Backend code (well-structured)
- Migration files (version controlled)
- Scripts with README

### ❌ Removed (Cleaned Up)
- `docker-compose.yml` - Not needed for Render
- `fly.toml` - Not using Fly.io yet
- `backend/Dockerfile` - Render uses native Python
- `DEPLOYMENT.md` - Replaced with RENDER_DEPLOY.md

---

## File Count

```bash
# Total files in project
find . -type f ! -path '*/.git/*' ! -path '*/venv/*' ! -path '*/__pycache__/*' | wc -l
# ~50 files (organized, not bloated)

# Python files
find backend -name "*.py" ! -path '*/venv/*' | wc -l
# ~20 files (clean architecture)

# Documentation
ls *.md | wc -l
# 8 markdown files (comprehensive but focused)
```

---

## Next Reorganization (If Needed)

If directory gets messy again:

### Option 1: Move Docs to docs/
```
nyc_scan/
├── docs/
│   ├── RUN_THIS_FIRST.md
│   ├── DATA_INGESTION_GUIDE.md
│   ├── RENDER_DEPLOY.md
│   └── ...
├── README.md
└── backend/
```

### Option 2: Combine Similar Docs
- Merge `GETTING_STARTED.md` + `RUN_THIS_FIRST.md`
- Merge `RENDER_DEPLOY.md` + deployment section of README
- Keep only: `README.md`, `SETUP_GUIDE.md`, `PROJECT_STATUS.md`

**Current structure is clean - no changes needed yet!**

---

## Directory Health Check

Run this to verify organization:

```bash
cd /Users/lucienmount/coding/nyc_scan

# Check for common issues
echo "=== Checking directory health ==="

# 1. No leftover Docker files
[ ! -f docker-compose.yml ] && echo "✅ No docker-compose.yml" || echo "❌ Found docker-compose.yml"
[ ! -f backend/Dockerfile ] && echo "✅ No Dockerfile" || echo "❌ Found Dockerfile"

# 2. Data folder exists
[ -d backend/data ] && echo "✅ Data folder exists" || echo "❌ No data folder"

# 3. Gitignore covers data
grep -q "backend/data/\*.csv" .gitignore && echo "✅ Data files gitignored" || echo "❌ Data not gitignored"

# 4. Main docs present
[ -f RUN_THIS_FIRST.md ] && echo "✅ Setup guide present" || echo "❌ No setup guide"
[ -f RENDER_DEPLOY.md ] && echo "✅ Deploy guide present" || echo "❌ No deploy guide"

# 5. Migration files present
[ -f backend/migrations/002_create_unified_buildings_table.sql ] && echo "✅ Main migration present" || echo "❌ No migration"

# 6. Scripts documented
[ -f backend/scripts/README.md ] && echo "✅ Scripts documented" || echo "❌ No script docs"

echo ""
echo "=== Directory structure ==="
tree -L 2 -I 'venv|__pycache__|.git|node_modules' .
```

---

**Current Status:** ✅ **CLEAN & ORGANIZED**

No further cleanup needed. Ready to proceed with data ingestion!

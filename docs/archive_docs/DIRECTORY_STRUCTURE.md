# NYC Scan - Directory Structure

Current production structure deployed to Render.

```
nyc_scan/
├── backend/
│   ├── main.py                    # FastAPI entry point
│   ├── requirements.txt           # Python dependencies
│   ├── .env                       # Local environment variables
│   ├── .env.example              # Example env vars
│   │
│   ├── models/                    # Data models
│   │   ├── config.py             # Settings & configuration
│   │   ├── database.py           # SQLAlchemy models
│   │   └── session.py            # Database session management
│   │
│   ├── routers/                   # API endpoints
│   │   ├── scan.py               # /api/scan - Point-and-scan endpoint
│   │   ├── buildings.py          # /api/buildings - Building queries
│   │   └── debug.py              # /api/debug - Debug endpoints (dev only)
│   │
│   ├── services/                  # Business logic
│   │   ├── clip_matcher.py       # CLIP vision matching
│   │   ├── geospatial.py         # PostGIS spatial queries
│   │   └── reference_images.py   # Google Street View + R2 storage
│   │
│   ├── utils/                     # Utilities
│   │   └── storage.py            # Cloudflare R2 storage client
│   │
│   ├── migrations/                # Database migrations
│   │   ├── 001_add_scan_tables.sql
│   │   └── 002_create_unified_buildings_table.sql
│   │
│   ├── scripts/                   # Data ingestion scripts
│   │   ├── README.md
│   │   ├── ingest_pluto.py       # Load NYC PLUTO data
│   │   ├── enrich_landmarks.py   # Add landmark metadata
│   │   └── preprocess_landmarks.py
│   │
│   └── data/                      # CSV files (gitignored)
│       ├── .gitkeep
│       └── [CSV files not in git]
│
├── render.yaml                    # Render deployment config
├── .gitignore
│
├── README.md                      # Project overview
├── FINAL_SUMMARY.md              # Current deployment state
├── RENDER_DEPLOY.md              # Deployment guide
├── RUN_THIS_FIRST.md             # Setup instructions
└── SECURITY.md                   # Security policy
```

## Database Schema

### `buildings_full_merge_scanning`
Main table with ~860k NYC buildings from PLUTO + landmark data:
- BBL (unique ID)
- Address, lat/lng
- Geometry (PostGIS)
- num_floors (for barometer validation)
- Landmark metadata (architect, style, year, etc.)
- is_landmark flag

### Spatial Indexes
- `idx_buildings_geom` - Geometry index for fast cone-of-vision queries
- `idx_buildings_coords` - Lat/lng index
- `idx_buildings_bbl` - Primary key

## API Endpoints

### `/api/scan` (POST)
Point-and-scan building identification
- Input: Photo, GPS, heading, altitude
- Process: Geospatial filter → CLIP matching
- Output: Ranked building matches

### `/api/buildings/{bbl}` (GET)
Get building details by BBL

### `/health` (GET)
Health check endpoint

## Tech Stack

### Backend
- **Runtime:** Python 3.11
- **Framework:** FastAPI + Uvicorn
- **Database:** Supabase PostgreSQL + PostGIS
- **ML:** OpenCLIP (ViT-B-32)
- **Storage:** Cloudflare R2
- **Caching:** Upstash Redis
- **Error Tracking:** Sentry
- **Deployment:** Render (free tier)

### Data Pipeline
- **PLUTO:** NYC building data (~860k buildings)
- **Landmarks:** Custom scored landmarks (~5k)
- **Images:** Google Street View → R2 storage

## Environment Variables

Required for production (set in Render dashboard):

```bash
# Database
DATABASE_URL=postgresql://...
SUPABASE_URL=https://...
SUPABASE_KEY=...
SUPABASE_SERVICE_KEY=...

# APIs
GOOGLE_MAPS_API_KEY=...
SENTRY_DSN=...

# Storage
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-xxxxx.r2.dev

# Optional
REDIS_URL=redis://...

# System
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
RENDER=true
```

## Deployment

**Platform:** Render
**URL:** https://nyc-scanning.onrender.com
**Auto-deploy:** Enabled on push to `main`

**Memory optimization:**
- CLIP model lazy-loads on first scan
- No uvicorn reload in production
- Startup: ~100MB → After first scan: ~350MB

## Data Ingestion Flow

1. **Download PLUTO CSV** → `backend/data/`
2. **Run migration:** `002_create_unified_buildings_table.sql`
3. **Ingest PLUTO:** `scripts/ingest_pluto.py`
4. **Enrich landmarks:** `scripts/enrich_landmarks.py`

Result: 860k buildings with spatial indexes ready for scanning.

## Git Ignored

```
backend/data/*.csv
backend/data/*.geojson
backend/__pycache__/
backend/venv/
.env
.DS_Store
```

## Cost Breakdown

| Service | Cost | Purpose |
|---------|------|---------|
| Render Free Tier | $0 | API hosting |
| Supabase Free Tier | $0 | Database (500MB) |
| Cloudflare R2 | ~$1/mo | Image storage |
| Google Maps API | ~$5-10/mo | Street View images |
| Upstash Redis | $0 | Caching (free tier) |
| Sentry | $0 | Error tracking (free tier) |
| **Total** | **~$6-11/mo** | |

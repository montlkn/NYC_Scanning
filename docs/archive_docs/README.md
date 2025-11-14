# NYC Scan - AI-Powered Building Recognition

Point your phone at any NYC building and instantly learn its name, architect, history, and architectural style.

**Status**: Production-ready Phase 1 (Top 100 buildings)
**Platform**: FastAPI backend + React Native mobile app (separate repo)
**Tech Stack**: OpenCLIP ViT-B-32 + PostGIS + pgvector

---

## Overview

NYC Scan uses computer vision (CLIP embeddings), geospatial filtering (PostGIS cone-of-vision), and multi-sensor fusion (GPS + compass + barometer) to identify NYC buildings in real-time (<100ms).

**Key Features:**
- Ultra-fast scanning (<100ms) using pre-computed embeddings
- Two-database architecture: comprehensive data + fast scanning
- 860k+ NYC buildings, 5k+ landmarks with rich metadata
- Multi-sensor fusion for accurate positioning
- Production-ready with Sentry monitoring and error tracking

---

## Architecture

```
Mobile App (React Native)
        ↓ HTTPS
FastAPI Backend
   ↙         ↘
Main DB    Phase 1 DB
(860k)     (5k tier 1)
Supabase   + pgvector
```

### Dual-Database Design

**Main Supabase DB** - Comprehensive building data
- 860,000+ NYC buildings (PLUTO dataset)
- Full metadata, landmarks, architectural details
- On-demand scanning (2-3s response)
- Use: Building details API, admin, analytics

**Phase 1 Postgres DB** - Ultra-fast scanning
- 5,000 curated tier 1 buildings (landmarks, iconic structures)
- Pre-computed CLIP embeddings (pgvector)
- Vector similarity search (<100ms response)
- Use: Production mobile app scanning

**Why two databases?** Separates fast real-time scanning from comprehensive data management. Phase 1 DB optimized for speed with pgvector HNSW indexing.

---

## Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 14+ with PostGIS and pgvector extensions
- Google Maps API key (Street View Static API)
- Cloudflare R2 account
- Supabase account (or self-hosted Postgres)

### 5-Minute Setup

```bash
# 1. Clone and setup
git clone https://github.com/your-username/nyc-scan.git
cd nyc-scan/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
nano .env  # Add your API keys

# 3. Run migrations
psql $DATABASE_URL < migrations/001_add_scan_tables.sql
psql $DATABASE_URL < migrations/002_create_unified_buildings_table.sql
psql $SCAN_DB_URL < migrations/003_scan_tables.sql

# 4. Start server
uvicorn main:app --reload --port 8000

# 5. Test
curl http://localhost:8000/api/debug/health
```

**Full setup guide**: See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

---

## Documentation

Comprehensive documentation available in `/docs`:

- **[Architecture](docs/ARCHITECTURE.md)** - System design, two-database architecture, data flow
- **[API Reference](docs/API_REFERENCE.md)** - Complete endpoint documentation
- **[Development Guide](docs/DEVELOPMENT.md)** - Local setup, debugging, contributing
- **[Deployment Guide](docs/DEPLOYMENT.md)** - Production deployment (Render, Fly.io, Docker)
- **[Data Pipeline](docs/DATA_PIPELINE.md)** - ETL process, data sources, importing
- **[Scripts Reference](docs/SCRIPTS.md)** - All maintenance and data processing scripts

---

## API Endpoints

### Scanning

**POST /api/phase1/scan** - Fast scanning (Phase 1 DB, <100ms)
```bash
curl -X POST http://localhost:8000/api/phase1/scan \
  -F "photo=@building.jpg" \
  -F "lat=40.7484" \
  -F "lng=-73.9857" \
  -F "bearing=45" \
  -F "pitch=10" \
  -F "gps_accuracy=5"
```

**POST /api/scan** - Main scanning (comprehensive, 2-3s)

### Building Data

**GET /api/buildings/{bbl}** - Get building details
**GET /api/buildings/nearby** - Find buildings near location
**GET /api/buildings/search** - Search by name/address/architect
**GET /api/stats** - Database statistics

See [API Reference](docs/API_REFERENCE.md) for complete documentation.

---

## Data Sources

### Final Dataset (November 2024)

**Location**: `backend/data/final/`

**Files:**
- `top_100.csv` - Top 100 buildings by importance score
- `top_500.csv` - Top 500
- `top_1000.csv` - Top 1,000
- `top_4000.csv` - Top 4,000
- `full_dataset.csv` - 37,296 analyzed buildings
- `full_dataset.geojson` - GeoJSON format

**Data Fields** (161 columns):
- **Identifiers**: BBL, building name, address
- **Location**: Lat/lng, geometry (WKT)
- **Physical**: Height, floors, year built
- **Architectural**: Architect, style, materials
- **Scoring**: Final score, ML score, walk score
- **Aesthetic**: Classicist, modernist, visionary scores
- **Context**: Rarity, surprise, local contrast

### Historical Sources

- **NYC PLUTO**: 860k buildings, tax lot data
- **NYC Landmarks**: 5k landmarks with metadata
- **Google Street View**: Reference images (8 angles per building)

---

## Technology Stack

**Backend:**
- **Framework**: FastAPI (async Python web framework)
- **Databases**: PostgreSQL + PostGIS + pgvector
- **ML Model**: OpenCLIP ViT-B-32 (512-dim embeddings)
- **Storage**: Cloudflare R2 (S3-compatible, images)
- **Cache**: Upstash Redis
- **Monitoring**: Sentry

**Data Processing:**
- **Geospatial**: PostGIS for cone-of-vision queries
- **Vector Search**: pgvector with HNSW indexing
- **Image Processing**: Pillow, torchvision

**Deployment:**
- **Platform**: Render (free tier or $7/mo starter)
- **CI/CD**: GitHub Actions (future)

---

## Project Structure

```
nyc-scan/
├── backend/
│   ├── main.py                       # FastAPI app entry point
│   ├── routers/
│   │   ├── scan.py                  # Main scan endpoint
│   │   ├── scan_phase1.py           # Fast Phase 1 scan
│   │   ├── buildings.py             # Building data endpoints
│   │   └── debug.py                 # Debug utilities
│   ├── services/
│   │   ├── clip_matcher.py          # CLIP model & matching
│   │   ├── geospatial.py            # PostGIS cone-of-vision
│   │   └── reference_images.py      # Street View fetching
│   ├── models/
│   │   ├── database.py              # Main DB models (SQLAlchemy)
│   │   ├── scan_db.py               # Phase 1 DB session
│   │   ├── session.py               # Async session manager
│   │   └── config.py                # Pydantic settings
│   ├── utils/
│   │   └── storage.py               # Cloudflare R2 uploads
│   ├── migrations/
│   │   ├── 001_add_scan_tables.sql
│   │   ├── 002_create_unified_buildings_table.sql
│   │   └── 003_scan_tables.sql (Phase 1 DB)
│   ├── scripts/                     # Data pipeline scripts
│   │   ├── import_top100_from_csv.py
│   │   ├── cache_panoramas_v2.py
│   │   ├── generate_embeddings_local.py
│   │   └── ... (see docs/SCRIPTS.md)
│   ├── data/
│   │   └── final/                   # New dataset (Nov 2024)
│   │       ├── top_100.csv
│   │       ├── top_500.csv
│   │       └── full_dataset.csv
│   ├── requirements.txt
│   ├── .env.example
│   └── .env (your local config)
├── docs/                            # Documentation
│   ├── ARCHITECTURE.md
│   ├── API_REFERENCE.md
│   ├── DEVELOPMENT.md
│   ├── DEPLOYMENT.md
│   ├── DATA_PIPELINE.md
│   └── SCRIPTS.md
├── README.md
└── .gitignore
```

---

## Data Pipeline

### Quick Pipeline (Top 100 Buildings)

```bash
# 1. Import buildings from new dataset
python scripts/import_top100_from_csv.py

# 2. Cache Street View images (8 angles × 100 = 800 images, ~$5.60)
python scripts/cache_panoramas_v2.py

# 3. Generate CLIP embeddings (~30 min on CPU)
python scripts/generate_embeddings_local.py

# 4. Validate
python scripts/validate_metadata.py

# 5. Test
curl -X POST http://localhost:8000/api/phase1/scan \
  -F "photo=@empire_state.jpg" -F "lat=40.7484" -F "lng=-73.9857" \
  -F "bearing=45" -F "pitch=10" -F "gps_accuracy=5"
```

**Full pipeline guide**: See [docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md)

---

## Current Status

### Phase 1 (Current)

**✅ Completed:**
- Two-database architecture implemented
- Main DB with 860k buildings
- Phase 1 DB with pgvector support
- Fast scanning endpoint (<100ms)
- Comprehensive documentation
- Data pipeline scripts
- Sentry error tracking
- Deployed to Render

**🚧 In Progress:**
- Importing new final dataset (top_100.csv → top_1000.csv)
- Caching Street View images for new buildings
- Generating CLIP embeddings
- Mobile app integration (separate repo)

### Phase 2 (Q1 2025)

- Expand to 5,000 tier 1 buildings
- User authentication (OAuth)
- Feedback loop for model improvement
- Mobile app release (iOS/Android beta)

### Phase 3 (Q2 2025)

- Tier 2 buildings (50k total)
- Fine-tuned CLIP model on NYC architecture
- AR overlays in mobile app
- Historical photo matching ("Then & Now")

---

## Performance

**Scanning Speed:**
- Phase 1 endpoint: <100ms average
- Main endpoint: 2-3s average

**Accuracy:**
- Top-1 accuracy: ~85% (tier 1 buildings)
- Top-3 accuracy: ~95%

**Coverage:**
- Tier 1 (Phase 1 DB): 100 buildings (expanding to 5,000)
- All buildings (Main DB): 860,245 buildings

**Cost:**
- Development: ~$5-10 one-time (Street View images)
- Production: ~$42-50/month (Render + databases + APIs)

---

## Environment Variables

Required in `.env`:

```bash
# Main Database
DATABASE_URL=postgresql://...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=xxx
SUPABASE_SERVICE_KEY=xxx

# Phase 1 Database (separate!)
SCAN_DB_URL=postgresql://...

# APIs
GOOGLE_MAPS_API_KEY=AIza...
SENTRY_DSN=https://...

# Storage
R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY_ID=xxx
R2_SECRET_ACCESS_KEY=xxx
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-xxx.r2.dev

# Cache
REDIS_URL=redis://...

# System
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
```

See [.env.example](backend/.env.example) for complete template.

---

## Contributing

We welcome contributions! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Write tests (future)
5. Commit (`git commit -m 'Add amazing feature'`)
6. Push (`git push origin feature/amazing-feature`)
7. Open a Pull Request

See [DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup and coding guidelines.

---

## License

[MIT License](LICENSE)

---

## Contact & Support

**Issues**: [GitHub Issues](https://github.com/your-username/nyc-scan/issues)
**Documentation**: [/docs](docs/)
**API Docs**: http://localhost:8000/docs (when running locally)

---

## Acknowledgments

**Data Sources:**
- NYC Open Data (PLUTO dataset)
- NYC Landmarks Preservation Commission
- Google Street View Static API

**Technologies:**
- OpenAI CLIP (via OpenCLIP)
- FastAPI framework
- PostGIS geospatial extension
- pgvector for vector similarity search
- Cloudflare R2 for object storage

---

## Roadmap

**Q4 2024:**
- ✅ Implement Phase 1 fast scanning
- ✅ Dual-database architecture
- ✅ Comprehensive documentation
- 🚧 Import new final dataset
- 🚧 Mobile app beta testing

**Q1 2025:**
- Expand to 5,000 tier 1 buildings
- User authentication
- iOS/Android app release
- Model fine-tuning on NYC data

**Q2 2025:**
- Tier 2 buildings (50k total)
- AR overlays
- Historical photo matching
- Community contributions

**Q3 2025:**
- Edge deployment (Cloudflare Workers)
- 3D building models integration
- Guided architecture tours
- API for third-party developers

---

**Built with ❤️ for NYC architecture enthusiasts**

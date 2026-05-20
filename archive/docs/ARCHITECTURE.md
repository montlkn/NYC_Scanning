# NYC Scan Architecture

## Overview

NYC Scan is an AI-powered building recognition system that allows users to identify NYC buildings by pointing their phone camera at them. The system combines computer vision (CLIP embeddings), geospatial filtering (PostGIS), and multi-sensor fusion (GPS, compass, barometer, IMU) to provide fast, accurate building identification.

## System Architecture

```
┌─────────────────┐
│  Mobile App     │  React Native + Expo
│  (Separate Repo)│  - Camera capture
│                 │  - GPS + compass + barometer
│                 │  - Kalman filtering
└────────┬────────┘
         │ HTTPS
         ▼
┌─────────────────────────────────────────────────────┐
│              FastAPI Backend                         │
│  ┌──────────────┬───────────────┬─────────────────┐ │
│  │  Routers     │   Services    │     Utils       │ │
│  │              │               │                 │ │
│  │ • scan.py    │ • clip_matcher│ • storage.py    │ │
│  │ • scan_phase1│ • geospatial  │   (R2 uploads)  │ │
│  │ • buildings  │ • ref_images  │                 │ │
│  │ • debug      │               │                 │ │
│  └──────────────┴───────────────┴─────────────────┘ │
└──────────┬──────────────┬───────────────────────────┘
           │              │
           ▼              ▼
    ┌──────────┐    ┌──────────────┐
    │ Main DB  │    │ Phase 1 DB   │
    │ Supabase │    │ Postgres     │
    │          │    │ + pgvector   │
    │ 860k bldg│    │ 5k tier 1    │
    └──────────┘    └──────────────┘
           │              │
           └──────┬───────┘
                  ▼
        ┌──────────────────┐
        │  External Services│
        │                  │
        │ • Cloudflare R2  │
        │ • Street View API│
        │ • Upstash Redis  │
        │ • Sentry         │
        └──────────────────┘
```

## Two-Database Architecture

### Why Two Databases?

NYC Scan uses a dual-database architecture to optimize for both **comprehensive data** and **fast scanning**:

#### Main Supabase Database
- **Purpose**: Comprehensive building data, metadata, landmark information
- **Size**: 860,000+ buildings
- **Use Cases**:
  - Building details API (`/api/buildings/{bbl}`)
  - Administrative operations
  - Analytics and reporting
  - On-demand scanning (slower but comprehensive)
- **Matching Strategy**: Fetch Street View images → encode with CLIP → compare
- **Performance**: 2-3 seconds per scan
- **Coverage**: All NYC buildings

#### Phase 1 Postgres Database
- **Purpose**: Ultra-fast scanning with pre-computed embeddings
- **Size**: ~5,000 tier 1 buildings (expandable to 50k tier 2)
- **Use Cases**:
  - Real-time mobile scanning
  - Production mobile app endpoint (`/api/phase1/scan`)
- **Matching Strategy**: Direct vector similarity search using pgvector
- **Performance**: <100ms per scan
- **Coverage**: Curated high-value buildings (landmarks, iconic structures)

### Database Schemas

#### Main Supabase DB

```sql
-- Primary table: 860k NYC buildings
buildings_full_merge_scanning (
    bbl PRIMARY KEY,              -- Borough-Block-Lot (10-digit NYC identifier)
    address TEXT,
    borough TEXT,
    bin TEXT,                     -- Building Identification Number
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    geom GEOMETRY(POINT, 4326),   -- PostGIS geometry
    year_built INTEGER,
    num_floors INTEGER,
    is_landmark BOOLEAN,
    landmark_name TEXT,
    architect TEXT,
    architectural_style TEXT,
    walk_score DOUBLE PRECISION,
    is_walk_optimized BOOLEAN,
    scan_enabled BOOLEAN,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)

-- Reference images from Street View/Mapillary
reference_images (
    id UUID PRIMARY KEY,
    BBL TEXT FOREIGN KEY,         -- Links to buildings (uppercase!)
    image_url TEXT,
    thumbnail_url TEXT,
    source TEXT,                  -- 'street_view' | 'mapillary' | 'user'
    compass_bearing INTEGER,      -- 0-360 degrees
    capture_lat DOUBLE PRECISION,
    capture_lng DOUBLE PRECISION,
    clip_embedding DOUBLE PRECISION[], -- Array, not pgvector
    created_at TIMESTAMP,
    cached_at TIMESTAMP
)

-- User scan history
scans (
    id UUID PRIMARY KEY,
    user_photo_url TEXT,
    gps_lat DOUBLE PRECISION,
    gps_lng DOUBLE PRECISION,
    compass_bearing INTEGER,
    candidate_bbls TEXT[],
    top_match_bbl TEXT,
    confidence_score DOUBLE PRECISION,
    processing_time_ms INTEGER,
    created_at TIMESTAMP
)

-- User feedback
scan_feedback (
    id UUID PRIMARY KEY,
    scan_id UUID FOREIGN KEY,
    is_correct BOOLEAN,
    correct_bbl TEXT,
    feedback_text TEXT,
    created_at TIMESTAMP
)

-- Cache performance metrics
cache_stats (
    id UUID PRIMARY KEY,
    cache_key TEXT,
    hit_count INTEGER,
    miss_count INTEGER,
    last_accessed TIMESTAMP
)
```

#### Phase 1 Postgres DB

```sql
-- Simplified building table for fast scanning
buildings (
    id SERIAL PRIMARY KEY,
    bbl TEXT UNIQUE,
    des_addres TEXT,              -- Address
    build_nme TEXT,               -- Building name
    style_prim TEXT,              -- Primary architectural style
    num_floors INTEGER,
    final_score DOUBLE PRECISION, -- Composite importance score
    geom GEOMETRY(MULTIPOLYGON, 4326),
    center GEOMETRY(POINT, 4326), -- Building centroid
    tier INTEGER                  -- 1=top, 2=important, 3=all others
)

-- Pre-computed CLIP embeddings (pgvector)
reference_embeddings (
    id SERIAL PRIMARY KEY,
    building_id INTEGER FOREIGN KEY,
    angle INTEGER,                -- 0, 45, 90, 135, 180, 225, 270, 315
    pitch INTEGER,                -- Camera pitch angle
    embedding vector(512),        -- pgvector type (ViT-B-32 CLIP)
    image_key TEXT,               -- R2 storage key
    created_at TIMESTAMP
)

-- Indexes for performance
CREATE INDEX idx_buildings_tier ON buildings(tier);
CREATE INDEX idx_buildings_geom ON buildings USING GIST(geom);
CREATE INDEX idx_buildings_center ON buildings USING GIST(center);
CREATE INDEX idx_embeddings_building ON reference_embeddings(building_id);

-- HNSW index for vector similarity search (critical for speed!)
CREATE INDEX idx_embeddings_vector ON reference_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Key Differences

| Feature | Main Supabase DB | Phase 1 DB |
|---------|------------------|------------|
| **Storage** | Supabase (managed Postgres) | Self-hosted Postgres |
| **Embeddings** | ARRAY (slower) | pgvector (optimized) |
| **Vector Search** | Linear scan | HNSW index |
| **Coverage** | 860k buildings | 5k tier 1 (expandable) |
| **Speed** | 2-3 seconds | <100ms |
| **Use Case** | Comprehensive data | Production scanning |
| **Extensions** | PostGIS | PostGIS + pgvector |

## API Endpoints

### Scanning Endpoints

#### POST /api/scan
**Main scanning endpoint** (Main Supabase DB)
- On-demand Street View fetching
- Slower but comprehensive
- All 860k buildings available
- Used for: Development, testing, coverage validation

**Request:**
```json
{
  "photo": "base64_encoded_image",
  "gps_lat": 40.7484,
  "gps_lng": -73.9857,
  "compass_bearing": 45,
  "altitude": 10.5
}
```

**Response:**
```json
{
  "matches": [
    {
      "bbl": "1008350041",
      "building_name": "Empire State Building",
      "address": "350 Fifth Avenue",
      "confidence": 0.89,
      "distance_meters": 23.4
    }
  ],
  "processing_time_ms": 2340
}
```

#### POST /api/phase1/scan
**Fast scanning endpoint** (Phase 1 DB with pgvector)
- Pre-computed embeddings
- Vector similarity search
- <100ms response time
- Used for: Production mobile app

**Request:** Same as above

**Response:**
```json
{
  "match": {
    "bbl": "1008350041",
    "building_name": "Empire State Building",
    "address": "350 Fifth Avenue",
    "confidence": 0.92,
    "matched_angle": 180,
    "tier": 1
  },
  "processing_time_ms": 87
}
```

### Building Data

#### GET /api/buildings/{bbl}
**Get detailed building information**

**Response:**
```json
{
  "bbl": "1008350041",
  "building_name": "Empire State Building",
  "address": "350 Fifth Avenue",
  "borough": "Manhattan",
  "year_built": 1931,
  "num_floors": 102,
  "is_landmark": true,
  "landmark_name": "Empire State Building",
  "architect": "Shreve, Lamb & Harmon",
  "architectural_style": "Art Deco",
  "walk_score": 98.5,
  "reference_images": [
    {
      "url": "https://r2.../0deg.jpg",
      "bearing": 0
    }
  ]
}
```

## Services

### CLIP Matcher (`services/clip_matcher.py`)

**Model**: OpenCLIP ViT-B-32
- **Input**: 224x224 RGB image
- **Output**: 512-dimensional embedding
- **Performance**: ~50ms per image on CPU
- **Similarity Metric**: Cosine similarity

**Usage:**
```python
from services.clip_matcher import encode_image, compare_images

# Encode user photo
user_embedding = encode_image(user_photo_bytes)

# Compare with reference
similarity = cosine_similarity(user_embedding, reference_embedding)
```

### Geospatial (`services/geospatial.py`)

**PostGIS cone-of-vision filtering**

Given user's GPS position and compass bearing, filters buildings within a directional cone:

```sql
SELECT * FROM buildings
WHERE ST_DWithin(
    geom,
    ST_SetSRID(ST_MakePoint($lng, $lat), 4326)::geography,
    $radius_meters
)
AND angle_between(
    bearing_to_building(ST_Centroid(geom), $user_point),
    $compass_bearing
) < $cone_half_angle
```

**Parameters:**
- `radius_meters`: 100m default (Phase 1), 500m max (Main)
- `cone_half_angle`: 30° (60° total cone)
- `altitude_filter`: Optional barometer-based floor estimation

### Reference Images (`services/reference_images.py`)

**Fetches and caches building images from Google Street View**

**Process:**
1. Check Redis cache (key: `ref_img:{bbl}:{bearing}`)
2. If miss, fetch from Street View API
3. Upload to R2 storage
4. Store metadata in database
5. Cache URL in Redis (TTL: 7 days)

**Street View API:**
- **URL**: `https://maps.googleapis.com/maps/api/streetview`
- **Parameters**:
  - `location`: `{lat},{lng}`
  - `heading`: Compass bearing (0-360)
  - `size`: 640x640
  - `fov`: 90 (field of view)
  - `pitch`: 10 (slight upward tilt)
- **Cost**: ~$0.007 per image

**Multi-angle capture:**
For Phase 1 embeddings, fetch 8 angles per building:
- 0° (North), 45° (NE), 90° (East), 135° (SE)
- 180° (South), 225° (SW), 270° (West), 315° (NW)

## Storage

### Cloudflare R2 (S3-Compatible)

**Bucket**: `building-images`
**Public URL**: `https://pub-234fc67c039149b2b46b864a1357763d.r2.dev`

**Structure:**
```
reference/
└── buildings/
    └── {building-slug}/
        ├── 0deg.jpg         # North-facing
        ├── 45deg.jpg
        ├── 90deg.jpg        # East-facing
        ├── 135deg.jpg
        ├── 180deg.jpg       # South-facing
        ├── 225deg.jpg
        ├── 270deg.jpg       # West-facing
        ├── 315deg.jpg
        └── metadata.json    # Capture info

scans/
└── {scan_id}.jpg            # User uploads
    # 30-day TTL (automatically deleted)
```

**Upload Process:**
```python
from utils.storage import upload_to_r2

# Upload reference image
url = upload_to_r2(
    image_bytes,
    f"reference/buildings/{building_slug}/0deg.jpg",
    content_type="image/jpeg"
)

# Upload user scan (with TTL)
url = upload_to_r2(
    user_photo,
    f"scans/{scan_id}.jpg",
    content_type="image/jpeg",
    ttl_days=30
)
```

## Data Pipeline

### 1. Data Sources

**NYC PLUTO Dataset** (860k buildings)
- Source: NYC Open Data
- Format: CSV (306MB)
- Contains: Address, BBL, coordinates, floors, year_built
- Updated: Annually

**NYC Landmarks Database** (5k landmarks)
- Source: NYC Landmarks Preservation Commission
- Format: CSV (3MB)
- Contains: Architect, style, historic data, walk scores
- Updated: Quarterly

**Google Street View** (on-demand)
- 8-bearing panoramic coverage
- Fetched as needed or pre-cached
- Cost: $0.007/image

### 2. Ingestion Pipeline

```bash
# 1. Preprocess landmarks (convert State Plane → lat/lng)
python scripts/preprocess_landmarks.py

# 2. Ingest PLUTO buildings → Main Supabase DB
python scripts/ingest_pluto.py

# 3. Enrich with landmark metadata
python scripts/enrich_landmarks.py

# 4. Import top 100 → Phase 1 DB
python scripts/import_top100_from_csv.py

# 5. Cache reference images (8 angles per building)
python scripts/cache_panoramas_v2.py

# 6. Generate CLIP embeddings
python scripts/generate_embeddings_local.py
```

### 3. Tiering System

Buildings are assigned tiers based on `final_score`:

**Tier 1** (Top 100-5,000 buildings)
- Criteria: Landmarks, iconic structures, high walk scores
- Score range: 85-100
- Full 8-angle coverage
- Pre-computed embeddings in Phase 1 DB

**Tier 2** (Next 50,000 buildings)
- Criteria: Notable buildings, high foot traffic
- Score range: 50-85
- 4-angle coverage (N, E, S, W)
- On-demand embedding generation

**Tier 3** (Remaining 800k+ buildings)
- All other buildings
- Score range: 0-50
- No pre-cached images
- Fallback to on-demand scanning

## Deployment

### Platform: Render

**Service Type**: Web Service (Free Tier)
**Region**: Oregon (us-west)
**Build Command**: `pip install -r requirements.txt`
**Start Command**: `uvicorn main:app --host 0.0.0.0 --port 8000`

**Limitations:**
- Spins down after 15min idle
- 60s cold start time
- 512MB RAM
- Shared CPU

**Production URL**: `https://nyc-scan-api.onrender.com`

### Environment Variables

**Required:**
```bash
# Main Database
DATABASE_URL=postgresql://...supabase.com:5432/postgres
SUPABASE_URL=https://....supabase.co
SUPABASE_KEY=eyJ...
SUPABASE_SERVICE_KEY=eyJ...

# Phase 1 Database (separate!)
SCAN_DB_URL=postgresql://...

# APIs
GOOGLE_MAPS_API_KEY=AIza...
SENTRY_DSN=https://...@sentry.io/...

# Storage
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-....r2.dev

# Caching
REDIS_URL=redis://....upstash.io:6379

# System
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
RENDER=true
```

## Performance Optimizations

### 1. Vector Search (Phase 1 DB)
- **HNSW Index**: Approximate nearest neighbor search
- **Parameters**: m=16, ef_construction=64
- **Trade-off**: 95%+ recall, 100x faster than linear scan

### 2. Geospatial Filtering
- **PostGIS GIST Index**: Spatial index on `geom` column
- **Cone-of-vision**: Reduces candidates from 860k → ~50-200
- **Radius limiting**: 100m for Phase 1, 500m max

### 3. Caching Strategy
- **Redis**: Reference image URLs (7-day TTL)
- **R2**: Image CDN with edge caching
- **In-memory**: CLIP model loaded once at startup

### 4. Async Processing
- **FastAPI async/await**: Non-blocking I/O
- **Parallel fetching**: Up to 5 concurrent Street View requests
- **Background tasks**: Logging, analytics, cache warming

## Security

### API Key Restrictions
- **Google Maps API**: Restricted to Street View Static API only
- **Supabase**: Row-level security (RLS) policies
- **R2**: Signed URLs for user uploads (short-lived)

### Data Privacy
- **User photos**: 30-day TTL, auto-deleted
- **GPS coordinates**: Not stored long-term
- **Scan history**: Anonymized, no PII

### Rate Limiting
- **Per-IP**: 100 requests/minute
- **Per-User**: 1000 requests/hour (future: requires auth)

## Monitoring

### Sentry Error Tracking
- **Integration**: FastAPI middleware
- **Context**: Request ID, user agent, GPS coords (rounded)
- **Performance**: Transaction sampling (10%)

### Metrics
- Scan success rate
- Average confidence scores
- Processing times (p50, p95, p99)
- Cache hit rates
- Database query times

## Future Enhancements

### Short-term
1. **Expand Phase 1 DB** to 5,000 buildings
2. **User authentication** (OAuth)
3. **Feedback loop** for model improvement
4. **Mobile app release** (iOS/Android)

### Long-term
1. **Edge deployment** (Cloudflare Workers)
2. **Fine-tuned CLIP model** on NYC buildings
3. **3D building models** integration
4. **AR overlays** in mobile app
5. **Historical photo matching** (Then & Now)

## Related Documentation

- [API Reference](API_REFERENCE.md) - Complete endpoint documentation
- [Deployment Guide](DEPLOYMENT.md) - Production setup
- [Development Guide](DEVELOPMENT.md) - Local development setup
- [Data Pipeline](DATA_PIPELINE.md) - ETL process details
- [Scripts Reference](SCRIPTS.md) - All maintenance scripts

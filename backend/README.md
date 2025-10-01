# NYC Scan Backend

Point-and-scan building identification system using computer vision, GPS, and compass data.

## Architecture Overview

### Tech Stack
- **Framework**: FastAPI (async Python web framework)
- **Database**: PostgreSQL + PostGIS (via Supabase)
- **Caching**: Redis (Upstash)
- **Storage**: Cloudflare R2 (S3-compatible)
- **ML Model**: OpenCLIP (ViT-B-32 with LAION weights)
- **Image Sources**: Google Street View Static API

### Core Components

1. **Geospatial Service** (`services/geospatial.py`)
   - Cone-of-vision calculation using user's GPS + compass bearing
   - PostGIS spatial queries for building filtering
   - Distance and relevance scoring

2. **Reference Image Service** (`services/reference_images.py`)
   - Fetch and cache Street View images
   - Bearing-based image selection
   - Parallel fetching for multiple candidates

3. **CLIP Matcher** (`services/clip_matcher.py`)
   - Image embedding generation
   - Cosine similarity comparison
   - Confidence scoring with landmark/proximity boosts

4. **Storage Utilities** (`utils/storage.py`)
   - Cloudflare R2 upload/download
   - Thumbnail generation
   - Public URL management

## Setup Instructions

### 1. Environment Setup

```bash
cd backend

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
- `SUPABASE_URL` - Your Supabase project URL
- `SUPABASE_KEY` - Supabase anon key
- `DATABASE_URL` - PostgreSQL connection string (from Supabase)
- `GOOGLE_MAPS_API_KEY` - Google Maps API key with Street View Static API enabled
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` - Cloudflare R2 credentials
- `REDIS_URL` - Redis connection string (e.g., from Upstash)

### 3. Database Setup

The backend uses your existing Supabase `buildings` table. You'll need to add these new tables:

```bash
# Run migrations (TODO: implement migration system)
psql $DATABASE_URL < migrations/001_add_scan_tables.sql
```

New tables needed:
- `reference_images` - Cached Street View images
- `scans` - User scan history
- `scan_feedback` - User feedback
- `cache_stats` - Cache performance metrics

### 4. Enable PostGIS

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

### 5. Download CLIP Model

The CLIP model will auto-download on first run (~350MB). To pre-download:

```python
python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')"
```

## Running the Backend

### Development Mode

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Production Mode

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Docker (Optional)

```bash
docker build -t nyc-scan-backend .
docker run -p 8000:8000 --env-file .env nyc-scan-backend
```

## API Endpoints

### Main Endpoints

#### `POST /api/scan`
Main scan endpoint - identifies building from photo + GPS + compass

**Form Data:**
- `photo` (file): Building photo
- `gps_lat` (float): Latitude
- `gps_lng` (float): Longitude
- `compass_bearing` (float): Compass bearing (0-360°)
- `phone_pitch` (float, optional): Phone pitch angle
- `user_id` (string, optional): User ID for tracking

**Response:**
```json
{
  "scan_id": "uuid",
  "matches": [
    {
      "bbl": "1000010001",
      "address": "1 Wall Street",
      "confidence": 0.87,
      "thumbnail_url": "https://...",
      "is_landmark": true
    }
  ],
  "show_picker": false,
  "processing_time_ms": 2450
}
```

#### `POST /api/scans/{scan_id}/confirm`
Confirm building identification

**Form Data:**
- `confirmed_bbl` (string): Confirmed building BBL

#### `GET /api/buildings/{bbl}`
Get building details

**Response:**
```json
{
  "bbl": "1000010001",
  "address": "1 Wall Street",
  "year_built": 1930,
  "architect": "Benjamin Wistar Morris",
  "style_primary": "Art Deco",
  "is_landmark": true,
  ...
}
```

### Debug Endpoints (Development Only)

- `GET /api/debug/test-geospatial` - Test cone-of-vision logic
- `GET /api/debug/test-clip` - Test CLIP model loading
- `GET /api/debug/test-bearing` - Test bearing calculations
- `POST /api/debug/test-image-comparison` - Compare two images
- `GET /api/debug/test-street-view` - Test Street View API

## Data Ingestion

### Load Building Data

```bash
# TODO: Implement data ingestion scripts
python scripts/ingest_pluto.py
python scripts/enrich_landmarks.py
```

### Pre-cache Reference Images

Pre-cache Street View images for top landmarks:

```bash
python scripts/precache_landmarks.py --top-n 5000
```

**Cost estimation**: ~$140 for 5,000 buildings × 4 directions @ $0.007/image

## Configuration

Key settings in `models/config.py`:

```python
max_scan_distance_meters = 100  # Max distance for candidates
cone_angle_degrees = 60         # View cone width
confidence_threshold = 0.70     # Auto-select threshold
landmark_boost_factor = 1.05    # Boost for landmarks
proximity_boost_factor = 1.10   # Boost for nearby buildings
```

## Performance Targets

- **Total scan time**: < 3 seconds
- **Geospatial query**: < 200ms
- **Reference image fetch**: < 800ms (5 candidates in parallel)
- **CLIP comparison**: < 1500ms (5 candidates)

## Monitoring & Analytics

### Scan Metrics

Track in `scans` table:
- Processing times (geospatial, fetch, CLIP)
- Candidate counts
- Confidence scores
- User confirmations
- Match accuracy

### Cache Metrics

Track in `cache_stats` table:
- Cache hit rate
- Daily fetch counts
- Cost tracking
- Average fetch times

## Deployment

### Railway.app

```bash
railway login
railway init
railway up
```

### Fly.io

```bash
fly launch
fly deploy
```

### Environment Variables

Set in your deployment platform:
```bash
railway variables set SUPABASE_URL=...
railway variables set GOOGLE_MAPS_API_KEY=...
# etc.
```

## Troubleshooting

### CLIP Model Not Loading
- Check CUDA availability: `python -c "import torch; print(torch.cuda.is_available())"`
- If no GPU, set `CLIP_DEVICE=cpu` in `.env`

### Street View Images Not Fetching
- Verify Google Maps API key has Street View Static API enabled
- Check API quotas and billing
- Test with `/api/debug/test-street-view`

### Database Connection Issues
- Verify `DATABASE_URL` format
- Check Supabase connection pooler settings
- Enable PostGIS extension

### Geospatial Queries Slow
- Ensure spatial indexes are created: `CREATE INDEX ON buildings USING GIST(geom)`
- Check `EXPLAIN ANALYZE` on queries
- Consider reducing `max_scan_distance` or `cone_angle`

## Cost Estimates

### Development (per month)
- Database: Free (Supabase free tier)
- Storage: Free (R2 10GB free)
- Redis: Free (Upstash 10K commands/day)
- Street View: Variable (~$50-100 for testing)

### Production (per month)
- Database: $25 (Supabase Pro)
- Storage: $5 (R2 beyond free tier)
- Redis: $10 (Upstash paid tier)
- Backend hosting: $5-20 (Railway/Fly.io)
- Street View: ~$0.02 per scan (on cache miss)

### One-time Costs
- Pre-cache 5K buildings: ~$140

## Future Improvements

1. **Mapillary Integration**: Reduce Street View costs by using free Mapillary images
2. **Image Embeddings**: Pre-compute and store CLIP embeddings for faster matching
3. **Model Fine-tuning**: Fine-tune CLIP on architectural images
4. **Multi-scale Search**: Combine multiple distance ranges
5. **Temporal Caching**: Update reference images seasonally
6. **User Uploads**: Allow users to contribute reference images
7. **LLM Descriptions**: Auto-generate building descriptions with Perplexity API

## Contributing

This is part of the NYC Architecture App project. See main README for contribution guidelines.

## License

[Your License]
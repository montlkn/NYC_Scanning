# NYC Scan Project Status

**Last Updated**: September 30, 2025

## 🎯 Project Overview

Building a point-and-scan building identification system for the NYC Architecture App. Users point their phone camera at a building, and the system identifies it using:
- GPS location
- Compass bearing
- Computer vision (CLIP image matching)
- Geospatial filtering (PostGIS)

## ✅ Completed Work

### Backend Infrastructure (Week 1 - Days 1-7)

#### 1. Project Setup ✓
- [x] Created backend directory structure
- [x] Set up requirements.txt with all dependencies
- [x] Created .env.example template
- [x] Created comprehensive README.md

#### 2. Database Models ✓
- [x] `Building` model (extends existing Supabase table)
- [x] `ReferenceImage` model (cached Street View images)
- [x] `Scan` model (user scan history)
- [x] `ScanFeedback` model (user feedback)
- [x] `CacheStat` model (performance tracking)
- [x] SQL migration script (001_add_scan_tables.sql)

#### 3. Configuration ✓
- [x] Pydantic settings management
- [x] Environment variable loading
- [x] Configurable parameters (cone angle, distance, confidence thresholds)

#### 4. Core Services ✓
- [x] **Geospatial Service** (`services/geospatial.py`)
  - Cone-of-vision WKT generation
  - PostGIS spatial queries
  - Bearing calculations
  - Relevance scoring
  - Distance calculations

- [x] **Reference Image Service** (`services/reference_images.py`)
  - Street View API integration
  - Bearing-based caching
  - Parallel image fetching
  - Availability checking
  - Pre-caching support

- [x] **CLIP Matcher Service** (`services/clip_matcher.py`)
  - OpenCLIP model initialization
  - Image embedding generation
  - Cosine similarity comparison
  - Confidence scoring with boosts
  - Batch processing support

- [x] **Storage Utilities** (`utils/storage.py`)
  - Cloudflare R2 integration
  - Image upload/download
  - Thumbnail generation
  - Public URL management

#### 5. API Endpoints ✓
- [x] **Scan Router** (`routers/scan.py`)
  - `POST /api/scan` - Main identification endpoint
  - `POST /api/scans/{id}/confirm` - User confirmation
  - `POST /api/scans/{id}/feedback` - User feedback
  - `GET /api/scans/{id}` - Scan details

- [x] **Buildings Router** (`routers/buildings.py`)
  - `GET /api/buildings/{bbl}` - Building details
  - `GET /api/buildings/{bbl}/images` - Reference images
  - `GET /api/buildings/nearby` - Proximity search
  - `GET /api/buildings/search` - Text search
  - `GET /api/buildings/top-landmarks` - Top landmarks
  - `GET /api/stats` - Database statistics

- [x] **Debug Router** (`routers/debug.py`)
  - `GET /api/debug/test-geospatial` - Test cone-of-vision
  - `GET /api/debug/test-clip` - Test CLIP model
  - `GET /api/debug/test-bearing` - Test bearing math
  - `POST /api/debug/test-image-comparison` - Compare images
  - `GET /api/debug/test-street-view` - Test Street View API
  - `GET /api/debug/config` - View configuration
  - `GET /api/debug/health-detailed` - Detailed health check

#### 6. FastAPI Application ✓
- [x] Main application (`main.py`)
- [x] CORS middleware
- [x] Request timing middleware
- [x] Global exception handling
- [x] Health check endpoints
- [x] Lifespan events for CLIP model initialization

## 🚧 In Progress

### Data Ingestion (Current Focus)
- [ ] PLUTO data loading script
- [ ] Landmark data enrichment script
- [ ] Data validation and cleanup
- [ ] Integration with existing Supabase buildings table

## 📋 Remaining Tasks

### Week 1 (Days 6-7) - Database Integration
- [ ] Set up async SQLAlchemy session management
- [ ] Create database connection pool
- [ ] Run SQL migration on Supabase
- [ ] Test database operations
- [ ] Implement data ingestion scripts
- [ ] Load PLUTO data (1.25M buildings)
- [ ] Enrich landmark data (3,712 buildings)

### Week 2 (Days 8-14) - Testing & Pre-caching
- [ ] Set up local PostgreSQL+PostGIS for development
- [ ] Test geospatial queries with real data
- [ ] Test Street View API integration
- [ ] Test CLIP model inference
- [ ] End-to-end API testing
- [ ] Build pre-caching script
- [ ] Pre-cache top 5,000 landmarks (4 directions each)
- [ ] Implement cost tracking
- [ ] Add Mapillary fallback (cost optimization)

### Week 3 (Days 15-21) - Mobile Integration
- [ ] **Mobile Scan Screen**
  - Replace placeholder ScanScreen.js
  - Integrate expo-camera with live preview
  - Add compass heading display
  - Implement pitch detection
  - Add crosshair UI
  - Add scan button with loading state
  - Implement photo capture

- [ ] **Results Screen**
  - Create ResultsScreen.js
  - Display top 3 matches with thumbnails
  - Show confidence scores
  - Implement building selection
  - Handle low-confidence scenarios
  - Add "none of these" option

- [ ] **Building Detail Integration**
  - Update BuildingDetailScreen with new API
  - Display scan-specific metadata
  - Add landmark badges
  - Show architect, style, materials
  - Add description when available

- [ ] **API Integration**
  - Connect mobile app to backend
  - Handle form-data uploads
  - Implement loading states
  - Add error handling
  - Add retry logic

### Deployment
- [ ] Set up production environment variables
- [ ] Deploy backend to Railway/Fly.io
- [ ] Configure Cloudflare R2 bucket
- [ ] Set up Redis cache (Upstash)
- [ ] Enable Google Maps Street View API
- [ ] Configure domain and SSL
- [ ] Set up monitoring and logging
- [ ] Performance testing in production

## 🎨 Architecture Decisions

### Why OpenCLIP?
- Open-source, LAION-trained models
- Better architectural image understanding than base CLIP
- 512-dim embeddings for fast similarity search
- Runs on CPU or GPU

### Why Cone-of-Vision?
- More accurate than simple radius search
- Considers user's viewing direction
- Reduces false positives
- Enables pitch-based tall building detection

### Why Street View Static API?
- High-quality, consistent imagery
- Known camera positions and bearings
- Wide coverage in NYC
- Can fallback to Mapillary for cost savings

### Why Cloudflare R2?
- S3-compatible API
- No egress fees
- Cheaper than S3
- Fast global CDN

## 📊 Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Total scan time | < 3s | Not tested |
| Geospatial query | < 200ms | Not tested |
| Reference image fetch | < 800ms | Not tested |
| CLIP comparison | < 1500ms | Not tested |
| Cache hit rate | > 80% | Not measured |
| Match accuracy | > 75% | Not measured |

## 💰 Cost Estimates

### Development Phase
- Database: Free (Supabase free tier)
- Storage: Free (R2 10GB free tier)
- Redis: Free (Upstash 10K commands/day)
- Street View testing: ~$50-100
- **Total**: ~$50-100

### One-time Pre-caching
- 5,000 buildings × 4 directions = 20,000 images
- $0.007 per Street View image
- **Total**: ~$140

### Production (Monthly)
- Database: $25 (Supabase Pro)
- Storage: $5 (R2 beyond free tier)
- Redis: $10 (Upstash paid tier)
- Backend hosting: $5-20 (Railway/Fly.io)
- Street View: ~$0.02 per scan (cache miss)
- **Total**: ~$45-60/month + variable per scan

## 🔧 Technical Challenges & Solutions

### Challenge 1: GPS Accuracy
**Problem**: GPS can be 5-30m off in urban canyons
**Solution**:
- Use compass bearing as primary filter
- Combine GPS + bearing for cone-of-vision
- Proximity boost for very close buildings
- Allow user to manually select from top 3

### Challenge 2: Street View Cost
**Problem**: $0.007 per image × 20K images = $140
**Solution**:
- Pre-cache only top 5,000 landmarks
- Cache 4 cardinal directions per building
- Implement Mapillary fallback (free)
- Smart bearing tolerance (±30°)
- Long cache TTL (30+ days)

### Challenge 3: CLIP Inference Speed
**Problem**: Image encoding is slow on CPU
**Solution**:
- Use ViT-B-32 (not L-14) for speed
- Run on GPU if available
- Pre-compute embeddings for reference images (future)
- Batch process multiple candidates
- Cache embeddings in database

### Challenge 4: Similar-looking Buildings
**Problem**: Many buildings look alike
**Solution**:
- Combine CLIP with geospatial filtering
- Boost landmarks (1.05x)
- Boost proximity (<30m = 1.10x)
- Show top 3 matches if confidence < 75%
- Track user confirmations for model improvement

## 🚀 Next Steps (Priority Order)

### 1. Immediate (This Week)
1. Set up database connection and session management
2. Run SQL migration on Supabase
3. Test geospatial queries with sample data
4. Test CLIP model loading and inference
5. Create data ingestion scripts

### 2. Short-term (Next Week)
1. Load PLUTO data into database
2. Enrich landmark data
3. Build pre-caching script
4. Pre-cache top 1,000 buildings for testing
5. End-to-end API testing
6. Start mobile screen implementation

### 3. Medium-term (Week 3)
1. Complete mobile integration
2. Full pre-caching (5,000 buildings)
3. Production deployment
4. Real-world testing in NYC
5. Performance optimization
6. User feedback collection

### 4. Future Enhancements
1. Mapillary integration for cost reduction
2. Pre-computed CLIP embeddings
3. LLM-generated building descriptions (Perplexity)
4. User-uploaded reference images
5. Model fine-tuning on architectural images
6. Augmented reality overlay
7. Historical photo comparison
8. Multi-building recognition

## 📝 Notes & Observations

### What Went Well
- Clean architecture with separated concerns
- Comprehensive error handling
- Debug endpoints for testing
- Flexible configuration system
- Good documentation

### What Needs Improvement
- Need database connection implementation
- Missing test suite
- No CI/CD pipeline yet
- Need cost tracking implementation
- Need monitoring/alerting setup

### Lessons Learned
- Supabase integration is straightforward
- CLIP model is larger than expected (~350MB)
- PostGIS spatial queries are powerful
- Street View API has good coverage but costs add up
- Cone-of-vision math is complex but necessary

## 🔗 Resources

### Documentation
- [FastAPI Docs](https://fastapi.tiangolo.com/)
- [OpenCLIP GitHub](https://github.com/mlfoundations/open_clip)
- [PostGIS Reference](https://postgis.net/docs/)
- [Google Street View Static API](https://developers.google.com/maps/documentation/streetview/overview)
- [Cloudflare R2 Docs](https://developers.cloudflare.com/r2/)

### Data Sources
- [NYC PLUTO](https://www.nyc.gov/site/planning/data-maps/open-data/dwn-pluto-mappluto.page)
- [NYC Landmarks](https://data.cityofnewyork.us/Housing-Development/LPC-Individual-Landmarks/ch5p-r223)
- Existing landmark CSV with scores (3,712 buildings)

### APIs & Services
- Supabase (database)
- Upstash (Redis)
- Cloudflare R2 (storage)
- Google Maps (Street View)
- Railway/Fly.io (hosting)

## 🙋 Questions & Decisions Needed

1. **Database Connection**: Use Supabase client or direct PostgreSQL connection?
   - Recommendation: Direct PostgreSQL with async SQLAlchemy for full control

2. **Pre-caching Strategy**: Cache all 5K immediately or gradually?
   - Recommendation: Start with 1K for testing, then expand

3. **Mobile Testing**: Test on iOS, Android, or both?
   - Recommendation: iOS first (simpler dev setup), then Android

4. **Deployment Platform**: Railway, Fly.io, or other?
   - Recommendation: Railway for simplicity, Fly.io for flexibility

5. **Monitoring**: Sentry, LogRocket, or built-in?
   - Recommendation: Start with Sentry for error tracking

## 📧 Contact & Support

For questions or issues, refer to the main architecture app repository.

---

**Status**: Backend foundation complete, moving to data integration and testing phase.
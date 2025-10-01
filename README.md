# NYC Scan - Point-and-Identify Building Recognition System

AI-powered building identification system for NYC architecture using computer vision, geospatial filtering, and multi-sensor fusion.

## 🏗️ Architecture

**Backend**: FastAPI + PostgreSQL/PostGIS + OpenCLIP
**Storage**: Cloudflare R2 (S3-compatible)
**Database**: Supabase (PostgreSQL + PostGIS 3.3)
**Vision**: OpenCLIP ViT-B-32 for image similarity
**Mobile**: React Native + Expo (separate repo)

## 🎯 How It Works

1. **User points camera** at NYC building
2. **Sensor fusion** combines GPS, barometer, compass, IMU → accurate 3D position
3. **Cone-of-vision query** filters to buildings in view (PostGIS ST_Contains)
4. **CLIP matching** compares user photo to cached Street View references
5. **Top match** returned with confidence score

### Sensor Stack
- **GPS** - Horizontal position (Kalman filtered)
- **Barometer** - Altitude/floor detection (±1 floor accuracy)
- **Magnetometer** - Compass bearing (0-360°)
- **Accelerometer + Gyroscope** - Dead reckoning when GPS lost
- **Kalman Filter** - Fuses all sensors for robust positioning

## 📁 Project Structure

```
nyc_scan/
├── backend/
│   ├── main.py                    # FastAPI app entry
│   ├── routers/                   # API endpoints
│   │   ├── scan.py               # /scan endpoint (main flow)
│   │   ├── buildings.py          # Building CRUD
│   │   └── debug.py              # Development helpers
│   ├── services/
│   │   ├── geospatial.py         # PostGIS cone-of-vision
│   │   ├── clip_matcher.py       # CLIP image comparison
│   │   └── reference_images.py   # Street View fetching
│   ├── models/
│   │   ├── database.py           # SQLAlchemy models
│   │   ├── session.py            # Async DB session
│   │   └── config.py             # Settings (Pydantic)
│   ├── utils/
│   │   └── storage.py            # Cloudflare R2 uploads
│   ├── scripts/
│   │   ├── precache_buildings.py       # Cache Street View images
│   │   └── reorganize_existing_r2.py   # Organize R2 storage
│   └── requirements.txt
└── README.md
```

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL with PostGIS extension
- Google Maps API key (Street View Static API)
- Cloudflare R2 account

### 1. Install Dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

Create `backend/.env`:

```bash
# Database (Supabase)
DATABASE_URL=postgresql://postgres.xxx:password@aws-0-us-east-1.pooler.supabase.com:5432/postgres

# Google Maps
GOOGLE_MAPS_API_KEY=AIzaSy...

# Cloudflare R2
R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY_ID=xxx
R2_SECRET_ACCESS_KEY=xxx
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-xxx.r2.dev

# App
ENV=development
DEBUG=true
CONFIDENCE_THRESHOLD=0.7
```

### 3. Run Backend

```bash
cd backend
source venv/bin/activate
python3 main.py
```

API will be available at `http://localhost:8000`

## 📊 Database Schema

**Buildings Table** (12,350 NYC buildings):
- `id` (UUID) - Primary key
- `des_addres` - Building address
- `geom` (MULTIPOLYGON) - Building footprint
- `style_prim` - Architectural style
- `arch_build` - Architect name
- `hist_dist` - Historic district

## 🗄️ R2 Storage Structure

```
reference/
└── buildings/
    ├── 110-west-74th-street/
    │   ├── 0deg.jpg
    │   ├── 45deg.jpg
    │   ├── ...
    │   ├── 315deg.jpg
    │   └── metadata.json
    ├── 1-pierrepont-street/
    └── ...

scans/
└── {scan_id}.jpg  # User photos (30-day expiration)
```

## 🔧 Key Scripts

### Pre-cache Buildings

```bash
python3 precache_buildings.py --limit 100 --delay 0.5
```

Fetches Street View images from 8 headings (0°, 45°, 90°, 135°, 180°, 225°, 270°, 315°) and uploads to R2.

### Reorganize R2 Storage

```bash
python3 reorganize_existing_r2.py --dry-run  # Preview changes
python3 reorganize_existing_r2.py            # Apply changes
python3 reorganize_existing_r2.py --delete-old  # Clean up old UUIDs
```

Migrates from UUID-based paths to human-readable structure with metadata.

## 🧪 Testing

```bash
# Test geospatial queries
python3 test_geospatial.py

# Test Street View API
python3 test_streetview.py

# Test R2 storage
python3 test_r2_storage.py
```

## 📡 API Endpoints

### POST `/scan`

Main building identification endpoint.

**Request** (multipart/form-data):
```json
{
  "photo": <file>,
  "gps_lat": 40.7484,
  "gps_lng": -73.9857,
  "compass_bearing": 45.0,
  "altitude": 10.5,
  "floor": 3,
  "confidence": 85,
  "movement_type": "stationary"
}
```

**Response**:
```json
{
  "scan_id": "uuid",
  "matches": [
    {
      "bbl": "1000010001",
      "address": "Empire State Building",
      "confidence": 0.92,
      "distance_meters": 25.5
    }
  ],
  "processing_time_ms": 450
}
```

## 🌍 Week 2 Progress (Completed)

- ✅ Database integration (Supabase PostgreSQL + PostGIS)
- ✅ Geospatial cone-of-vision queries
- ✅ Google Street View API integration
- ✅ Cloudflare R2 storage setup
- ✅ Pre-caching script (79 images cached)
- ✅ **NEW**: Multi-sensor fusion (GPS + Barometer + IMU + Kalman filter)
- ✅ **NEW**: R2 reorganization with readable paths + metadata

## 📱 Mobile App Integration

The mobile app uses full sensor fusion:

```javascript
import { PositionFusion, calculatePositionConfidence } from './utils/sensorFusion';

const fusion = new PositionFusion();

// GPS updates
fusion.updateGPS(lat, lng, altitude, accuracy);

// Barometer for floor detection
const { floor } = fusion.updateBarometer(pressureHPa);

// IMU for dead reckoning
fusion.updateIMU(accelerometer, gyroscope);

// Get position confidence (0-100)
const confidence = calculatePositionConfidence({
  hasGPS, gpsAccuracy, hasBarometer, hasIMU, timeSinceLastGPS
});
```

## 🚢 Next Steps (Week 3)

- [ ] Dockerize backend
- [ ] Deploy to Fly.io
- [ ] Connect mobile app to production API
- [ ] End-to-end testing
- [ ] Performance optimization

## 📝 License

MIT

## 🤝 Contributing

This is a learning project. Feel free to fork and experiment!

---

**Built with ❤️ for NYC architecture enthusiasts**

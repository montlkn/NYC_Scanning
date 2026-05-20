# Integration and Scaling Guide

This document covers observability tools, deployment options, and API integration for the NYC Scan building identification system.

## Table of Contents
1. [Observability Stack](#observability-stack)
2. [Modal Deployment](#modal-deployment)
3. [API Integration](#api-integration)
4. [Adding New Buildings](#adding-new-buildings)

---

## Observability Stack

### Current Setup: Sentry
Already integrated in `backend/main.py`:
```python
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    integrations=[FastApiIntegration()],
    traces_sample_rate=0.1,
    environment="production" if os.getenv("RENDER") else "development"
)
```
**Covers:** Error tracking, performance monitoring, stack traces.

---

### PostHog (Recommended)
**Purpose:** Product analytics, user behavior tracking

**When to add:** Before public launch

**Install:**
```bash
pip install posthog
```

**Integration:**
```python
# backend/services/analytics.py
import posthog

posthog.project_api_key = os.getenv('POSTHOG_API_KEY')
posthog.host = 'https://app.posthog.com'

def track_scan(scan_id: str, result: dict):
    """Track scan events for analytics"""
    posthog.capture(
        distinct_id=scan_id,
        event='building_scan',
        properties={
            'confidence': result.get('confidence'),
            'num_candidates': result.get('num_candidates'),
            'processing_time_ms': result.get('processing_time_ms'),
            'has_match': result.get('status') == 'match_found',
            'bin': result.get('bin'),
        }
    )

def track_confirmation(scan_id: str, confirmed_bin: str, was_top_match: bool):
    """Track user confirmations"""
    posthog.capture(
        distinct_id=scan_id,
        event='scan_confirmed',
        properties={
            'confirmed_bin': confirmed_bin,
            'was_top_match': was_top_match,
        }
    )
```

**Metrics to track:**
- Scan success rate (match found vs no candidates)
- Confidence score distribution
- Top match accuracy (confirmed vs suggested)
- Processing time by endpoint
- Geographic distribution of scans
- Most scanned buildings

---

### Prometheus + Grafana (Future)
**Purpose:** Real-time infrastructure monitoring, alerting

**When to add:** When scaling beyond 1000 daily active users

**Setup:**
```python
# backend/middleware/metrics.py
from prometheus_client import Counter, Histogram, generate_latest
import time

SCAN_REQUESTS = Counter('scan_requests_total', 'Total scan requests')
SCAN_DURATION = Histogram('scan_duration_seconds', 'Scan processing time')
CLIP_INFERENCE = Histogram('clip_inference_seconds', 'CLIP model inference time')
DB_QUERY_TIME = Histogram('db_query_seconds', 'Database query time')

@app.middleware("http")
async def metrics_middleware(request, call_next):
    start = time.time()
    response = await call_next(request)

    if '/api/scan' in request.url.path:
        SCAN_REQUESTS.inc()
        SCAN_DURATION.observe(time.time() - start)

    return response

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")
```

**Key metrics:**
- `scan_requests_total` - Total API calls
- `scan_duration_seconds` - End-to-end latency
- `clip_inference_seconds` - CLIP model time (GPU bound)
- `db_query_seconds` - Supabase query performance
- `reference_embeddings_count` - Database size

---

### Rollbar vs Sentry

| Feature | Sentry (Current) | Rollbar |
|---------|-----------------|---------|
| Error tracking | Yes | Yes |
| Performance monitoring | Yes | Limited |
| Session replay | Yes | No |
| FastAPI integration | Native | Requires wrapper |
| Pricing | Free tier generous | Similar |
| **Recommendation** | **Keep Sentry** | Redundant |

**Verdict:** Stick with Sentry. Adding Rollbar provides no additional value.

---

## Modal Deployment

### Why Modal?
- GPU access for CLIP inference
- Auto-scaling (pay per request)
- No Docker required
- Cold start ~2-5 seconds
- Direct Python deployment

### Basic Modal Setup

**Install:**
```bash
pip install modal
modal setup  # Authenticate
```

**Create `modal_app.py`:**
```python
import modal

# Define the image with all dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi",
        "uvicorn",
        "open-clip-torch",
        "torch",
        "pillow",
        "numpy",
        "httpx",
        "sqlalchemy[asyncio]",
        "psycopg[binary,pool]",
        "boto3",
        "python-dotenv",
    )
)

app = modal.App("nyc-scan-api", image=image)

# Mount the backend code
backend_mount = modal.Mount.from_local_dir(
    local_path="./backend",
    remote_path="/app/backend"
)

@app.function(
    gpu="T4",  # For CLIP inference
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    mounts=[backend_mount],
    timeout=60,
)
@modal.asgi_app()
def fastapi_app():
    import sys
    sys.path.insert(0, "/app/backend")

    from main import app
    return app
```

**Create secrets in Modal:**
```bash
modal secret create nyc-scan-secrets \
  DATABASE_URL=your_supabase_url \
  R2_ENDPOINT_URL=your_r2_url \
  R2_ACCESS_KEY_ID=your_key \
  R2_SECRET_ACCESS_KEY=your_secret \
  R2_BUCKET=your_bucket \
  R2_PUBLIC_URL=your_public_url \
  GOOGLE_MAPS_API_KEY=your_key \
  SENTRY_DSN=your_dsn
```

**Deploy:**
```bash
modal deploy modal_app.py
```

**Result:**
```
https://your-workspace--nyc-scan-api-fastapi-app.modal.run
```

### Modal Cost Estimate
- T4 GPU: $0.000164/second
- Average scan: 2 seconds = $0.00033
- 1000 scans/day = $0.33/day
- Cold start: Free (included in compute time)

### Local Development with Modal
```python
# For local testing with remote GPU
@app.local_entrypoint()
def main():
    # Run single test scan
    result = scan_building.remote(photo_bytes, gps_lat, gps_lng, bearing, pitch)
    print(result)
```

---

## API Integration

### Endpoints for App Integration

**1. Scan Building** - `POST /api/scan`
```typescript
// React Native / TypeScript example
interface ScanRequest {
  photo: File;
  gps_lat: number;
  gps_lng: number;
  compass_bearing: number;
  phone_pitch: number;
}

interface ScanResponse {
  status: 'match_found' | 'no_candidates' | 'error';
  scan_id: string;
  top_match?: {
    bin: string;
    address: string;
    confidence: number;
  };
  candidates: Array<{
    bin: string;
    address: string;
    confidence: number;
    distance_meters: number;
  }>;
  processing_time_ms: number;
}

async function scanBuilding(data: ScanRequest): Promise<ScanResponse> {
  const formData = new FormData();
  formData.append('photo', data.photo);
  formData.append('gps_lat', data.gps_lat.toString());
  formData.append('gps_lng', data.gps_lng.toString());
  formData.append('compass_bearing', data.compass_bearing.toString());
  formData.append('phone_pitch', data.phone_pitch.toString());

  const response = await fetch('https://api.nycscan.app/api/scan', {
    method: 'POST',
    body: formData,
  });

  return response.json();
}
```

**2. Confirm Match** - `POST /api/confirm-with-photo`
```typescript
interface ConfirmRequest {
  scan_id: string;
  confirmed_bin: string;
  photo_url: string;
  compass_bearing: number;
  phone_pitch: number;
}

async function confirmMatch(data: ConfirmRequest): Promise<void> {
  const params = new URLSearchParams({
    scan_id: data.scan_id,
    confirmed_bin: data.confirmed_bin,
    photo_url: data.photo_url,
    compass_bearing: data.compass_bearing.toString(),
    phone_pitch: data.phone_pitch.toString(),
  });

  await fetch(`https://api.nycscan.app/api/confirm-with-photo?${params}`, {
    method: 'POST',
  });
}

Now, here's the client-side JavaScript code you can use in your frontend to compress images before upload (for even better performance):
// Client-side image compression before upload
async function compressImage(file, maxSize = 1024, quality = 0.85) {
    return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = (e) => {
            const img = new Image();
            img.onload = () => {
                const canvas = document.createElement('canvas');
                
                // Calculate new dimensions (max 1024px)
                let width = img.width;
                let height = img.height;
                
                if (width > height && width > maxSize) {
                    height = (height / width) * maxSize;
                    width = maxSize;
                } else if (height > maxSize) {
                    width = (width / height) * maxSize;
                    height = maxSize;
                }
                
                canvas.width = width;
                canvas.height = height;
                
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, width, height);
                
                canvas.toBlob((blob) => {
                    resolve(new File([blob], file.name, {
                        type: 'image/jpeg',
                        lastModified: Date.now()
                    }));
                }, 'image/jpeg', quality);
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    });
}

// Usage in your upload function:
async function scanBuilding(photoFile, gpsData) {
    // Compress image before upload
    const compressedPhoto = await compressImage(photoFile);
    
    const formData = new FormData();
    formData.append('photo', compressedPhoto);
    formData.append('gps_lat', gpsData.latitude);
    formData.append('gps_lng', gpsData.longitude);
    formData.append('compass_bearing', gpsData.bearing);
    formData.append('phone_pitch', gpsData.pitch);
    
    const response = await fetch('/api/scan', {
        method: 'POST',
        body: formData
    });
    
    return await response.json();
}
```

### Mobile App Flow

```
1. User opens camera
   ↓
2. App captures:
   - Photo (resize to 1024px max)
   - GPS coordinates (from device)
   - Compass bearing (from device magnetometer)
   - Phone pitch (from accelerometer)
   ↓
3. Upload photo to R2 (or send directly)
   ↓
4. Call POST /api/scan
   ↓
5. Display results:
   - Top match with confidence
   - Top 3 candidates if cofidence is under 80%
   - "Not my building?" option
   ↓
6. User confirms correct building
   ↓
7. Call POST /api/confirm-with-photo
   ↓
8. Feedback loop improves future matches
```

### React Native Camera Integration
```typescript
import { Camera } from 'expo-camera';
import * as Location from 'expo-location';
import { Magnetometer, Accelerometer } from 'expo-sensors';

async function captureAndScan() {
  // Get GPS
  const location = await Location.getCurrentPositionAsync({});
  const { latitude, longitude } = location.coords;

  // Get compass bearing (simplified)
  const magnetometerData = await new Promise((resolve) => {
    const sub = Magnetometer.addListener((data) => {
      sub.remove();
      resolve(data);
    });
  });
  const bearing = Math.atan2(magnetometerData.y, magnetometerData.x) * (180 / Math.PI);

  // Get phone pitch
  const accelData = await new Promise((resolve) => {
    const sub = Accelerometer.addListener((data) => {
      sub.remove();
      resolve(data);
    });
  });
  const pitch = Math.asin(accelData.z) * (180 / Math.PI);

  // Capture photo
  const photo = await cameraRef.current.takePictureAsync({
    quality: 0.8,
    base64: false,
  });

  // Resize to 1024px max (client-side optimization)
  const resizedPhoto = await resizeImage(photo.uri, 1024);

  // Scan
  const result = await scanBuilding({
    photo: resizedPhoto,
    gps_lat: latitude,
    gps_lng: longitude,
    compass_bearing: bearing,
    phone_pitch: pitch,
  });

  return result;
}
```

### Error Handling
```typescript
async function scanWithRetry(data: ScanRequest, maxRetries = 3): Promise<ScanResponse> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const result = await scanBuilding(data);
      return result;
    } catch (error) {
      if (i === maxRetries - 1) throw error;
      await sleep(1000 * Math.pow(2, i)); // Exponential backoff
    }
  }
}
```

---

## Adding New Buildings

### 3-Step Process

**Step 1: Add to Database**
```sql
INSERT INTO buildings_full_merge_scanning (bin, bbl, address, borough, geocoded_lat, geocoded_lng)
VALUES ('1234567', '1234567890', '123 Main St', 'Manhattan', 40.7128, -74.0060);
```

**Step 2: Generate Street View Images**
```python
# Use existing script
python3 scripts/generate_reference_images_top500.py

# Or add individual building
async def add_single_building(bin_val, lat, lng):
    await cache_building(bin_val, lat, lng, "New Building", settings)
```

**Step 3: Generate CLIP Embeddings**
```bash
python3 scripts/generate_embeddings_bins.py
```

### Automated Pipeline (Future)
```python
# backend/scripts/add_building_pipeline.py
async def add_building_complete(bin_val, bbl, address, borough, lat, lng):
    """Full pipeline: DB → Images → Embeddings"""

    # 1. Insert into database
    await insert_building(bin_val, bbl, address, borough, lat, lng)

    # 2. Generate Street View images
    image_count = await cache_building(bin_val, lat, lng, address, settings)

    # 3. Generate embeddings
    await generate_embeddings_for_bin(bin_val)

    return {
        'bin': bin_val,
        'images_generated': image_count,
        'embeddings_created': image_count
    }
```

---

## Summary

| Component | Status | Priority | When to Implement |
|-----------|--------|----------|-------------------|
| Sentry | Done | - | Already integrated |
| PostHog | Not started | High | Before public launch |
| Prometheus/Grafana | Not started | Low | 1000+ DAU |
| Modal deployment | Not started | High | Replace local server |
| API integration | Ready | High | Connect to app |
| Building pipeline | Scripts exist | Medium | Automate for scale |

**Next Steps:**
1. Deploy to Modal (removes need for local server)
2. Add PostHog for user analytics
3. Connect mobile app to API endpoints
4. Automate building addition pipeline
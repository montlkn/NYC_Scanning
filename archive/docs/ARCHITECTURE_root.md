# Architecture: PostHog + Modal Integration

Visual overview of how the components work together.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│                        FRONTEND (React Native)                         │
│                    • Camera capture                                     │
│                    • GPS/Compass sensors                               │
│                    • Image compression                                 │
│                                                                         │
└────────────────────────────┬────────────────────────────────────────────┘
                             │ HTTPS
                             │
                             ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│                      MODAL API (Deployed)                             │
│              https://workspace--nyc-scan-api.modal.run                │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  FastAPI (main.py)                                             │  │
│  │  • Health checks                                               │  │
│  │  • CORS middleware                                             │  │
│  │  • Error handling                                              │  │
│  └────────┬─────────────────────────────────────────────────────┘  │
│           │                                                          │
│  ┌────────▼────────────────────────────────────────────────────┐  │
│  │  Routers                                                       │  │
│  │  • POST /api/scan          → scan.router                     │  │
│  │  • POST /api/confirm       → scan.router                     │  │
│  │  • POST /api/feedback      → scan.router                     │  │
│  │  • GET /api/buildings      → buildings.router                │  │
│  └────────┬────────────────────────────────────────────────────┘  │
│           │                                                          │
│  ┌────────▼───────────────────────────────────────────────────┐   │
│  │  Services                                                    │   │
│  │                                                              │   │
│  │  ┌──────────────────┐  ┌──────────────────┐               │   │
│  │  │ geospatial.py    │  │ reference_images │               │   │
│  │  │ • Cone-of-vision │  │ • Street View    │               │   │
│  │  │ • PostGIS        │  │ • Image matching │               │   │
│  │  └──────────────────┘  └──────────────────┘               │   │
│  │                                                              │   │
│  │  ┌──────────────────┐  ┌──────────────────┐               │   │
│  │  │ clip_matcher.py  │  │ analytics.py ✨  │               │   │
│  │  │ • CLIP inference │  │ • track_scan()   │               │   │
│  │  │ • Scoring        │  │ • track_confirm()│               │   │
│  │  │ (T4 GPU)         │  └──────────────────┘               │   │
│  │  └──────────────────┘                                      │   │
│  │                                                              │   │
│  └────────┬─────────────────────────────────────────────────┘   │
│           │                                                          │
│  ┌────────▼──────────────────────────────────────────────────┐   │
│  │  Dependencies                                              │   │
│  │  • Supabase (Database + PostGIS)                          │   │
│  │  • Redis (Caching)                                        │   │
│  │  • R2 (Image storage)                                     │   │
│  │  • Sentry (Error tracking)                                │   │
│  │  • PostHog (Analytics) ✨                                 │   │
│  └────────┬──────────────────────────────────────────────────┘   │
│           │                                                          │
└───────────┼──────────────────────────────────────────────────────┘
            │
            ├──→ Supabase PostgreSQL ← Buildings, Embeddings, Scans
            │
            ├──→ Redis ← Cached reference images
            │
            ├──→ R2 ← User photos, Street View images
            │
            ├──→ Sentry ← Error reports
            │
            └──→ PostHog ✨ ← Analytics events (building_scan, scan_confirmed)
                   │
                   └─→ PostHog Dashboard
                       • Real-time metrics
                       • Scan funnel analysis
                       • Confidence distribution
                       • Geographic heatmaps
```

## Data Flow: Scan Request

```
1. USER CAPTURE (Frontend)
   ├─ Photo (camera)
   ├─ GPS (latitude, longitude)
   ├─ Compass (bearing 0-360°)
   └─ Phone tilt (pitch angle)
        │
        ▼
2. IMAGE COMPRESSION
   └─ Reduce to max 1024px, JPEG quality 0.85
        │
        ▼
3. HTTP REQUEST
   └─ POST /api/scan (FormData with compressed photo)
        │
        ▼
4. BACKEND PROCESSING
   ├─ Upload photo to R2
   ├─ Geospatial filtering (cone-of-vision)
   ├─ Fetch reference images
   ├─ CLIP comparison (on T4 GPU)
   └─ Sort by confidence
        │
        ▼
5. ANALYTICS TRACKING
   ├─ call track_scan(scan_id, {
   │     confidence: 0.95,
   │     num_candidates: 45,
   │     processing_time_ms: 2150,
   │     status: 'match_found',
   │     bin: '1234567'
   │   })
   └─ PostHog receives event
        │
        ▼
6. RESPONSE
   └─ JSON with matches, confidence, processing time
        │
        ▼
7. FRONTEND DISPLAY
   ├─ Show top match if confidence >= 0.80
   └─ Show picker if confidence < 0.80
        │
        ▼
8. USER CONFIRMATION
   └─ User taps to confirm building
        │
        ▼
9. CONFIRMATION REQUEST
   └─ POST /api/scans/{scan_id}/confirm
        │
        ▼
10. CONFIRMATION TRACKING
    ├─ call track_confirmation(scan_id, confirmed_bin, was_top_match)
    └─ PostHog receives event
         │
         ▼
11. DASHBOARD UPDATE
    └─ PostHog shows new metrics
```

## PostHog Analytics Integration

### Event: `building_scan`
Triggered after every scan completes.

```typescript
track_scan(scan_id, {
  confidence: number,           // 0-1 confidence score
  num_candidates: number,       // How many buildings in view
  processing_time_ms: number,   // Total time
  status: 'match_found' | 'no_candidates',
  bin: string,                  // Top match BIN if found
})

// Example event in PostHog:
{
  event: 'building_scan',
  timestamp: '2024-11-16T15:30:45Z',
  properties: {
    confidence: 0.92,
    num_candidates: 27,
    processing_time_ms: 2340,
    status: 'match_found',
    bin: '1012567'
  },
  distinct_id: 'scan-uuid-here'
}
```

### Event: `scan_confirmed`
Triggered when user confirms a building.

```typescript
track_confirmation(scan_id, confirmed_bin, was_top_match)

// Example event in PostHog:
{
  event: 'scan_confirmed',
  timestamp: '2024-11-16T15:30:50Z',
  properties: {
    confirmed_bin: '1012567',
    was_top_match: true
  },
  distinct_id: 'scan-uuid-here'
}
```

## Modal Deployment Infrastructure

```
┌──────────────────────────────────────────────────────────────┐
│                    Modal.com (Cloud)                         │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  App: nyc-scan-api                                          │
│  ├─ Container: Debian Python 3.11                           │
│  ├─ GPU: T4 (shared, auto-scaling)                          │
│  ├─ Memory: 10GB+ (auto-scaled)                             │
│  ├─ Timeout: 60 seconds per request                         │
│  ├─ Dependencies: All pre-installed in image                │
│  ├─ Code: /app/backend mounted from local                   │
│  ├─ Secrets: From Modal secret store                        │
│  └─ Scaling: Auto-scales from 0 to N instances             │
│                                                               │
│  Endpoints:                                                   │
│  ├─ GET /health                    (health check)           │
│  ├─ POST /api/scan                 (main inference)         │
│  ├─ POST /api/scans/{id}/confirm   (user feedback)          │
│  ├─ GET /api/buildings             (reference data)         │
│  └─ GET /metrics                   (Prometheus metrics)     │
│                                                               │
│  Pricing (T4 GPU):                                          │
│  ├─ Compute: $0.000164/second                               │
│  ├─ Avg scan: 2 seconds = $0.00033                          │
│  ├─ 1000 scans/day = $0.30/day                              │
│  └─ Free tier: 50GB/month = ~50K scans                      │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

### Frontend (React Native)
```
✓ Capture photo from camera
✓ Get GPS location
✓ Get compass bearing
✓ Get phone tilt/pitch
✓ Compress image before upload
✓ Call /api/scan endpoint
✓ Handle errors with retry logic
✓ Display results
✓ Get user confirmation
✓ Call /api/scans/{id}/confirm
✓ Send feedback
```

### Backend (FastAPI on Modal)
```
✓ Receive photo + GPS + compass
✓ Validate inputs
✓ Upload photo to R2
✓ Query buildings in cone-of-vision (PostGIS)
✓ Fetch reference images from R2/cache
✓ Run CLIP inference on T4 GPU
✓ Score and rank matches
✓ Track scan in PostHog
✓ Return matches + confidence
✓ Receive confirmation
✓ Track confirmation in PostHog
✓ Receive feedback
✓ Store in database
```

### PostHog (Analytics)
```
✓ Receive building_scan events
✓ Track confidence distribution
✓ Calculate success rate
✓ Show geographic heatmap
✓ Track top buildings
✓ Receive scan_confirmed events
✓ Calculate confirmation rate
✓ Build conversion funnels
✓ Show trends over time
✓ Alert on anomalies
```

## Deployment Checklist

```
┌─────────────────────────────────────────────────────────┐
│  Installation (local, one-time)                         │
├─────────────────────────────────────────────────────────┤
│  ☐ pip install modal                                    │
│  ☐ modal setup (authenticate)                           │
│  ☐ Get PostHog API key from posthog.com                │
│  └─ Get all secrets from backend/.env                   │
│                                                          │
├─────────────────────────────────────────────────────────┤
│  Deployment (one-time)                                  │
├─────────────────────────────────────────────────────────┤
│  ☐ Create Modal secret: modal secret create ...         │
│  ☐ Deploy API: modal deploy modal_app.py                │
│  ☐ Copy Modal URL from output                           │
│  ☐ Test health endpoint: curl /health                   │
│  └─ Update frontend API_BASE_URL                        │
│                                                          │
├─────────────────────────────────────────────────────────┤
│  Frontend Integration (app development)                 │
├─────────────────────────────────────────────────────────┤
│  ☐ Copy API functions from docs/API_INTEGRATION.md     │
│  ☐ Copy TypeScript types                                │
│  ☐ Implement camera integration                         │
│  ☐ Test scanBuilding() function                         │
│  ☐ Test confirmBuilding() function                      │
│  ☐ Test error handling with retry                       │
│  └─ Deploy app to App Store / Google Play               │
│                                                          │
├─────────────────────────────────────────────────────────┤
│  Monitoring (ongoing)                                   │
├─────────────────────────────────────────────────────────┤
│  ☐ Check PostHog events appear in dashboard             │
│  ☐ Monitor Modal logs: modal tail nyc-scan-api          │
│  ☐ Check Sentry for errors                              │
│  ☐ Monitor confidence scores                            │
│  ☐ Track confirmation rate                              │
│  └─ Optimize based on metrics                           │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## Technology Stack Summary

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | React Native / Expo | Mobile app |
| **API** | FastAPI | REST endpoints |
| **Compute** | Modal + T4 GPU | Inference scaling |
| **ML Model** | OpenCLIP | Image similarity |
| **Database** | Supabase (PostgreSQL + PostGIS) | Building data |
| **Cache** | Redis | Image cache |
| **Storage** | Cloudflare R2 | Photo storage |
| **Analytics** | PostHog | User behavior |
| **Errors** | Sentry | Error tracking |
| **Maps** | Google Maps API | Geocoding |

## Key Metrics Tracked

### Building Scan Event
```
Properties collected:
├─ Confidence (0-1): How sure is the match?
├─ Num Candidates: How many buildings in view?
├─ Processing Time: How long did it take?
├─ Status: Did we find a match?
└─ BIN: Which building was matched?

Derived metrics:
├─ Success Rate: % with status='match_found'
├─ Avg Confidence: Mean confidence of matches
├─ P95 Processing Time: 95th percentile latency
├─ Most Scanned Buildings: Top BINs
└─ Geographic Distribution: Heatmap of GPS coords
```

### Scan Confirmation Event
```
Properties collected:
├─ Confirmed BIN: Which building user selected
└─ Was Top Match: Was it the top result?

Derived metrics:
├─ Confirmation Rate: % of scans that get confirmed
├─ Accuracy: % where confirmed = top match
├─ User Journey: Scan → Confirm → Feedback funnel
└─ Cohort Analysis: Behavior by confidence threshold
```

## Next: Optimization Opportunities

```
After deploying and collecting data:

1. ML Model Improvements
   ├─ Retrain CLIP on confirmed wrong matches
   ├─ Fine-tune confidence thresholds
   └─ Add multi-angle reference images

2. Infrastructure Optimization
   ├─ Switch to L4 GPU if latency matters
   ├─ Implement reference image pre-caching
   ├─ Add CDN for faster image delivery
   └─ Implement batch inference

3. Product Improvements
   ├─ Show confidence-based UI feedback
   ├─ Implement 3D model visualization
   ├─ Add building info from Wikipedia/OpenStreetMap
   └─ Gamify the building database growth

4. Analytics Enhancements
   ├─ Cohort analysis by location
   ├─ A/B test UI improvements
   ├─ Predict at-risk users
   └─ Attribution tracking
```

---

This architecture is scalable, observable, and production-ready! 🚀

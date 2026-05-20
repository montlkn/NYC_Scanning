# API Reference

Complete API documentation for NYC Scan backend endpoints.

**Base URL**: `https://nyc-scan-api.onrender.com` (Production)
**Local Dev**: `http://localhost:8000`

## Table of Contents

- [Scanning Endpoints](#scanning-endpoints)
- [Building Data Endpoints](#building-data-endpoints)
- [Debug Endpoints](#debug-endpoints)
- [Response Codes](#response-codes)
- [Rate Limiting](#rate-limiting)

---

## Scanning Endpoints

### POST /api/scan

**Main scanning endpoint** - Identifies building from photo + GPS + sensor data.

Uses Main Supabase DB with on-demand Street View fetching. Slower but comprehensive coverage.

**Request:**

```bash
curl -X POST https://nyc-scan-api.onrender.com/api/scan \
  -F "photo=@building.jpg" \
  -F "gps_lat=40.7484" \
  -F "gps_lng=-73.9857" \
  -F "compass_bearing=45" \
  -F "phone_pitch=10" \
  -F "phone_roll=0" \
  -F "altitude=12.5" \
  -F "floor=3" \
  -F "confidence=85" \
  -F "movement_type=stationary" \
  -F "gps_accuracy=5.2"
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `photo` | File | Yes | Building photo (JPEG/PNG, max 10MB) |
| `gps_lat` | float | Yes | User's GPS latitude (-90 to 90) |
| `gps_lng` | float | Yes | User's GPS longitude (-180 to 180) |
| `compass_bearing` | float | Yes | Compass heading in degrees (0-360, 0=North) |
| `phone_pitch` | float | No | Phone pitch angle (-90 to 90, default: 0) |
| `phone_roll` | float | No | Phone roll angle (default: 0) |
| `altitude` | float | No | Altitude in meters from barometer |
| `floor` | int | No | Estimated floor number |
| `confidence` | int | No | Position confidence score (0-100) |
| `movement_type` | string | No | `stationary`\|`walking`\|`running` |
| `gps_accuracy` | float | No | GPS accuracy in meters |
| `user_id` | string | No | Optional user ID for tracking |

**Response (200 OK):**

```json
{
  "scan_id": "550e8400-e29b-41d4-a716-446655440000",
  "matches": [
    {
      "bbl": "1008350041",
      "building_name": "Empire State Building",
      "address": "350 Fifth Avenue",
      "borough": "Manhattan",
      "confidence": 0.89,
      "distance_meters": 23.4,
      "bearing_to_building": 42,
      "is_landmark": true,
      "architectural_style": "Art Deco",
      "year_built": 1931,
      "num_floors": 102
    },
    {
      "bbl": "1012890036",
      "building_name": "Lever House",
      "address": "390 Park Avenue",
      "borough": "Manhattan",
      "confidence": 0.76,
      "distance_meters": 45.2,
      "bearing_to_building": 38,
      "is_landmark": true,
      "architectural_style": "International Style",
      "year_built": 1952,
      "num_floors": 24
    }
  ],
  "candidates_count": 15,
  "processing_time_ms": 2340,
  "timing": {
    "upload_ms": 320,
    "geospatial_ms": 125,
    "reference_fetch_ms": 1450,
    "clip_matching_ms": 445
  }
}
```

**Response (200 OK - No candidates):**

```json
{
  "scan_id": "550e8400-e29b-41d4-a716-446655440000",
  "error": "no_candidates",
  "message": "No buildings found in your view. Try getting closer or adjusting your angle.",
  "matches": [],
  "processing_time_ms": 450
}
```

**Error Responses:**

```json
// 400 Bad Request - Invalid parameters
{
  "detail": "Invalid latitude"
}

// 500 Internal Server Error
{
  "detail": "Failed to process scan",
  "scan_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### POST /api/phase1/scan

**Fast scanning endpoint** - Ultra-fast building identification using pre-computed embeddings.

Uses Phase 1 Postgres DB with pgvector. <100ms response time. Tier 1 buildings only (~5k).

**Request:**

```bash
curl -X POST https://nyc-scan-api.onrender.com/api/phase1/scan \
  -F "photo=@building.jpg" \
  -F "lat=40.7484" \
  -F "lng=-73.9857" \
  -F "bearing=45" \
  -F "pitch=10" \
  -F "gps_accuracy=5.2"
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `photo` | File | Yes | Building photo (JPEG/PNG, max 10MB) |
| `lat` | float | Yes | User's GPS latitude |
| `lng` | float | Yes | User's GPS longitude |
| `bearing` | float | Yes | Compass heading (0-360) |
| `pitch` | float | Yes | Phone pitch angle |
| `gps_accuracy` | float | Yes | GPS accuracy in meters |

**Response (200 OK):**

```json
{
  "building": {
    "building_id": 123,
    "bbl": "1008350041",
    "name": "Empire State Building",
    "address": "350 Fifth Avenue",
    "score": 0.92,
    "lat": 40.7484,
    "lng": -73.9857
  },
  "latency_ms": 87,
  "candidates_count": 8,
  "match_details": {
    "matched_angle": 180,
    "confidence": "high"
  }
}
```

**Response (200 OK - No buildings nearby):**

```json
{
  "error": "No buildings nearby",
  "latency_ms": 23
}
```

**Response (200 OK - No matches found):**

```json
{
  "error": "No matches found",
  "latency_ms": 65
}
```

---

## Building Data Endpoints

### GET /api/buildings/{bbl}

**Get detailed building information by BBL.**

**Request:**

```bash
curl https://nyc-scan-api.onrender.com/api/buildings/1008350041
```

**Response (200 OK):**

```json
{
  "bbl": "1008350041",
  "address": "350 Fifth Avenue",
  "borough": "Manhattan",
  "latitude": 40.7484,
  "longitude": -73.9857,
  "year_built": 1931,
  "num_floors": 102,
  "height_ft": 1454,
  "building_class": "O4",
  "is_landmark": true,
  "landmark_name": "Empire State Building",
  "architect": "Shreve, Lamb & Harmon [William Lamb]",
  "style_primary": "Art Deco",
  "style_secondary": null,
  "materials": ["Limestone", "Granite", "Aluminum"],
  "final_score": 100.0,
  "historical_score": 98.5,
  "visual_score": 95.2,
  "cultural_score": 100.0,
  "walk_score": 98.5,
  "description": "The Empire State Building is a 102-story Art Deco skyscraper...",
  "description_sources": ["Wikipedia", "NYC Landmarks", "AIA Guide"]
}
```

**Response (404 Not Found):**

```json
{
  "detail": "Building not found"
}
```

---

### GET /api/buildings/{bbl}/images

**Get all reference images for a building.**

**Request:**

```bash
curl https://nyc-scan-api.onrender.com/api/buildings/1008350041/images
```

**Response (200 OK):**

```json
{
  "bbl": "1008350041",
  "images": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "url": "https://pub-...r2.dev/reference/buildings/empire-state-building/0deg.jpg",
      "thumbnail_url": "https://pub-...r2.dev/reference/buildings/empire-state-building/0deg_thumb.jpg",
      "source": "street_view",
      "compass_bearing": 0,
      "capture_lat": 40.7484,
      "capture_lng": -73.9857,
      "created_at": "2024-10-01T15:30:00Z"
    },
    {
      "id": "550e8400-e29b-41d4-a716-446655440002",
      "url": "https://pub-...r2.dev/reference/buildings/empire-state-building/90deg.jpg",
      "source": "street_view",
      "compass_bearing": 90,
      "created_at": "2024-10-01T15:30:05Z"
    }
  ],
  "count": 8
}
```

---

### GET /api/buildings/nearby

**Get buildings near a location (simple radius search).**

**Request:**

```bash
curl "https://nyc-scan-api.onrender.com/api/buildings/nearby?lat=40.7484&lng=-73.9857&radius_meters=100&landmarks_only=true&limit=10"
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `lat` | float | Yes | Latitude |
| `lng` | float | Yes | Longitude |
| `radius_meters` | float | No | Search radius (10-500m, default: 100) |
| `limit` | int | No | Max results (1-100, default: 20) |
| `landmarks_only` | bool | No | Return only landmarks (default: false) |

**Response (200 OK):**

```json
{
  "location": {
    "lat": 40.7484,
    "lng": -73.9857
  },
  "radius_meters": 100,
  "buildings": [
    {
      "bbl": "1008350041",
      "address": "350 Fifth Avenue",
      "building_name": "Empire State Building",
      "distance_meters": 0,
      "is_landmark": true,
      "year_built": 1931
    }
  ],
  "count": 1
}
```

---

### GET /api/buildings/search

**Search buildings by address, landmark name, or architect.**

**Request:**

```bash
curl "https://nyc-scan-api.onrender.com/api/buildings/search?q=empire&borough=Manhattan&landmarks_only=true&limit=10"
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `q` | string | Yes | Search query (min 2 chars) |
| `borough` | string | No | Filter by borough |
| `landmarks_only` | bool | No | Landmarks only (default: false) |
| `limit` | int | No | Max results (1-100, default: 20) |

**Response (200 OK):**

```json
{
  "query": "empire",
  "buildings": [
    {
      "bbl": "1008350041",
      "address": "350 Fifth Avenue",
      "building_name": "Empire State Building",
      "borough": "Manhattan",
      "is_landmark": true,
      "architectural_style": "Art Deco",
      "year_built": 1931
    }
  ],
  "count": 1
}
```

---

### GET /api/buildings/top-landmarks

**Get top-rated landmark buildings.**

**Request:**

```bash
curl "https://nyc-scan-api.onrender.com/api/buildings/top-landmarks?limit=100&borough=Manhattan"
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | int | No | Max results (1-500, default: 100) |
| `borough` | string | No | Filter by borough |

**Response (200 OK):**

```json
{
  "landmarks": [
    {
      "bbl": "1012890036",
      "building_name": "Lever House",
      "address": "390 Park Avenue",
      "final_score": 100.0,
      "year_built": 1950,
      "architectural_style": "International Style"
    },
    {
      "bbl": "1013070001",
      "building_name": "Seagram Building",
      "address": "375 Park Avenue",
      "final_score": 100.0,
      "year_built": 1955,
      "architectural_style": "International Style"
    }
  ],
  "count": 100
}
```

---

### GET /api/stats

**Get database statistics.**

**Request:**

```bash
curl https://nyc-scan-api.onrender.com/api/stats
```

**Response (200 OK):**

```json
{
  "total_buildings": 860245,
  "total_landmarks": 5342,
  "total_reference_images": 42736,
  "buildings_with_reference_images": 5342,
  "avg_images_per_building": 8.0,
  "cache_coverage_percent": 0.62,
  "last_updated": "2024-10-01T12:00:00Z"
}
```

---

## Debug Endpoints

### GET /api/debug/health

**Health check endpoint.**

**Response (200 OK):**

```json
{
  "status": "healthy",
  "timestamp": "2024-10-01T15:30:00Z",
  "version": "1.0.0"
}
```

---

## Response Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad Request - Invalid parameters |
| 404 | Not Found - Resource doesn't exist |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Server is down |

---

## Rate Limiting

**Current Limits:**
- **Per IP**: 100 requests per minute
- **Per User** (future): 1000 requests per hour

**Rate Limit Headers:**

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1633099200
```

**Rate Limit Exceeded Response (429):**

```json
{
  "detail": "Rate limit exceeded. Try again in 45 seconds.",
  "retry_after": 45
}
```

---

## Error Handling

All errors follow this format:

```json
{
  "detail": "Error message describing what went wrong",
  "error_code": "OPTIONAL_ERROR_CODE",
  "timestamp": "2024-10-01T15:30:00Z"
}
```

Common error codes:
- `INVALID_COORDINATES` - GPS coordinates out of range
- `NO_CANDIDATES` - No buildings found in search area
- `PHOTO_TOO_LARGE` - Uploaded photo exceeds 10MB
- `UNSUPPORTED_FORMAT` - Photo format not supported
- `RATE_LIMIT_EXCEEDED` - Too many requests

---

## Authentication (Future)

Currently, the API is open (no authentication required). Future versions will support:

- **API Keys**: For programmatic access
- **OAuth 2.0**: For user-based authentication
- **JWT Tokens**: For mobile app sessions

---

## SDKs & Client Libraries

**Coming Soon:**
- Python SDK
- JavaScript/TypeScript SDK
- React Native SDK (for mobile app)

---

## Changelog

### v1.0.0 (Current)
- Initial API release
- Main scan endpoint (`/api/scan`)
- Phase 1 fast scan endpoint (`/api/phase1/scan`)
- Building detail endpoints
- Search and filtering

### v0.9.0 (Beta)
- Development build
- Testing endpoints

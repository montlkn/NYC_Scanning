# NYC Scan System V2: Bulletproof Building Identification

## Executive Summary

This document describes the complete architecture for a flawless building identification system that can identify **any building in NYC** (1.08M buildings) using GPS, compass, and building footprint geometry as the primary method, with on-demand CLIP visual matching only for disambiguation.

### Key Improvements Over V1

| Aspect | V1 (Current) | V2 (New) |
|--------|--------------|----------|
| **Coverage** | 485 buildings with embeddings | 1,083,146 buildings (100% NYC) |
| **Primary Method** | CLIP embedding matching | GPS + footprint intersection |
| **CLIP Role** | Primary (expensive, limited) | Disambiguation only (cheap, targeted) |
| **Accuracy** | Depends on reference image quality | Deterministic geometry math |
| **Cost per scan** | ~$0.05-0.10 (Modal + API calls) | ~$0.005 average |
| **Speed** | 2-5 seconds | <500ms for 90% of scans |
| **Reliability** | Fails without embeddings | Works for every building |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         USER SCANS BUILDING                                 │
│                    (Photo + GPS + Compass Bearing)                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STEP 1: FOOTPRINT CONE QUERY                             │
│                         (PostGIS, <50ms)                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. Build view cone polygon from user position + bearing                    │
│  2. Query building_footprints for intersection                              │
│  3. Calculate visibility scores for each candidate                          │
│  4. Return ranked candidates                                                │
│                                                                             │
│  SQL:                                                                       │
│  SELECT bin, address, height_roof,                                          │
│         ST_Distance(centroid::geography, user_point::geography) as dist,    │
│         ST_Area(ST_Intersection(footprint, cone)) as visible_area           │
│  FROM building_footprints                                                   │
│  WHERE ST_Intersects(footprint, cone_polygon)                               │
│  ORDER BY score DESC                                                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STEP 2: RESULT CLASSIFICATION                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  CASE A: SINGLE BUILDING (est. 40% of scans)                                │
│  ─────────────────────────────────────────────                              │
│  Only one building footprint intersects the cone.                           │
│  → Return immediately with 95%+ confidence                                  │
│  → No CLIP, no picker, instant result                                       │
│                                                                             │
│  CASE B: CLEAR WINNER (est. 35% of scans)                                   │
│  ─────────────────────────────────────────────                              │
│  Multiple buildings, but one has significantly higher score:                │
│  - Much closer distance                                                     │
│  - Better bearing alignment                                                 │
│  - Larger visible facade area                                               │
│  → Return top match with 85%+ confidence                                    │
│  → Show picker for confirmation (optional)                                  │
│                                                                             │
│  CASE C: AMBIGUOUS (est. 20% of scans)                                      │
│  ─────────────────────────────────────────                                  │
│  2-3 buildings with similar scores. Common scenarios:                       │
│  - Corner buildings                                                         │
│  - Dense urban blocks                                                       │
│  - User between two buildings                                               │
│  → Proceed to STEP 3 (CLIP disambiguation)                                  │
│                                                                             │
│  CASE D: NO BUILDINGS (est. 5% of scans)                                    │
│  ─────────────────────────────────────────                                  │
│  No footprints intersect the cone. Causes:                                  │
│  - User in park/plaza                                                       │
│  - GPS drift                                                                │
│  - Pointing at sky                                                          │
│  → Expand search radius                                                     │
│  → Suggest user contribution if still no results                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ (Only CASE C)
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STEP 3: ON-DEMAND CLIP DISAMBIGUATION                    │
│                         (Only for ambiguous cases)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  For each ambiguous candidate (2-3 buildings max):                          │
│                                                                             │
│  3.1 CHECK EXISTING EMBEDDINGS                                              │
│  ─────────────────────────────────                                          │
│  Query reference_embeddings table for pre-computed vectors.                 │
│  If found → use directly (free, fast)                                       │
│                                                                             │
│  3.2 CHECK USER CONTRIBUTION IMAGES                                         │
│  ─────────────────────────────────────                                      │
│  Query user_images table for community photos.                              │
│  If found → compute embedding on-the-fly                                    │
│                                                                             │
│  3.3 FETCH STREET VIEW ON-DEMAND (last resort)                              │
│  ─────────────────────────────────────────────                              │
│  For candidates without any reference:                                      │
│  - Calculate optimal camera heading (user → building centroid)              │
│  - Fetch ONE Street View image per building                                 │
│  - Compute CLIP embedding                                                   │
│  - Cache embedding for future use                                           │
│  - Cost: $0.007 per image                                                   │
│                                                                             │
│  3.4 COMPARE AND SELECT                                                     │
│  ───────────────────────                                                    │
│  - Encode user's photo with CLIP                                            │
│  - Compute cosine similarity against each candidate                         │
│  - Select highest similarity as winner                                      │
│  - Apply confidence based on similarity gap                                 │
│                                                                             │
│  Cost Analysis (for CASE C only):                                           │
│  - With embeddings: $0 (just vector comparison)                             │
│  - Without embeddings: $0.007-0.021 (1-3 Street View fetches)               │
│  - Average: ~$0.01 per ambiguous scan                                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STEP 4: RETURN RESULT                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Response payload:                                                          │
│  {                                                                          │
│    "scan_id": "uuid",                                                       │
│    "matches": [                                                             │
│      {                                                                      │
│        "bin": "1234567",                                                    │
│        "address": "123 Main St",                                            │
│        "confidence": 92.5,                                                  │
│        "distance_meters": 25.3,                                             │
│        "bearing_difference": 5.2,                                           │
│        "verification_method": "footprint_single"                            │
│      }                                                                      │
│    ],                                                                       │
│    "show_picker": false,                                                    │
│    "verification_method": "footprint|clip|hybrid",                          │
│    "processing_time_ms": 150,                                               │
│    "can_contribute": true                                                   │
│  }                                                                          │
│                                                                             │
│  Verification Methods:                                                      │
│  - "footprint_single": Only one building in cone (highest confidence)       │
│  - "footprint_winner": Clear geometric winner                               │
│  - "clip_disambiguation": CLIP broke the tie                                │
│  - "user_selected": User picked from ambiguous options                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema

### New Table: `building_footprints`

This table stores the geometry for ALL NYC buildings (1.08M rows).

```sql
-- Migration: 20251213_building_footprints.sql

-- Enable PostGIS if not already enabled
CREATE EXTENSION IF NOT EXISTS postgis;

-- Create building footprints table
CREATE TABLE IF NOT EXISTS building_footprints (
    bin TEXT PRIMARY KEY,
    bbl TEXT,
    name TEXT,
    footprint GEOMETRY(MULTIPOLYGON, 4326),
    centroid GEOMETRY(POINT, 4326),
    height_roof FLOAT,
    ground_elevation FLOAT,
    shape_area FLOAT,
    construction_year INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Critical indexes for spatial queries
CREATE INDEX IF NOT EXISTS idx_footprints_footprint_gist
    ON building_footprints USING GIST (footprint);

CREATE INDEX IF NOT EXISTS idx_footprints_centroid_gist
    ON building_footprints USING GIST (centroid);

CREATE INDEX IF NOT EXISTS idx_footprints_bbl
    ON building_footprints (bbl);

-- Index for height-based queries
CREATE INDEX IF NOT EXISTS idx_footprints_height
    ON building_footprints (height_roof DESC NULLS LAST);

-- Analyze for query optimization
ANALYZE building_footprints;
```

### Data Loading

Load from `BUILDING_20251104.csv` (NYC Open Data Building Footprints):

```python
# scripts/load_building_footprints.py
# Loads 1.08M building footprints into PostGIS

# CSV columns used:
# - the_geom: MULTIPOLYGON WKT
# - BIN: Building Identification Number
# - BASE_BBL: Borough-Block-Lot
# - NAME: Building name (often null)
# - Height Roof: Roof height in feet
# - Ground Elevation: Ground level elevation
# - SHAPE_AREA: Footprint area in sq ft
# - Construction Year: Year built
```

---

## Scoring Algorithm

### Visibility Score Calculation

```python
def calculate_visibility_score(
    candidate: dict,
    user_lat: float,
    user_lng: float,
    user_bearing: float,
    cone_angle: float = 60,
    max_distance: float = 100
) -> float:
    """
    Calculate how likely this building is the user's target.

    Score components (all normalized 0-1):
    1. Distance score (closer = higher)
    2. Bearing alignment (center of cone = higher)
    3. Visible area score (more visible facade = higher)
    4. Height score (taller buildings more prominent)

    Returns: Combined score 0-100
    """

    # 1. DISTANCE SCORE (40% weight)
    # Exponential decay: buildings very close score much higher
    distance = candidate['distance_meters']
    distance_score = math.exp(-distance / 30)  # 30m decay constant

    # 2. BEARING ALIGNMENT (30% weight)
    # How close to center of view cone
    bearing_diff = candidate['bearing_difference']
    half_cone = cone_angle / 2
    bearing_score = 1 - (bearing_diff / half_cone)
    bearing_score = max(0, bearing_score)

    # 3. VISIBLE AREA (20% weight)
    # What fraction of footprint is visible in cone
    visible_area = candidate['visible_area']
    total_area = candidate['shape_area']
    area_score = min(1.0, visible_area / total_area) if total_area > 0 else 0.5

    # 4. HEIGHT SCORE (10% weight)
    # Taller buildings are more visually prominent
    height = candidate.get('height_roof', 30)  # default 30ft
    height_score = min(1.0, height / 200)  # normalize to 200ft

    # Combine with weights
    combined = (
        0.40 * distance_score +
        0.30 * bearing_score +
        0.20 * area_score +
        0.10 * height_score
    )

    return round(combined * 100, 2)
```

### Ambiguity Detection

```python
def is_ambiguous(candidates: List[dict]) -> bool:
    """
    Determine if the top candidates are too close in score to auto-select.

    Ambiguous if:
    - 2+ candidates AND
    - Score gap between #1 and #2 is < 15 points AND
    - Both are within 50m of user
    """
    if len(candidates) < 2:
        return False

    top_score = candidates[0]['score']
    second_score = candidates[1]['score']
    score_gap = top_score - second_score

    both_close = (
        candidates[0]['distance_meters'] < 50 and
        candidates[1]['distance_meters'] < 50
    )

    return score_gap < 15 and both_close
```

---

## API Changes

### Updated `/scan` Endpoint Response

```python
@router.post("/scan")
async def scan_building(...):
    """
    V2 scan endpoint with footprint-first approach.

    Returns:
        matches: Top 3 candidate buildings
        verification_method: How the result was determined
        show_picker: Whether UI should show building picker
        ambiguous_candidates: If ambiguous, the BINs that need CLIP
    """

    # Step 1: Footprint cone query
    candidates = await geospatial_v2.get_candidates_by_footprint(
        db, gps_lat, gps_lng, compass_bearing, phone_pitch
    )

    # Step 2: Classify result
    if len(candidates) == 0:
        return handle_no_candidates(...)

    if len(candidates) == 1:
        return {
            'matches': [candidates[0]],
            'verification_method': 'footprint_single',
            'confidence': 95,
            'show_picker': False
        }

    if not is_ambiguous(candidates):
        return {
            'matches': candidates[:3],
            'verification_method': 'footprint_winner',
            'confidence': candidates[0]['score'],
            'show_picker': candidates[0]['score'] < 85
        }

    # Step 3: CLIP disambiguation (ambiguous case)
    disambiguated = await clip_disambiguate(
        db, user_photo_url, candidates[:3]
    )

    return {
        'matches': disambiguated,
        'verification_method': 'clip_disambiguation',
        'confidence': disambiguated[0]['confidence'],
        'show_picker': disambiguated[0]['confidence'] < 80
    }
```

---

## Implementation Plan

### Phase 1: Data Foundation (Day 1)

1. **Create migration script** for `building_footprints` table
2. **Load 1.08M footprints** from CSV into PostGIS
3. **Verify spatial indexes** are working (test query performance)
4. **Join with buildings_full_merge_scanning** to link metadata

### Phase 2: Core Algorithm (Day 2)

1. **Create `geospatial_v2.py`** with footprint intersection logic
2. **Implement visibility scoring** algorithm
3. **Implement ambiguity detection**
4. **Write unit tests** for edge cases (corners, dense blocks, etc.)

### Phase 3: CLIP Integration (Day 3)

1. **Create `clip_disambiguation.py`** for on-demand CLIP
2. **Implement embedding cache** (check existing → fetch if missing)
3. **Add Street View fallback** with proper heading calculation
4. **Optimize for <500ms** response time

### Phase 4: Router Updates (Day 4)

1. **Update `scan.py`** to use new flow
2. **Update response schemas** for verification_method
3. **Add metrics/logging** for monitoring
4. **Handle edge cases** gracefully

### Phase 5: Testing & Deployment (Day 5)

1. **Integration tests** with real NYC locations
2. **Load testing** (simulate 100 concurrent scans)
3. **A/B testing** against V1 (if possible)
4. **Deploy to staging** → production

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| **P50 latency** | <200ms | Most scans are geometry-only |
| **P95 latency** | <800ms | Includes CLIP disambiguation |
| **P99 latency** | <2000ms | Complex edge cases |
| **Accuracy** | >95% | Correct building in top 3 |
| **Top-1 accuracy** | >85% | Correct building as #1 |
| **Cost per scan** | <$0.01 | Average across all cases |

---

## Monitoring & Analytics

### Key Metrics to Track

```python
# For each scan, log:
{
    "scan_id": "uuid",
    "verification_method": "footprint_single|footprint_winner|clip_disambiguation",
    "num_candidates": 5,
    "was_ambiguous": false,
    "clip_fetches": 0,  # Street View images fetched
    "clip_cache_hits": 0,  # Embeddings found in cache
    "processing_time_ms": 150,
    "user_confirmed": true,
    "was_correct": true,  # Did user confirm our top match?
}
```

### Dashboard Queries

```sql
-- Verification method distribution
SELECT verification_method, COUNT(*), AVG(processing_time_ms)
FROM scans WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY verification_method;

-- Accuracy by method
SELECT verification_method,
       COUNT(*) FILTER (WHERE was_correct) * 100.0 / COUNT(*) as accuracy
FROM scans WHERE confirmed_bin IS NOT NULL
GROUP BY verification_method;

-- CLIP usage (cost driver)
SELECT DATE(created_at), SUM(clip_fetches) * 0.007 as street_view_cost
FROM scans GROUP BY DATE(created_at) ORDER BY 1 DESC;
```

---

## Edge Cases & Handling

### 1. Corner Buildings

User at intersection pointing diagonally. Multiple buildings in cone.

**Solution:** Score by visible facade area. The building with more facade in view wins.

### 2. Building Behind Building

User points at tall building, short building is between them.

**Solution:** Consider obstruction. If building A is between user and building B, A gets priority unless B is significantly taller and visible above A.

### 3. GPS Drift

User's GPS is 20m off. Cone misses actual target.

**Solution:**
- Use wider cone (90° instead of 60°)
- If no results, expand radius in 25m increments
- Track GPS accuracy from device, adjust cone accordingly

### 4. User Looking Up at Skyscraper

User very close to base, pointing up at top.

**Solution:** Weight height more heavily when pitch > 30°. Tall buildings score higher.

### 5. Parks/Plazas

User in open space with no buildings in cone.

**Solution:** Expand search to 200m, suggest they move closer or contribute.

---

## Cost Projection

### Assumptions
- 10,000 scans/day
- 20% require CLIP disambiguation
- 50% of disambiguations have cached embeddings
- Average 2.5 Street View fetches per uncached disambiguation

### Monthly Cost

| Item | Calculation | Cost |
|------|-------------|------|
| **Footprint queries** | Free (PostGIS) | $0 |
| **CLIP disambiguation** | 10,000 × 20% × 50% × 2.5 × $0.007 × 30 | $525/mo |
| **Modal compute** | Reduced by 80% | ~$100/mo |
| **Total** | | ~$625/mo |

Compared to V1 estimate of ~$1,500/mo, this is a **58% cost reduction**.

As embeddings grow from user contributions, costs decrease further.

---

## Files to Create/Modify

### New Files
- `backend/migrations/20251213_building_footprints.sql`
- `backend/scripts/load_building_footprints.py`
- `backend/services/geospatial_v2.py`
- `backend/services/clip_disambiguation.py`

### Modified Files
- `backend/routers/scan.py` - Use new flow
- `backend/models/database.py` - Add BuildingFootprint model
- `backend/models/config.py` - Add V2 config options

---

## Rollback Plan

If V2 causes issues in production:

1. Keep V1 code intact (don't delete)
2. Add feature flag: `USE_SCAN_V2=true/false`
3. Route based on flag in scan.py
4. Monitor metrics, roll back if accuracy drops

```python
if settings.use_scan_v2:
    return await scan_v2(...)
else:
    return await scan_v1(...)  # Original flow
```

---

## Success Criteria

V2 is successful when:

1. **100% coverage**: Any NYC building can be scanned
2. **>95% accuracy**: Correct building in top 3 matches
3. **<500ms P50**: Fast response for most scans
4. **<$0.01/scan**: Average cost target met
5. **User satisfaction**: Fewer "wrong building" reports

---

## Next Steps

1. Review and approve this plan
2. Create building_footprints table and load data
3. Implement geospatial_v2.py
4. Implement clip_disambiguation.py
5. Update scan router
6. Test with real NYC coordinates
7. Deploy to staging
8. A/B test against V1
9. Full production rollout

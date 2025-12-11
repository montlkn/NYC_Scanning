# Cloudflare R2 Bucket Structure

## Overview

We use **two separate R2 buckets** to organize building images:

1. **`building-images`** - Reference images (ground truth)
2. **`user-images`** - User-submitted photos (community contributions)

This separation improves organization, security, and analytics.

---

## Bucket 1: `building-images` (Reference Images)

**Purpose**: Official reference images for CLIP embedding comparison

**Structure**:
```
building-images/
├── {BIN}/                          # Organized by Building Identification Number
│   ├── 0deg_0pitch.jpg            # Street View from north
│   ├── 0deg_0pitch_thumb.jpg
│   ├── 90deg_0pitch.jpg           # Street View from east
│   ├── 180deg_0pitch.jpg          # Street View from south
│   ├── 270deg_0pitch.jpg          # Street View from west
│   ├── 0deg_20pitch.jpg           # Looking up (20° pitch)
│   └── ... (multiple angles/pitches)
├── 1000000/
├── 1000005/
└── ... (thousands of building folders)

scans/                              # TEMPORARY: 24-hour retention
└── {uuid}.jpg                     # Initial scan upload before confirmation
└── {uuid}_thumb.jpg
```

**Content**:
- Street View images (Google Maps API)
- Pre-generated reference embeddings
- ~5,900 buildings currently

**Access**: Public read

**Lifecycle**:
- Reference images: Permanent
- `scans/` folder: Auto-delete after 24 hours (pending confirmation)

---

## Bucket 2: `user-images` (User Contributions)

**Purpose**: User-submitted building photos for model improvement

**Structure**:
```
user-images/
├── {user_id}/                      # Organized by user
│   ├── {BIN}/                     # Then by building
│   │   ├── {scan_id}_90deg_20251203_143022.jpg
│   │   ├── {scan_id}_90deg_20251203_143022_thumb.jpg
│   │   ├── {scan_id}_180deg_20251203_150133.jpg
│   │   └── ... (multiple photos from different angles/times)
│   └── {another_BIN}/
│       └── ...
└── anonymous/                      # For users without accounts
    └── {BIN}/
        └── ...
```

**Filename Format**: `{scan_id}_{bearing}deg_{timestamp}.jpg`

**Example**: `abc-123_90deg_20251203_143022.jpg`
- `abc-123` = Scan ID
- `90deg` = Camera was facing east (90° compass bearing)
- `20251203_143022` = December 3, 2025 at 14:30:22 UTC

**Content**:
- User-confirmed building photos
- Multi-angle contributions
- Metadata: GPS, compass bearing, timestamp

**Access**: Public read

**Lifecycle**: Permanent (unless user requests deletion)

---

## Upload Flow

### Normal Scan (Non-Walk)

```
1. User takes photo
   ↓
2. Upload to building-images/scans/{uuid}.jpg (temp storage)
   ↓
3. Backend runs CLIP matching against reference embeddings
   ↓
4. If match found (high confidence):
   → Show building info
   → Keep scan in scans/ for 24h, then auto-delete
   ↓
5. If low confidence or no match:
   → User confirms correct building
   → Move to user-images/{user_id}/{BIN}/{scan_id}_{bearing}deg_{timestamp}.jpg
   → Generate embedding, add to reference_embeddings table
```

### Walk Verification (New System)

```
1. User on walk, arrives at building
   ↓
2. Takes photo for verification
   ↓
3. Frontend tries:
   Tier 1: CLIP match (if reference embeddings exist)
   Tier 2: Cone of vision (GPS + compass + gyro)
   ↓
4. If Tier 2 verifies but Tier 1 fails:
   → Upload to building-images/scans/{uuid}.jpg
   → Call /api/confirm-with-photo with building BIN
   → Backend moves to user-images/{user_id}/{BIN}/...
   → Generate embedding, add to reference_embeddings
   ↓
5. User continues walk (instant verification via Tier 2)
6. Backend improves model in background (lazy embedding)
```

---

## Migration Notes

### Old Structure (Deprecated)
```
building-images/
├── scans/{uuid}.jpg               # ✅ KEEP (temp storage)
├── user-images/{user_id}/{BIN}/   # ❌ MOVE to user-images bucket
└── {BIN}/                         # ✅ KEEP (reference images)
```

### New Structure (Current)
```
building-images/
├── scans/{uuid}.jpg               # Temp storage (24h TTL)
└── {BIN}/                         # Reference images (permanent)

user-images/                        # NEW BUCKET
└── {user_id}/{BIN}/               # User contributions
```

**Migration Command**:
```bash
python backend/scripts/migrate_user_images_to_bucket.py
```

---

## Environment Variables

```bash
# Building Images Bucket (reference images)
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-234fc67c039149b2b46b864a1357763d.r2.dev

# User Images Bucket (user contributions)
R2_USER_IMAGES_BUCKET=user-images
R2_USER_IMAGES_PUBLIC_URL=https://pub-234fc67c039149b2b46b864a1357763d.r2.dev
```

---

## Analytics & Monitoring

### Building Images Bucket
- **Current Size**: 848.95 MB
- **Total Objects**: 5,898
- **Average per Building**: 12 images (various angles/pitches)

### User Images Bucket
- **Current Size**: TBD (newly created)
- **Growth Rate**: Estimated 50-100 images/day during beta
- **Unique Users**: Track via folder count
- **Buildings Covered**: Count unique BIN folders

### Daily Reembedding Job

Script: `backend/scripts/reembed_user_images.py`

**Schedule**: 2 AM daily (cron)

**Tasks**:
1. Find user images without embeddings
2. Generate CLIP embeddings
3. Insert into `reference_embeddings` table
4. Verify image quality
5. Clean up orphaned references

---

## Security & Privacy

### Building Images
- **Access**: Public (read-only)
- **No PII**: Only Street View images
- **Attribution**: Google Maps API

### User Images
- **Access**: Public (read-only)
- **User Control**: Organized by user_id
- **Privacy**: No EXIF data (stripped before upload)
- **Deletion**: User can request removal via API

### CORS Policy
```json
{
  "AllowedOrigins": ["https://jink.city", "https://*.jink.city"],
  "AllowedMethods": ["GET", "HEAD"],
  "AllowedHeaders": ["*"],
  "ExposeHeaders": ["ETag", "Content-Length"]
}
```

---

## Cost Estimation

### Storage Costs (Cloudflare R2)
- **Free tier**: 10 GB/month
- **Overage**: $0.015/GB/month

**Current Usage**:
- building-images: 848.95 MB
- user-images: ~0 MB (new)

**Projected 6 months**:
- building-images: 1-2 GB (slow growth, mostly Street View)
- user-images: 2-5 GB (50-100 photos/day × 6 months)
- **Total**: 3-7 GB (still within free tier!)

### Request Costs
- **Free tier**: 1M Class A (write), 10M Class B (read)
- **Overage**: $4.50/1M Class A, $0.36/1M Class B

**Monthly Estimate**:
- Writes: ~3,000 (well below free tier)
- Reads: ~50,000 (well below free tier)

---

## Backup Strategy

### Automated Backups
- R2 has built-in versioning (enable in Cloudflare dashboard)
- Supabase `reference_embeddings` table backs up daily

### Manual Backup
```bash
# Download entire bucket
aws s3 sync s3://building-images ./backups/building-images/ \
  --endpoint-url https://e6377cda4e44d1e548ce1c684965ee6a.r2.cloudflarestorage.com

aws s3 sync s3://user-images ./backups/user-images/ \
  --endpoint-url https://e6377cda4e44d1e548ce1c684965ee6a.r2.cloudflarestorage.com
```

---

## Future Enhancements

1. **Cloudflare Images**: Resize on-the-fly instead of storing thumbnails
2. **R2 Lifecycle Rules**: Auto-delete scans/ after 24h
3. **CDN Cache**: Cache popular building images at edge
4. **User Image Moderation**: Flag inappropriate content
5. **Image Deduplication**: Detect and merge duplicate uploads

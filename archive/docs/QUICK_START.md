# Quick Start: PostHog + Modal Deployment

All setup is complete! Here's what was done and the next steps.

## ✅ What Was Completed

### 1. **PostHog Analytics**
Analytics package installed and integrated:
- Tracks building scans with confidence, processing time, and results
- Tracks user confirmations to measure accuracy
- Events are sent to PostHog in real-time

**Code changes:**
- Added `posthog==3.5.0` to `backend/requirements.txt`
- Added PostHog initialization to `backend/main.py` (lines 47-51)
- Added tracking calls in `backend/routers/scan.py`:
  - `track_scan()` after scan completes (line 188)
  - `track_confirmation()` when user confirms building (line 248)

### 2. **Modal GPU Deployment**
Ready-to-deploy configuration with T4 GPU:
- File: `modal_app.py`
- Includes all backend dependencies
- Configured for CLIP inference on GPU
- Auto-scaling based on demand
- ~$0.30-0.50/day for 1000 scans

### 3. **Complete API Documentation**
Frontend integration guide created:
- File: `docs/API_INTEGRATION.md`
- Complete endpoint documentation
- TypeScript/React Native code examples
- Image compression helpers
- Error handling patterns
- Camera integration code

### 4. **Deployment Instructions**
Step-by-step guides created:
- `MODAL_SETUP.md` - How to deploy to Modal
- `DEPLOYMENT_CHECKLIST.md` - Complete setup checklist
- `QUICK_START.md` - This file

---

## 🚀 Next Steps (In Order)

### Step 1: Get PostHog API Key (5 minutes)
```
1. Go to https://posthog.com
2. Sign up (free tier)
3. Create project "NYC Scan"
4. Copy your API key (starts with "phc_")
```

### Step 2: Set Up Modal (5 minutes)
```bash
# Install Modal CLI
pip install modal

# Authenticate
modal setup
```

### Step 3: Create Modal Secret (2 minutes)
Copy your actual values from `backend/.env` and run:

```bash
modal secret create nyc-scan-secrets \
  DATABASE_URL="postgresql://..." \
  SCAN_DB_URL="postgresql://..." \
  SUPABASE_URL="https://..." \
  SUPABASE_KEY="eyJh..." \
  SUPABASE_SERVICE_KEY="eyJh..." \
  REDIS_URL="redis://..." \
  GOOGLE_MAPS_API_KEY="AIzaSy..." \
  R2_ACCOUNT_ID="..." \
  R2_ACCESS_KEY_ID="..." \
  R2_SECRET_ACCESS_KEY="..." \
  R2_BUCKET="building-images" \
  R2_PUBLIC_URL="https://..." \
  POSTHOG_API_KEY="phc_..." \
  SENTRY_DSN="https://..." \
  ENV="production" \
  DEBUG="false"
```

### Step 4: Deploy to Modal (2-5 minutes)
```bash
cd /Users/lucienmount/coding/nyc_scan
modal deploy modal_app.py
```

Expected output:
```
✓ Created image with ID img-123456
✓ Deployed nyc-scan-api
✓ Available at: https://YOUR-WORKSPACE--nyc-scan-api-fastapi-app.modal.run
```

### Step 5: Verify Deployment (1 minute)
```bash
# Health check
curl https://YOUR-WORKSPACE--nyc-scan-api-fastapi-app.modal.run/health

# Should return:
# {
#   "status": "healthy",
#   "checks": { "api": "ok", "clip_model": "ok", ... }
# }
```

### Step 6: Update Frontend (10 minutes)
Copy this code into your React Native app:

```typescript
// Update API base URL
const API_BASE_URL = 'https://YOUR-WORKSPACE--nyc-scan-api-fastapi-app.modal.run';

// Copy types and functions from docs/API_INTEGRATION.md
// Key functions needed:
// - scanBuilding()
// - confirmBuilding()
// - submitFeedback()
// - compressImage()
// - scanWithRetry()
// - captureAndScan()
```

See `docs/API_INTEGRATION.md` for complete code.

### Step 7: Test End-to-End (5 minutes)
```typescript
async function testEverything() {
  // 1. Capture photo and GPS
  const photo = await capturePhoto();
  const gps = await getGpsData();

  // 2. Scan building
  const result = await scanWithErrorHandling(photo, gps);
  console.log('✓ Scan successful:', result.matches[0]);

  // 3. Confirm building
  await confirmBuilding(result.scan_id, result.matches[0].bin);
  console.log('✓ Confirmation sent');

  // 4. Submit feedback
  await submitFeedback(result.scan_id, 5, 'correct');
  console.log('✓ Feedback recorded');

  // 5. Check PostHog
  console.log('✓ Check PostHog dashboard for events');
}
```

### Step 8: View Analytics (1 minute)
1. Go to PostHog dashboard at https://app.posthog.com
2. Select your "NYC Scan" project
3. Go to "Events"
4. You should see:
   - `building_scan` events (one per scan)
   - `scan_confirmed` events (one per confirmation)

---

## 📋 Files Reference

| File | Purpose | Status |
|------|---------|--------|
| `backend/requirements.txt` | Python dependencies | ✅ Updated with PostHog & Modal |
| `backend/services/analytics.py` | PostHog functions | ✅ Fixed and ready |
| `backend/main.py` | App initialization | ✅ PostHog integrated |
| `backend/routers/scan.py` | Scan endpoint | ✅ Analytics tracking added |
| `modal_app.py` | Modal deployment config | ✅ Created and ready |
| `docs/API_INTEGRATION.md` | Frontend guide | ✅ Complete with examples |
| `MODAL_SETUP.md` | Modal instructions | ✅ Complete guide |
| `DEPLOYMENT_CHECKLIST.md` | Full checklist | ✅ Complete guide |

---

## 🎯 What Happens When Users Scan

```
User opens app
    ↓
User takes photo + location is captured
    ↓
App calls POST /api/scan
    ↓
Backend:
  1. Resizes image
  2. Runs geospatial filtering
  3. Fetches reference images
  4. Runs CLIP comparison
  5. Calls track_scan() → PostHog ✨
  6. Returns matches
    ↓
App shows building matches
    ↓
User taps to confirm building
    ↓
App calls POST /api/scans/{scan_id}/confirm
    ↓
Backend calls track_confirmation() → PostHog ✨
    ↓
PostHog dashboard updated in real-time
```

---

## 💡 Key Metrics You'll See in PostHog

**Real-time on PostHog Dashboard:**
- Building scans per minute
- Average confidence score
- Top matched buildings
- Geographic distribution (from GPS)
- Confirmation rate
- Processing time distribution

**Example Charts:**
- "Scans by Confidence" - Histogram of confidence scores
- "Top Buildings" - Most frequently scanned
- "Geographic Map" - Where users are scanning
- "Confirmation Rate" - % of users confirming top match

---

## ⚡ Performance

**Expected latency:**
- Cold start: 2-5 seconds (first request)
- Warm start: 0.5-2 seconds (subsequent requests)
- Network upload: 100-300ms
- CLIP inference: 500-800ms
- Database queries: 100-200ms

**Total: 1-3 seconds per scan**

---

## 🔐 Security Notes

- All secrets stored in Modal only (not in code)
- POSTHOG_API_KEY is public key (safe to expose)
- R2 secret key never leaves Modal servers
- Database credentials use connection pooling
- Sentry DSN is safe to expose

---

## 📊 Next Analytics Steps

After deployment is live for a few days:

1. **Set up funnels** in PostHog to track:
   - Scan → Confirmation → Feedback conversion
   - Drop-off points where users abandon app

2. **Create dashboards** for:
   - Real-time scan volume
   - Confidence score trends
   - Building identification accuracy

3. **Set up alerts** for:
   - High error rates
   - Low confirmation rates
   - API performance degradation

4. **Track user cohorts:**
   - Users who scan frequently
   - Users with high/low accuracy
   - Geographic patterns

---

## 🆘 Troubleshooting

**PostHog not showing events?**
- Verify POSTHOG_API_KEY is set in Modal secret
- Check Modal logs: `modal tail nyc-scan-api`
- Events appear 30-60 seconds after scan

**Modal deployment failed?**
- Check error message: `modal deploy modal_app.py`
- Verify secret exists: `modal secret list`
- Try deploying again with `--force` flag

**High latency on first scan?**
- Cold start is normal (2-5 seconds)
- Subsequent scans are faster
- Can pre-warm with `modal run`

---

## 🎓 Example Complete Flow

See `docs/API_INTEGRATION.md` for these ready-to-use functions:

```typescript
// 1. Image compression
await compressImage(photoFile, 1024, 0.85)

// 2. Scan with retry logic
await scanWithRetry(photo, gpsData, maxRetries=3)

// 3. Complete camera integration
await captureAndScan(cameraRef)

// 4. Error handling
await scanWithErrorHandling(photo, gps)
```

---

## ✨ You're all set!

Everything is configured and ready to go. Just follow the 8 steps above and you'll have:

✅ Analytics tracking every scan
✅ GPU-powered inference on Modal
✅ Real-time metrics in PostHog
✅ Scalable infrastructure
✅ Complete frontend integration guide

**Estimated total setup time: 30 minutes**

---

**Questions?** Check:
- `MODAL_SETUP.md` for deployment help
- `docs/API_INTEGRATION.md` for frontend code
- `DEPLOYMENT_CHECKLIST.md` for complete reference

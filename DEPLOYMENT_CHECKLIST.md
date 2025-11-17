# PostHog + Modal Deployment Checklist

Complete setup for deploying NYC Scan API with analytics and GPU support.

## ✅ Completed Setup Steps

### 1. PostHog Analytics Integration
- [x] Added `posthog==3.5.0` to `backend/requirements.txt`
- [x] Added `modal==0.58.0` to `backend/requirements.txt`
- [x] Fixed `backend/services/analytics.py` with proper imports
- [x] Integrated PostHog initialization in `backend/main.py`
- [x] Added `track_scan()` calls in `/api/scan` endpoint
- [x] Added `track_confirmation()` calls in `/api/scans/{scan_id}/confirm` endpoint

**Analytics events tracked:**
- `building_scan` - User performs a building scan
  - Properties: confidence, num_candidates, processing_time_ms, has_match, bin
- `scan_confirmed` - User confirms building identification
  - Properties: confirmed_bin, was_top_match

### 2. Modal Deployment Configuration
- [x] Created `modal_app.py` with:
  - Debian slim Python 3.11 image
  - All backend dependencies pre-installed
  - T4 GPU allocation for CLIP inference
  - Backend code mounting
  - 60-second timeout for long-running scans
  - Secret integration

### 3. Documentation
- [x] Created `MODAL_SETUP.md` with:
  - Installation and authentication steps
  - Secret creation guide
  - Deployment instructions
  - Cost estimation ($0.30-0.50/day for 1000 scans)
  - Monitoring and troubleshooting
- [x] Created `docs/API_INTEGRATION.md` with:
  - Complete endpoint documentation
  - TypeScript/React Native examples
  - Image compression helpers
  - Retry logic with exponential backoff
  - Error handling patterns
  - Complete type definitions
  - Example React Native camera integration

---

## 🚀 Next Steps: Deployment

### Step 1: Install Modal CLI
```bash
pip install modal
modal setup  # Authenticate with Modal
```

### Step 2: Create Modal Secret
Use your actual secrets from `.env`:
```bash
modal secret create nyc-scan-secrets \
  DATABASE_URL="..." \
  SCAN_DB_URL="..." \
  SUPABASE_URL="..." \
  SUPABASE_KEY="..." \
  SUPABASE_SERVICE_KEY="..." \
  REDIS_URL="..." \
  GOOGLE_MAPS_API_KEY="..." \
  R2_ACCOUNT_ID="..." \
  R2_ACCESS_KEY_ID="..." \
  R2_SECRET_ACCESS_KEY="..." \
  R2_BUCKET="building-images" \
  R2_PUBLIC_URL="..." \
  POSTHOG_API_KEY="..." \
  SENTRY_DSN="..." \
  ENV="production" \
  DEBUG="false"
```

### Step 3: Deploy to Modal
```bash
modal deploy modal_app.py
```

Expected output:
```
✓ Created image with ID img-123456
✓ Mounted backend code
✓ Deployed nyc-scan-api on T4 GPU
✓ Available at: https://your-workspace--nyc-scan-api-fastapi-app.modal.run
```

### Step 4: Verify Deployment
```bash
curl https://your-workspace--nyc-scan-api-fastapi-app.modal.run/health
```

---

## 📊 PostHog Analytics Setup

### 1. Create PostHog Account
1. Go to https://posthog.com
2. Sign up (free tier available)
3. Create project "NYC Scan"
4. Copy API key

### 2. Set POSTHOG_API_KEY
Add to Modal secret (from Step 2 above) and local `.env`:
```
POSTHOG_API_KEY=phc_YOUR_API_KEY_HERE
```

### 3. View Analytics Dashboard
1. Go to PostHog dashboard
2. Events section shows:
   - `building_scan` events with confidence scores
   - `scan_confirmed` events with BIN values
   - Real-time user engagement metrics
   - Confidence score distribution
   - Geographic scan heatmap (from GPS coordinates)

### 4. Key Metrics to Track
- **Scan Success Rate**: `building_scan` events where `has_match == true`
- **Average Confidence**: Distribution of `confidence` property
- **Top Matched Buildings**: Most common `bin` values
- **Confirmation Rate**: Ratio of `scan_confirmed` to `building_scan`
- **Processing Performance**: Average `processing_time_ms`

---

## 🔧 Frontend Integration

### 1. Update API Base URL
In your React Native app:
```typescript
const API_BASE_URL = 'https://your-workspace--nyc-scan-api-fastapi-app.modal.run';
```

### 2. Copy TypeScript Types
From `docs/API_INTEGRATION.md`, copy interfaces into your app:
```typescript
interface ScanResponse { ... }
interface Match { ... }
interface GpsData { ... }
```

### 3. Implement API Functions
Copy these functions from `docs/API_INTEGRATION.md`:
- `scanBuilding()` - Main scan endpoint
- `confirmBuilding()` - User confirmation
- `submitFeedback()` - Feedback collection
- `compressImage()` - Image optimization
- `scanWithRetry()` - Retry logic
- `captureAndScan()` - React Native camera integration

### 4. Test Complete Flow
```typescript
async function testScan() {
  const photo = await capturePhoto();
  const gps = await getGpsData();
  const result = await scanWithErrorHandling(photo, gps);
  console.log('Scan result:', result);

  // Confirm building
  await confirmBuilding(result.scan_id, result.matches[0].bin);

  // Submit feedback
  await submitFeedback(result.scan_id, 5, 'correct');
}
```

---

## 🔐 Security Checklist

- [ ] POSTHOG_API_KEY stored in Modal secrets only (not in code)
- [ ] R2_SECRET_ACCESS_KEY stored in Modal secrets only
- [ ] SUPABASE_SERVICE_KEY stored in Modal secrets only
- [ ] SENTRY_DSN doesn't expose sensitive data
- [ ] Database credentials use connection pooling
- [ ] CORS restricted to your app domain (update `main.py` if needed)
- [ ] GPS coordinates logged for location services only

---

## 📈 Cost Monitoring

### Estimated Monthly Costs (1000 scans/day):

| Component | Cost/Month |
|-----------|------------|
| Modal T4 GPU | $9-15 |
| PostHog Analytics | Free tier |
| Sentry Error Tracking | Free tier (with paid options) |
| Supabase Database | Depends on usage |
| R2 Storage | ~$0.015/GB |
| **Total** | ~$10-20+ |

**To reduce costs:**
- Use Modal's free tier (~50,000 scans/month)
- Switch to smaller GPU if performance allows (L4 if heavier workloads)
- Optimize image processing to reduce inference time
- Use caching for reference images

---

## 🐛 Troubleshooting

### PostHog Events Not Appearing
- Verify `POSTHOG_API_KEY` is set in Modal secrets
- Check API logs: `modal tail nyc-scan-api`
- Ensure `posthog.capture()` is called (check line 188 in `scan.py`)
- Wait 30-60 seconds for events to appear in dashboard

### Modal Deployment Failed
```bash
# Check logs
modal tail nyc-scan-api

# Check if secret exists
modal secret list

# Redeploy
modal deploy modal_app.py --force
```

### High CLIP Inference Time
- Consider upgrading to L4 GPU: change `gpu="T4"` to `gpu="L4"` in `modal_app.py`
- Reduce image resolution if possible
- Cache reference embeddings

### Cold Starts Taking Too Long
- Cold starts (2-5 seconds) are normal for GPU inference
- Use Modal's scheduler to keep instance warm
- Recommend showing loading UI during first scan

---

## 📚 Documentation Files

1. **MODAL_SETUP.md** - Modal deployment instructions
2. **docs/API_INTEGRATION.md** - Complete API documentation with examples
3. **docs/INTEGRATION_AND_SCALING.md** - Original planning document
4. **DEPLOYMENT_CHECKLIST.md** - This file

---

## ✨ Summary

You now have:

✅ **Analytics**: PostHog integration tracks scans and confirmations
✅ **GPU Inference**: Modal deploys API with T4 GPU support
✅ **Documentation**: Complete API docs for frontend integration
✅ **Error Tracking**: Sentry integration for production monitoring
✅ **Auto-scaling**: Modal handles scaling based on demand
✅ **Cost Optimization**: Pay only for GPU time used

**Ready to deploy!** Follow the Next Steps section above to get live.

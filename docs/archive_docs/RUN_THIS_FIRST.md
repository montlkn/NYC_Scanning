# 🚀 NYC Scan - Complete Setup Guide

**Start here!** This guide will take you from CSVs to deployed API in ~45 minutes.

---

## ✅ Prerequisites Checklist

- [x] PLUTO CSV in `backend/data/`
- [x] Landmarks CSV in `backend/data/`
- [ ] Python 3.11+ with venv activated
- [ ] Supabase database accessible
- [ ] Google Maps API key (with restrictions set!)
- [ ] Cloudflare R2 bucket created
- [ ] Upstash Redis (optional)

---

## Step 1: Restrict Google Maps API (2 min)

🔒 **CRITICAL FOR SECURITY!**

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Click your API key
3. **Application restrictions:**
   - Select "HTTP referrers"
   - Add: `https://nyc-scan-api.onrender.com/*`
   - Add: `http://localhost:8000/*` (for dev)
4. **API restrictions:**
   - Select "Restrict key"
   - Choose **Street View Static API** ONLY
5. Click **Save**

---

## Step 2: Preprocess Landmarks CSV (2 min)

Your landmarks CSV has State Plane coordinates, need to convert to lat/lng:

```bash
cd /Users/lucienmount/coding/nyc_scan/backend
source venv/bin/activate

# Install pyproj if needed
pip install pyproj

# Extract lat/lng from geometry field
python scripts/preprocess_landmarks.py \
  --input data/walk_optimized_landmarks.csv \
  --output data/landmarks_processed.csv
```

Expected output:
```
✅ Processed 5000 landmarks
📄 Output: data/landmarks_processed.csv
```

---

## Step 3: Run Database Migration (1 min)

Creates NEW `buildings` table (won't touch your existing 37k landmarks):

```bash
psql $DATABASE_URL < migrations/002_create_unified_buildings_table.sql
```

Expected output:
```
CREATE TABLE
CREATE INDEX
...
✅ Migration 002 completed successfully!
```

---

## Step 4: Ingest PLUTO Data (10 min)

Load ~860k NYC buildings:

```bash
# Test first (optional)
python scripts/ingest_pluto.py \
  --csv data/Primary_Land_Use_Tax_Lot_Output__PLUTO__20251001.csv \
  --dry-run --limit 100

# Full ingest (~8-10 minutes)
python scripts/ingest_pluto.py \
  --csv data/Primary_Land_Use_Tax_Lot_Output__PLUTO__20251001.csv
```

Expected output:
```
✅ Parsed 860000 valid buildings
💾 Inserting into database...
  Processed 100000/860000 buildings...
  Processed 200000/860000 buildings...
  ...
✅ Database commit successful!

SUMMARY
Total CSV rows:     860000
Inserted:           860000
```

---

## Step 5: Enrich with Landmarks (2 min)

Add your landmark metadata:

```bash
python scripts/enrich_landmarks.py \
  --csv data/landmarks_processed.csv \
  --create-missing
```

Expected output:
```
✅ Parsed 5000 landmarks
💾 Updating database...
✅ Database commit successful!

SUMMARY
Updated buildings:  4800
Created buildings:  200  # Landmarks not in PLUTO
Not found:          0
```

---

## Step 6: Verify Data (1 min)

```bash
psql $DATABASE_URL -c "
SELECT
  COUNT(*) as total_buildings,
  COUNT(*) FILTER (WHERE is_landmark) as landmarks,
  COUNT(*) FILTER (WHERE num_floors IS NOT NULL) as with_floors,
  ROUND(100.0 * COUNT(*) FILTER (WHERE num_floors IS NOT NULL) / COUNT(*), 1) as floor_coverage_pct
FROM buildings;
"
```

Expected:
```
 total_buildings | landmarks | with_floors | floor_coverage_pct
-----------------+-----------+-------------+--------------------
          860200 |      5000 |      750000 |               87.2
```

⚠️ **If floor_coverage_pct < 80%**: Check PLUTO CSV has `numfloors` column!

---

## Step 7: Add Sentry (5 min)

Error tracking for production:

1. Go to [sentry.io](https://sentry.io) → Create account
2. Create new project: **Python** → **FastAPI**
3. Copy DSN (looks like: `https://xxx@xxx.ingest.sentry.io/xxx`)
4. Add to backend:

```bash
cd backend
pip install sentry-sdk[fastapi]
echo "sentry-sdk[fastapi]==1.40.0" >> requirements.txt
```

5. Update `main.py`:

```python
# Add at top of main.py
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    integrations=[FastApiIntegration()],
    traces_sample_rate=0.1,  # 10% of transactions
    environment="production" if os.getenv("RENDER") else "development"
)
```

6. Add to `.env`:
```bash
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx
```

---

## Step 8: Clean Up Directory (5 min)

Remove unnecessary files and organize:

```bash
cd /Users/lucienmount/coding/nyc_scan

# Already removed (you did this earlier):
# - docker-compose.yml
# - fly.toml
# - backend/Dockerfile

# Check what's left
ls -1
```

Should see:
```
.env
.gitignore
backend/
DATA_INGESTION_GUIDE.md
GETTING_STARTED.md
PLAN OF ACTION.txt
PROJECT_STATUS.md
README.md
RENDER_DEPLOY.md
RUN_THIS_FIRST.md  ← You are here!
SECURITY.md
render.yaml
```

Clean structure ✅

---

## Step 9: Push to GitHub (3 min)

```bash
cd /Users/lucienmount/coding/nyc_scan

# Check status
git status

# Add all changes
git add .

# Commit
git commit -m "Add unified buildings table, data ingestion, and Render config

- Created buildings table merging PLUTO + landmarks
- Added ingest_pluto.py and enrich_landmarks.py scripts
- num_floors field for barometer floor validation
- Configured for Render deployment
- Added Sentry error tracking
- Removed Docker files (using Render native Python)
"

# Push
git push origin main
```

---

## Step 10: Deploy to Render (15 min)

1. Go to [render.com](https://render.com) → Sign in with GitHub
2. Click **New +** → **Web Service**
3. Select `nyc_scan` repository
4. Settings:
   - **Name:** `nyc-scan-api`
   - **Region:** `Oregon` (or `Virginia` for east coast)
   - **Branch:** `main`
   - **Root Directory:** `backend`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Plan:** `Free`

5. **Environment Variables** (click "Advanced"):
   ```
   DATABASE_URL=postgresql://...
   SUPABASE_URL=https://...
   SUPABASE_KEY=eyJ...
   SUPABASE_SERVICE_KEY=eyJ...
   GOOGLE_MAPS_API_KEY=AIzaSy...
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=building-images
   R2_PUBLIC_URL=https://pub-xxxxx.r2.dev
   REDIS_URL=redis://...  (optional)
   SENTRY_DSN=https://...
   PYTHON_VERSION=3.11.0
   CLIP_DEVICE=cpu
   PORT=8000
   ```

6. Click **Create Web Service**
7. Wait ~10 minutes for build (installing deps + downloading CLIP model)

---

## Step 11: Verify Deployment (2 min)

Once deployed, Render gives you URL: `https://nyc-scan-api.onrender.com`

Test:
```bash
# Health check
curl https://nyc-scan-api.onrender.com/health

# Should return:
{
  "status": "healthy",
  "checks": {
    "api": "ok",
    "clip_model": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

Visit docs: `https://nyc-scan-api.onrender.com/docs`

---

## Step 12: Update Mobile App (2 min)

```javascript
// architecture-app/src/config/api.js
const API_BASE_URL = __DEV__
  ? 'http://localhost:8000/api'
  : 'https://nyc-scan-api.onrender.com/api';
```

---

## 🎉 You're Done!

### What You Built:
- ✅ Unified buildings database (860k+ buildings)
- ✅ Floor data for barometer validation
- ✅ Landmark scoring and metadata
- ✅ Production API on Render (free!)
- ✅ Error tracking with Sentry
- ✅ Secure Google Maps API
- ✅ Clean, organized codebase

### Next Steps:
1. Test scan from mobile app
2. Pre-cache top landmarks:
   ```bash
   # In Render Shell (Dashboard → Shell tab)
   python scripts/precache_landmarks.py --top-n 100
   ```
3. Monitor errors in Sentry
4. Upgrade to Render Starter ($7/mo) if spin-down is annoying

---

## 📊 Cost Breakdown

| Service | Cost | Purpose |
|---------|------|---------|
| Render Free Tier | $0 | API hosting |
| Supabase Free Tier | $0 | Database (500MB) |
| Cloudflare R2 | ~$1/mo | Image storage |
| Upstash Redis | $0 | Caching (free tier) |
| Google Maps API | ~$5-10/mo | Street View images |
| Sentry | $0 | Error tracking (free tier) |
| **Total** | **~$6-11/mo** | |

---

## Troubleshooting

### "pyproj not found" during preprocessing
```bash
pip install pyproj
```

### Database connection refused
```bash
# Test connection
psql $DATABASE_URL -c "SELECT 1"

# Check Supabase not paused
```

### PLUTO ingestion very slow
This is normal - 860k rows takes ~8-10 minutes. Get coffee ☕

### Render build fails
Check logs for specific error. Common: missing `requirements.txt` dependencies.

### First API request times out
Normal! Free tier spins down after 15min idle. First request wakes it up (~60s).

---

**Questions?** Check [RENDER_DEPLOY.md](RENDER_DEPLOY.md) for detailed troubleshooting.

**Ready?** Start with Step 1! 🚀

# Deploy NYC Scan to Render

Complete guide for deploying the NYC Scan backend to Render's free tier.

---

## Prerequisites

✅ GitHub repository with latest code pushed
✅ Supabase database with migrations run
✅ PLUTO + Landmarks data ingested
✅ Google Maps API key
✅ Cloudflare R2 bucket
✅ Upstash Redis (optional but recommended)

---

## Step 1: Push to GitHub

Make sure all changes are committed and pushed:

```bash
cd /Users/lucienmount/coding/nyc_scan

git add .
git commit -m "Add data ingestion scripts and Render config"
git push origin main
```

---

## Step 2: Create Render Account

1. Go to [render.com](https://render.com)
2. Sign up with GitHub
3. Authorize Render to access your repositories

---

## Step 3: Create New Web Service

1. Click **"New +"** → **"Web Service"**
2. Connect your `nyc_scan` repository
3. Configure service:

### Basic Settings
- **Name:** `nyc-scan-api`
- **Region:** `Oregon` (or `Virginia` for east coast)
- **Branch:** `main`
- **Root Directory:** `backend`
- **Runtime:** `Python 3`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python main.py`

### Instance Type
- **Plan:** `Free` (750 hours/month, spins down after 15min idle)

---

## Step 4: Set Environment Variables

In Render dashboard, go to **Environment** tab and add:

### Supabase
```
DATABASE_URL=postgresql://postgres.XXX:[password]@aws-0-us-east-1.pooler.supabase.co:5432/postgres
SUPABASE_URL=https://XXX.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Google Maps
```
GOOGLE_MAPS_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

### Cloudflare R2
```
R2_ACCOUNT_ID=your_account_id
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-xxxxx.r2.dev
```

### Redis (Optional)
```
REDIS_URL=redis://default:[password]@[host]:6379
```

### System
```
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
```

---

## Step 5: Deploy

1. Click **"Create Web Service"**
2. Render will:
   - Clone your repository
   - Install Python dependencies (~5-7 minutes)
   - Download CLIP model (~350MB, ~3-5 minutes)
   - Start the server

3. Monitor logs in real-time

Expected output:
```
==> Installing dependencies...
==> Collecting fastapi...
==> Installing open-clip-torch...
==> Downloading CLIP model...
==> Starting server...
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## Step 6: Verify Deployment

### Check Health Endpoint

Once deployed, Render will give you a URL like:
`https://nyc-scan-api.onrender.com`

Test it:
```bash
curl https://nyc-scan-api.onrender.com/health
```

Expected response:
```json
{
  "status": "healthy",
  "timestamp": 1759344840.132164,
  "checks": {
    "api": "ok",
    "clip_model": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

### Test API Documentation

Visit: `https://nyc-scan-api.onrender.com/docs`

You should see the interactive Swagger UI.

---

## Step 7: Update Mobile App

In your React Native app, update the API URL:

```javascript
// architecture-app/src/config/api.js
const API_BASE_URL = __DEV__
  ? 'http://localhost:8000/api'
  : 'https://nyc-scan-api.onrender.com/api';

export default API_BASE_URL;
```

---

## Free Tier Limitations

### Spin-Down Behavior
- ⚠️ **After 15 minutes of inactivity**, service spins down
- **First request** after spin-down takes ~1 minute to wake up
- Subsequent requests are instant

### Workarounds
1. **Accept it:** Fine for MVP/testing
2. **Keep-alive service:** Use [UptimeRobot](https://uptimerobot.com/) to ping every 14 minutes (free)
3. **Upgrade to Starter ($7/mo):** No spin-down, always on

### Resource Limits
- **Memory:** 512MB (sufficient for CLIP model)
- **CPU:** Shared
- **Build minutes:** Unlimited
- **Bandwidth:** 100GB/month (plenty)

---

## Troubleshooting

### Issue: Build fails on `open-clip-torch`

**Error:** `Could not find a version that satisfies open-clip-torch`

**Solution:** Check `requirements.txt` has correct version:
```
open-clip-torch==2.24.0
```

### Issue: "Out of memory" error

**Error:** `MemoryError` or `Killed`

**Solution:**
1. Reduce CLIP model batch size in code
2. Or upgrade to Starter plan (1GB RAM)

### Issue: Database connection refused

**Error:** `could not connect to server`

**Solution:**
- Verify `DATABASE_URL` is correct (use pooler URL, not direct)
- Check Supabase is not paused
- Test connection: `psql $DATABASE_URL -c "SELECT 1"`

### Issue: First request times out

**Cause:** Service spinning up from sleep

**Solution:** Wait 60-90 seconds, then retry. This is normal for free tier.

### Issue: CLIP model download fails

**Error:** `Failed to download model`

**Solution:**
- Check Render has internet access (should by default)
- Model downloads from HuggingFace, ensure not blocked
- Check build logs for specific error

---

## Monitoring & Logs

### View Logs
- Render Dashboard → Your Service → **Logs** tab
- Real-time streaming logs
- Filter by date/time

### Metrics
- Render Dashboard → Your Service → **Metrics** tab
- CPU usage
- Memory usage
- Response times
- HTTP requests/sec

### Alerts
- Set up email/Slack notifications
- **Settings** → **Notifications**
- Alert on:
  - Deploy failures
  - Service crashes
  - High error rates

---

## Upgrading to Paid Plan

If you need always-on service or more resources:

### Starter Plan ($7/mo)
- **No spin-down** (always running)
- **1GB RAM** (better for CLIP)
- **1 CPU**
- 400 build hours/month

### Standard Plan ($25/mo)
- **2GB RAM**
- **2 CPUs**
- Faster response times

---

## Switching to Fly.io Later

If you need global deployment:

1. Create `backend/Dockerfile`:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download CLIP model
RUN python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')"

# Copy code
COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
```

2. Deploy to Fly.io:
```bash
flyctl launch
flyctl secrets import < .env
flyctl deploy
```

3. Update mobile app URL to Fly.io URL

---

## CI/CD: Auto-Deploy on Push

Render automatically deploys on push to `main` branch.

To disable:
1. Service Settings → **Auto-Deploy**
2. Toggle off

To deploy manually:
1. Dashboard → **Manual Deploy**
2. Select branch
3. Click **Deploy**

---

## Cost Comparison

| Platform | Free Tier | Paid Tier | Global | Ease |
|----------|-----------|-----------|--------|------|
| **Render** | ✅ 750hrs | $7/mo | ❌ | ⭐⭐⭐⭐⭐ |
| **Railway** | ❌ | $5-10/mo | ❌ | ⭐⭐⭐⭐ |
| **Fly.io** | ❌ | $2-5/mo | ✅ | ⭐⭐⭐ |
| **Vercel** | ❌ Bad fit | $20/mo | ✅ | ⭐⭐ |

**Recommendation:** Start with Render free tier, upgrade to Fly.io if you go global.

---

## Next Steps

After successful deployment:

1. ✅ Test `/health` endpoint
2. ✅ Test `/api/scan` with Postman/curl
3. ✅ Update mobile app with production URL
4. ✅ Test full scan flow from mobile app
5. ✅ Set up monitoring/alerts
6. ✅ Pre-cache top 100 landmarks:
   ```bash
   # SSH into Render shell (Dashboard → Shell tab)
   python scripts/precache_landmarks.py --top-n 100
   ```

---

## Support

- **Render Docs:** https://render.com/docs
- **Render Community:** https://community.render.com/
- **Status Page:** https://status.render.com/

---

**You're deployed! 🚀**

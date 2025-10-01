# Getting Started with NYC Scan Backend

Welcome! This guide will help you get the NYC Scan backend up and running.

## 🎯 Quick Start (5 minutes)

### Prerequisites
- Python 3.11+
- PostgreSQL + PostGIS (via Supabase)
- Google Maps API key
- Cloudflare R2 account
- Redis instance (Upstash recommended)

### Installation

```bash
# 1. Navigate to backend directory
cd backend

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env with your credentials (see below)

# 5. Run database migrations
psql $DATABASE_URL < migrations/001_add_scan_tables.sql

# 6. Start the server
python main.py
```

The API will be available at `http://localhost:8000`

Visit `http://localhost:8000/docs` for interactive API documentation.

## 🔑 Required Credentials

### 1. Supabase
- **URL**: Your Supabase project URL
- **Keys**: Anon key and service key
- **Database URL**: Connection string from Supabase settings

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key
SUPABASE_SERVICE_KEY=your-service-key
DATABASE_URL=postgresql://postgres:password@db.your-project.supabase.co:5432/postgres
```

### 2. Google Maps API
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "Street View Static API"
4. Create API key
5. **Important**: Restrict key to Street View Static API only

```bash
GOOGLE_MAPS_API_KEY=your-api-key
```

### 3. Cloudflare R2
1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. Navigate to R2 Object Storage
3. Create a bucket named "building-images"
4. Create API token with read/write permissions
5. Note your Account ID

```bash
R2_ACCOUNT_ID=your-account-id
R2_ACCESS_KEY_ID=your-access-key
R2_SECRET_ACCESS_KEY=your-secret-key
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-xxxxx.r2.dev
```

### 4. Redis (Upstash)
1. Go to [Upstash](https://upstash.com/)
2. Create a Redis database
3. Copy the connection URL

```bash
REDIS_URL=redis://default:password@host:6379
```

## 🧪 Testing the Setup

### 1. Check Health
```bash
curl http://localhost:8000/health
```

Should return:
```json
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

### 2. Test CLIP Model
```bash
curl http://localhost:8000/api/debug/test-clip
```

Should return:
```json
{
  "status": "ok",
  "model": "ViT-B-32",
  "device": "cuda",
  "model_loaded": true
}
```

### 3. Test Geospatial
```bash
curl "http://localhost:8000/api/debug/test-geospatial?lat=40.7074&lng=-74.0113&bearing=180&distance=100"
```

Should return candidate buildings and cone WKT.

### 4. Test Street View
```bash
curl "http://localhost:8000/api/debug/test-street-view?lat=40.7074&lng=-74.0113&bearing=180"
```

Should confirm Street View availability.

## 📊 Next Steps

### 1. Load Building Data
You need to populate your Supabase `buildings` table with NYC building data.

**Option A: Use existing data**
If you already have buildings data in Supabase, you're good to go!

**Option B: Load PLUTO data**
```bash
python scripts/ingest_pluto.py
```

### 2. Enrich Landmark Data
Add scores and metadata to landmark buildings:
```bash
python scripts/enrich_landmarks.py --csv path/to/landmarks.csv
```

### 3. Pre-cache Reference Images
Pre-cache Street View images for top landmarks:
```bash
# Start small for testing
python scripts/precache_landmarks.py --top-n 100

# Full pre-cache (costs ~$140)
python scripts/precache_landmarks.py --top-n 5000
```

### 4. Test Full Scan Flow
Use the interactive API docs at `http://localhost:8000/docs` to:
1. Upload a test building photo
2. Provide GPS coordinates
3. Provide compass bearing
4. See matched results

## 🔍 Common Issues

### Issue: CLIP Model Not Loading
**Error**: `RuntimeError: CLIP model not initialized`

**Solution**:
```bash
# Pre-download the model
python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')"
```

### Issue: Database Connection Failed
**Error**: `could not connect to server`

**Solution**:
- Verify `DATABASE_URL` in `.env`
- Check Supabase is not paused
- Test connection: `psql $DATABASE_URL -c "SELECT 1"`

### Issue: Street View Images Not Fetching
**Error**: `Failed to fetch Street View image`

**Solution**:
- Verify API key is correct
- Check API is enabled in Google Cloud Console
- Check billing is enabled
- Test: `curl "https://maps.googleapis.com/maps/api/streetview?size=600x600&location=40.7074,-74.0113&key=YOUR_KEY"`

### Issue: R2 Upload Failed
**Error**: `Failed to upload image to R2`

**Solution**:
- Verify R2 credentials
- Check bucket exists and is named correctly
- Test with AWS CLI: `aws s3 ls --endpoint-url https://ACCOUNT_ID.r2.cloudflarestorage.com`

## 📱 Connecting Mobile App

Once the backend is running, you can connect the mobile app:

### 1. Update API URL
In your mobile app, update the API base URL:

```javascript
// src/config/api.js
const API_BASE_URL =
  __DEV__
    ? 'http://localhost:8000/api'  // Local development
    : 'https://your-backend.railway.app/api';  // Production
```

### 2. Test Connection
```javascript
// Test health endpoint
fetch(`${API_BASE_URL}/../health`)
  .then(res => res.json())
  .then(data => console.log('Backend health:', data));
```

### 3. Implement Scan Flow
See `mobile_integration_example.js` for reference implementation.

## 🚀 Deployment

### Deploy to Railway
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Initialize project
railway init

# Add environment variables
railway variables set SUPABASE_URL=...
railway variables set GOOGLE_MAPS_API_KEY=...
# ... (set all variables)

# Deploy
railway up
```

### Deploy to Fly.io
```bash
# Install Fly CLI
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Launch app
fly launch

# Set secrets
fly secrets set SUPABASE_URL=...
fly secrets set GOOGLE_MAPS_API_KEY=...
# ... (set all secrets)

# Deploy
fly deploy
```

## 📚 Additional Resources

- **Backend README**: [backend/README.md](backend/README.md) - Comprehensive backend documentation
- **Project Status**: [PROJECT_STATUS.md](PROJECT_STATUS.md) - Current progress and roadmap
- **Plan of Action**: [PLAN OF ACTION.txt](PLAN OF ACTION.txt) - Original 3-week plan
- **API Docs**: http://localhost:8000/docs - Interactive API documentation

## 💡 Tips

1. **Development Mode**: Use `--reload` flag for auto-restart on code changes
2. **GPU Acceleration**: Set `CLIP_DEVICE=cuda` if you have NVIDIA GPU
3. **Cost Control**: Start with small pre-cache (100 buildings) before full run
4. **Testing**: Use debug endpoints extensively before production
5. **Monitoring**: Check logs regularly for errors and performance

## 🐛 Troubleshooting

If you encounter issues:

1. Check logs: `tail -f backend.log`
2. Verify environment variables: `curl http://localhost:8000/api/debug/config`
3. Test individual components with debug endpoints
4. Check database connection: `psql $DATABASE_URL`
5. Verify API keys are correct and have proper permissions

## 📞 Support

For questions or issues:
- Check [PROJECT_STATUS.md](PROJECT_STATUS.md) for known issues
- Review API documentation at `/docs`
- Check backend logs for error details

---

**Happy Building! 🏗️**
# Deployment Guide

Complete guide for deploying NYC Scan backend to production.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Database Setup](#database-setup)
- [Environment Configuration](#environment-configuration)
- [Render Deployment](#render-deployment)
- [Alternative Platforms](#alternative-platforms)
- [Post-Deployment](#post-deployment)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before deploying, ensure you have:

1. **GitHub repository** with latest code pushed
2. **Supabase account** (for Main DB)
3. **Postgres database** with pgvector (for Phase 1 DB)
4. **Cloudflare R2 account** (for image storage)
5. **Google Maps API key** (with Street View Static API enabled)
6. **Upstash Redis** account (for caching)
7. **Sentry account** (for error tracking, optional)

---

## Database Setup

### Main Supabase Database

**1. Create Supabase Project:**

```bash
# Visit: https://supabase.com/dashboard
# Create new project: "nyc-scan-main"
# Region: US East (closest to users)
# Plan: Free tier (upgradeable)
```

**2. Enable PostGIS Extension:**

```sql
-- In Supabase SQL Editor
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
```

**3. Run Migrations:**

```bash
# Get connection string from Supabase dashboard
# Settings → Database → Connection String (Session mode)

export DATABASE_URL="postgresql://postgres.xxx@aws-xxx.pooler.supabase.com:5432/postgres"

# Run migrations in order
psql $DATABASE_URL < backend/migrations/001_add_scan_tables.sql
psql $DATABASE_URL < backend/migrations/002_create_unified_buildings_table.sql
```

**4. Verify Tables:**

```sql
-- Check tables exist
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;

-- Should see:
-- buildings_full_merge_scanning
-- reference_images
-- scans
-- scan_feedback
-- cache_stats
```

---

### Phase 1 Postgres Database (pgvector)

**Option 1: Supabase (Recommended)**

```bash
# Create second Supabase project
# Project name: "nyc-scan-phase1"
# Region: Same as main DB

# Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;

# Run Phase 1 migration
psql $SCAN_DB_URL < backend/migrations/003_scan_tables.sql

# Verify pgvector
SELECT * FROM pg_extension WHERE extname = 'vector';
```

**Option 2: Render Managed PostgreSQL**

```bash
# Visit: https://dashboard.render.com
# New → PostgreSQL
# Name: nyc-scan-phase1
# Plan: Free (25MB storage)

# Wait for provisioning...
# Copy "Internal Database URL"

# Install pgvector extension
psql $SCAN_DB_URL -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql $SCAN_DB_URL -c "CREATE EXTENSION IF NOT EXISTS postgis;"

# Run migration
psql $SCAN_DB_URL < backend/migrations/003_scan_tables.sql
```

**Option 3: Self-Hosted (Docker)**

```yaml
# docker-compose.yml
version: '3.8'
services:
  postgres:
    image: ankane/pgvector:latest
    environment:
      POSTGRES_DB: nyc_scan
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: your_password
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```

```bash
docker-compose up -d
export SCAN_DB_URL="postgresql://postgres:your_password@localhost:5432/nyc_scan"
psql $SCAN_DB_URL -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql $SCAN_DB_URL -c "CREATE EXTENSION IF NOT EXISTS postgis;"
psql $SCAN_DB_URL < backend/migrations/003_scan_tables.sql
```

---

## Environment Configuration

### Required Environment Variables

Create `.env` file (for local testing):

```bash
# ===== Main Database =====
DATABASE_URL=postgresql://postgres.xxx@aws-xxx.pooler.supabase.com:5432/postgres
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# ===== Phase 1 Database (CRITICAL!) =====
SCAN_DB_URL=postgresql://postgres.yyy@aws-yyy.pooler.supabase.com:5432/postgres

# ===== Google Maps API =====
GOOGLE_MAPS_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXX

# ===== Cloudflare R2 Storage =====
R2_ACCOUNT_ID=your_account_id
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key
R2_BUCKET=building-images
R2_PUBLIC_URL=https://pub-234fc67c039149b2b46b864a1357763d.r2.dev

# ===== Upstash Redis =====
REDIS_URL=redis://default:password@selected-javelin-15870.upstash.io:6379

# ===== Sentry (Optional) =====
SENTRY_DSN=https://xxx@o123456.ingest.sentry.io/789012

# ===== System =====
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
```

### Getting API Keys

**1. Google Maps API:**

```bash
# Visit: https://console.cloud.google.com
# Enable "Street View Static API"
# Create API Key
# Add restrictions:
#   - API restrictions → Street View Static API only
#   - Application restrictions → HTTP referrers (production domain)
```

**2. Cloudflare R2:**

```bash
# Visit: https://dash.cloudflare.com
# R2 → Create bucket → "building-images"
# Settings → Public access → Allow
# R2 API Tokens → Create API token
# Copy: Account ID, Access Key ID, Secret Access Key
```

**3. Upstash Redis:**

```bash
# Visit: https://console.upstash.com
# Create Database → "nyc-scan-cache"
# Region: US East
# Copy Redis URL
```

**4. Sentry:**

```bash
# Visit: https://sentry.io
# Create Project → Python → FastAPI
# Copy DSN
```

---

## Render Deployment

### Step 1: Create Web Service

```bash
# Visit: https://dashboard.render.com
# New → Web Service
# Connect GitHub repository
```

### Step 2: Configure Service

```yaml
Name: nyc-scan-api
Region: Oregon (or closest to users)
Branch: main
Root Directory: backend/
Runtime: Python 3
Build Command: pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
Plan: Free (or Starter $7/month for always-on)
```

### Step 3: Add Environment Variables

In Render dashboard → Environment:

```
DATABASE_URL = postgresql://...
SCAN_DB_URL = postgresql://...
GOOGLE_MAPS_API_KEY = AIza...
R2_ACCOUNT_ID = ...
R2_ACCESS_KEY_ID = ...
R2_SECRET_ACCESS_KEY = ...
R2_BUCKET = building-images
R2_PUBLIC_URL = https://pub-...r2.dev
REDIS_URL = redis://...
SENTRY_DSN = https://...
PYTHON_VERSION = 3.11.0
CLIP_DEVICE = cpu
RENDER = true
```

### Step 4: Deploy

```bash
# Render auto-deploys on git push
git add .
git commit -m "Deploy to Render"
git push origin main

# Monitor deployment:
# https://dashboard.render.com → nyc-scan-api → Events
```

### Step 5: Verify Deployment

```bash
# Health check
curl https://nyc-scan-api.onrender.com/api/debug/health

# Stats
curl https://nyc-scan-api.onrender.com/api/stats

# Test scan (with real photo)
curl -X POST https://nyc-scan-api.onrender.com/api/phase1/scan \
  -F "photo=@test.jpg" \
  -F "lat=40.7484" \
  -F "lng=-73.9857" \
  -F "bearing=45" \
  -F "pitch=10" \
  -F "gps_accuracy=5"
```

---

## Alternative Platforms

### Fly.io

**Pros:** Global edge deployment, faster cold starts
**Cons:** More complex setup

```bash
# Install Fly CLI
brew install flyctl

# Login
flyctl auth login

# Create app
flyctl launch --name nyc-scan-api --region ewr

# Configure
cat > fly.toml <<EOF
app = "nyc-scan-api"
primary_region = "ewr"

[build]
  builder = "paketobuildpacks/builder:base"

[env]
  PORT = "8000"
  PYTHON_VERSION = "3.11"

[[services]]
  http_checks = []
  internal_port = 8000
  protocol = "tcp"

  [[services.ports]]
    port = 80
    handlers = ["http"]

  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]
EOF

# Set secrets
flyctl secrets set DATABASE_URL="postgresql://..."
flyctl secrets set SCAN_DB_URL="postgresql://..."
# ... (all other env vars)

# Deploy
flyctl deploy
```

### Railway

**Pros:** Simple setup, integrated databases
**Cons:** Higher cost

```bash
# Install Railway CLI
npm install -g railway

# Login
railway login

# Create project
railway init

# Add service
railway up

# Set environment variables in dashboard
# https://railway.app/dashboard
```

### Docker + VPS (DigitalOcean, AWS EC2, etc.)

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# Build
docker build -t nyc-scan-api .

# Run locally
docker run -p 8000:8000 --env-file .env nyc-scan-api

# Deploy to registry
docker tag nyc-scan-api:latest your-registry/nyc-scan-api:latest
docker push your-registry/nyc-scan-api:latest

# On VPS
docker pull your-registry/nyc-scan-api:latest
docker run -d -p 80:8000 --env-file .env --name nyc-scan nyc-scan-api
```

---

## Post-Deployment

### 1. Import Data

```bash
# SSH into Render shell (or run locally with production env)
# https://dashboard.render.com → nyc-scan-api → Shell

# Import top 100 buildings
python scripts/import_top100_from_csv.py

# Cache Street View images (8 angles × 100 = 800 images, ~$5.60)
python scripts/cache_panoramas_v2.py

# Generate CLIP embeddings (CPU: ~30 min)
python scripts/generate_embeddings_local.py
```

### 2. Verify Data

```bash
# Check Phase 1 DB
psql $SCAN_DB_URL
SELECT COUNT(*) FROM buildings WHERE tier = 1;
SELECT COUNT(*) FROM reference_embeddings;

# Should see:
# buildings: 100
# reference_embeddings: 800 (100 × 8 angles)
```

### 3. Test Scanning

```bash
# Test with known building
curl -X POST https://nyc-scan-api.onrender.com/api/phase1/scan \
  -F "photo=@empire_state.jpg" \
  -F "lat=40.7484" \
  -F "lng=-73.9857" \
  -F "bearing=45" \
  -F "pitch=10" \
  -F "gps_accuracy=5"

# Should return Empire State Building match with high confidence
```

### 4. Monitor

**Sentry Dashboard:**
- Visit: https://sentry.io/organizations/your-org/projects/nyc-scan/
- Monitor error rates, performance

**Render Metrics:**
- Dashboard → nyc-scan-api → Metrics
- CPU usage, memory, response times

**Database Health:**
```sql
-- Main DB query performance
SELECT * FROM pg_stat_statements ORDER BY total_time DESC LIMIT 10;

-- Phase 1 DB pgvector index usage
SELECT * FROM pg_indexes WHERE tablename = 'reference_embeddings';
```

---

## Troubleshooting

### Issue: "pg_config executable not found"

```bash
# On Render, add buildpack:
# Settings → Build & Deploy → Add Buildpack
# https://github.com/heroku/heroku-buildpack-apt

# Create backend/Aptfile:
postgresql-server-dev-all
```

### Issue: "Module 'torch' has no attribute 'cuda'"

```bash
# In requirements.txt, ensure CPU-only PyTorch:
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.2.0+cpu
torchvision==0.17.0+cpu
```

### Issue: "SCAN_DB_URL not set"

```bash
# Verify environment variable in Render:
# Dashboard → Environment → SCAN_DB_URL

# Or check in shell:
echo $SCAN_DB_URL
```

### Issue: "Rate limit exceeded on Street View API"

```bash
# Check Google Cloud Console:
# APIs & Services → Quotas
# Street View Static API: 25,000/day limit

# For large imports, request quota increase:
# https://console.cloud.google.com/quotas
```

### Issue: "HNSW index not used (slow queries)"

```sql
-- Check index exists:
SELECT * FROM pg_indexes WHERE tablename = 'reference_embeddings';

-- If missing, create manually:
CREATE INDEX idx_embeddings_vector ON reference_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Verify usage:
EXPLAIN ANALYZE
SELECT * FROM reference_embeddings
ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 10;
-- Should see "Index Scan using idx_embeddings_vector"
```

### Issue: "Cold starts taking 60+ seconds"

```bash
# Free tier spins down after 15min idle
# Solutions:
#   1. Upgrade to Starter plan ($7/mo) for always-on
#   2. Add cron job to ping every 10min:
#      https://cron-job.org → Add job → https://nyc-scan-api.onrender.com/api/debug/health
#   3. Use Render health checks (auto-enabled)
```

### Issue: "Out of memory (512MB limit)"

```bash
# CLIP model + PyTorch = ~400MB RAM
# Solutions:
#   1. Upgrade Render plan to 1GB RAM
#   2. Optimize model loading:
#      - Load model once at startup (already done)
#      - Use model.eval() to disable gradients
#      - Clear cache after each scan:
#        import gc; gc.collect()
```

---

## Scaling Considerations

### Horizontal Scaling

```bash
# Render: Auto-scaling (Starter plan+)
# Settings → Scaling → Instances: 1-5 (auto)

# Load balancer automatically distributes traffic
```

### Database Connection Pooling

```python
# Already configured in models/session.py
engine = create_async_engine(
    DATABASE_URL,
    poolclass=AsyncAdaptedQueuePool,
    pool_size=5,
    max_overflow=10
)
```

### CDN for Images

```bash
# Cloudflare R2 has built-in CDN
# Enable cache headers in utils/storage.py:
metadata = {
    'CacheControl': 'public, max-age=604800'  # 7 days
}
```

### Redis Cache Warming

```bash
# Pre-populate Redis with top 100 building images
python scripts/warm_cache.py
```

---

## Monitoring & Alerts

### Sentry Alerts

```bash
# Sentry → Alerts → Create Alert Rule
# Conditions:
#   - Error rate > 1% for 5 minutes
#   - 500 errors > 10 in 1 hour
# Actions:
#   - Email team
#   - Slack notification
```

### Uptime Monitoring

```bash
# Use: https://uptimerobot.com
# Add monitor:
#   - URL: https://nyc-scan-api.onrender.com/api/debug/health
#   - Interval: 5 minutes
#   - Alert contacts: Email, Slack
```

---

## Rollback Procedure

```bash
# In Render dashboard:
# 1. Go to: Dashboard → nyc-scan-api → Events
# 2. Find previous successful deploy
# 3. Click "Rollback to this deploy"

# OR via git:
git revert HEAD
git push origin main
# Render auto-deploys
```

---

## Security Checklist

- [ ] All environment variables set (no hardcoded secrets)
- [ ] Google Maps API key restricted to Street View only
- [ ] Supabase RLS policies enabled (if needed)
- [ ] R2 bucket has signed URLs for uploads
- [ ] HTTPS enforced (automatic on Render)
- [ ] CORS configured for mobile app domain
- [ ] Rate limiting enabled
- [ ] Sentry error tracking active
- [ ] Database backups enabled (Supabase auto-backups)

---

## Cost Estimate

**Free Tier (Development):**
- Render: $0 (spins down after 15min)
- Supabase: $0 (500MB, 2GB transfer)
- R2: $0 (10GB storage, 1M requests)
- Redis: $0 (10,000 commands/day)
- Street View API: $0.007/image (~$5-10 initial import)
- **Total: ~$5-10 one-time + $0/month**

**Production (Starter):**
- Render: $7/mo (always-on)
- Supabase: $25/mo (8GB, 100GB transfer)
- R2: $0.15/mo (20GB storage)
- Redis: $0 (free tier sufficient)
- Street View API: ~$10/mo (1,500 new images)
- Sentry: $0 (free tier: 5k errors/mo)
- **Total: ~$42-50/month**

---

## Next Steps

1. Review [Development Guide](DEVELOPMENT.md) for local setup
2. Review [Data Pipeline](DATA_PIPELINE.md) for data import process
3. Set up monitoring and alerts
4. Configure CI/CD for automated testing
5. Plan scaling strategy for production load

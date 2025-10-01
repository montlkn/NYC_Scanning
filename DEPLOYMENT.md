# Deployment Guide - NYC Scan Backend

## 🐳 Docker Deployment

### Local Testing with Docker Compose

1. **Build and run**:
```bash
docker-compose up --build
```

2. **Test the API**:
```bash
curl http://localhost:8000/health
```

3. **Stop**:
```bash
docker-compose down
```

---

## ☁️ Fly.io Deployment (Recommended)

### Prerequisites
- [Install flyctl](https://fly.io/docs/hands-on/install-flyctl/)
- Create Fly.io account: `flyctl auth signup`

### Initial Setup

1. **Login to Fly.io**:
```bash
flyctl auth login
```

2. **Launch the app** (creates app, don't deploy yet):
```bash
flyctl launch --no-deploy
```

Follow prompts:
- App name: `nyc-scan` (or your choice)
- Region: `ewr` (Newark - closest to NYC)
- PostgreSQL: No (we use Supabase)
- Redis: No

3. **Set secrets**:
```bash
# Database
flyctl secrets set DATABASE_URL="postgresql://postgres.xxx:password@aws-0-us-east-1.pooler.supabase.com:5432/postgres"

# Google Maps
flyctl secrets set GOOGLE_MAPS_API_KEY="AIzaSy..."

# Cloudflare R2
flyctl secrets set R2_ACCOUNT_ID="xxx"
flyctl secrets set R2_ACCESS_KEY_ID="xxx"
flyctl secrets set R2_SECRET_ACCESS_KEY="xxx"
flyctl secrets set R2_BUCKET="building-images"
flyctl secrets set R2_PUBLIC_URL="https://pub-xxx.r2.dev"
```

4. **Deploy**:
```bash
flyctl deploy
```

This will:
- Build Docker image with CLIP model (~500MB)
- Push to Fly.io registry
- Deploy to EWR region
- Auto-scale to 0 when idle (free tier friendly)

5. **Check deployment**:
```bash
flyctl status
flyctl logs
```

6. **Open in browser**:
```bash
flyctl open /health
```

---

## 🔧 Configuration

### Scaling

**Upgrade to more RAM** (if CLIP model needs it):
```bash
flyctl scale memory 1024  # 1GB RAM
```

**Multiple regions** (global edge):
```bash
flyctl regions add lhr ord  # London, Chicago
flyctl scale count 3
```

**Stay-awake mode** (disable auto-stop):
```bash
# Edit fly.toml:
[http_service]
  min_machines_running = 1  # Keep 1 always on
```

### Monitoring

```bash
flyctl logs          # Stream logs
flyctl status        # Check health
flyctl dashboard     # Web dashboard
flyctl ssh console   # SSH into container
```

### Secrets Management

```bash
flyctl secrets list                    # List secrets
flyctl secrets set KEY=VALUE          # Set secret
flyctl secrets unset KEY              # Remove secret
```

---

## 💰 Cost Estimates

### Free Tier (Hobby)
- 3 shared-cpu VMs (256MB RAM) - **FREE**
- 3GB persistent storage - **FREE**
- 160GB outbound transfer - **FREE**

**Our app**: 1 VM (512MB) = ~$2-3/mo

### Paid Tier (if scaling)
- 1 dedicated-cpu (1GB RAM): ~$6/mo
- 2 regions (global): ~$12/mo
- Total: **$12-15/mo**

vs DigitalOcean App Platform: **$20-25/mo**

---

## 🚀 CI/CD with GitHub Actions

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Fly.io

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - uses: superfly/flyctl-actions/setup-flyctl@master

      - name: Deploy to Fly.io
        run: flyctl deploy --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```

Get token: `flyctl auth token`

Store in GitHub: Settings → Secrets → `FLY_API_TOKEN`

---

## 🧪 Testing Deployment

```bash
# Health check
curl https://nyc-scan.fly.dev/health

# Test scan endpoint
curl -X POST https://nyc-scan.fly.dev/scan \
  -F "photo=@test_building.jpg" \
  -F "gps_lat=40.7484" \
  -F "gps_lng=-73.9857" \
  -F "compass_bearing=45" \
  -F "altitude=10" \
  -F "floor=3"
```

---

## 📱 Connect Mobile App

Update mobile app `.env`:

```bash
# Local development
EXPO_PUBLIC_API_URL=http://localhost:8000

# Production
EXPO_PUBLIC_API_URL=https://nyc-scan.fly.dev
```

In `ScanScreen.js`, it will automatically use the correct URL:
```javascript
const BACKEND_URL = process.env.EXPO_PUBLIC_API_URL || 'http://localhost:8000';
```

---

## 🐛 Troubleshooting

**Build fails - Out of memory**:
```bash
# Increase build VM RAM temporarily
flyctl deploy --build-arg RAM=2048
```

**App crashes - CLIP model too large**:
```bash
# Increase runtime memory
flyctl scale memory 1024
```

**Slow cold starts**:
```bash
# Keep 1 machine always running
flyctl scale count 1 --min-machines-running=1
```

**Database connection fails**:
```bash
# Check Supabase allows Fly.io IPs
# Or add to connection pooler allowlist
```

---

## 📊 Monitoring & Metrics

Fly.io provides built-in metrics:
- Request rate, latency, errors
- CPU, memory usage
- Connection stats

Access via: `flyctl dashboard` or https://fly.io/dashboard

---

## 🔄 Rollback

```bash
# List releases
flyctl releases

# Rollback to previous
flyctl releases rollback
```

---

**Ready to deploy? Run `flyctl deploy`** 🚀
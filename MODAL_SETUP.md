# Modal Deployment Setup Guide

This guide covers how to set up and deploy the NYC Scan API to Modal with GPU support.

## Prerequisites

1. Modal account (free tier available at https://modal.com)
2. All secrets from `.env` file
3. Modal CLI installed: `pip install modal`

## Step 1: Authenticate with Modal

```bash
modal setup
```

This will open a browser to authenticate and generate credentials.

## Step 2: Create Modal Secret

Create a Modal secret named `nyc-scan-secrets` containing all your environment variables:

```bash
modal secret create nyc-scan-secrets \
  DATABASE_URL="postgresql://..." \
  SCAN_DB_URL="postgresql://..." \
  SUPABASE_URL="https://..." \
  SUPABASE_KEY="eyJh..." \
  SUPABASE_SERVICE_KEY="eyJh..." \
  REDIS_URL="redis://..." \
  GOOGLE_MAPS_API_KEY="AIzaSy..." \
  R2_ACCOUNT_ID="your_account_id" \
  R2_ACCESS_KEY_ID="your_key" \
  R2_SECRET_ACCESS_KEY="your_secret" \
  R2_BUCKET="building-images" \
  R2_PUBLIC_URL="https://pub-..." \
  POSTHOG_API_KEY="phc_..." \
  SENTRY_DSN="https://..." \
  ENV="production" \
  DEBUG="false"
```

## Step 3: Deploy

From the project root directory:

```bash
modal deploy modal_app.py
```

This will:
1. Build the Docker image with all dependencies
2. Upload the backend code
3. Deploy the FastAPI application with GPU support
4. Return a URL like: `https://your-workspace--nyc-scan-api-fastapi-app.modal.run`

## Step 4: Verify Deployment

Test the health check endpoint:

```bash
curl https://your-workspace--nyc-scan-api-fastapi-app.modal.run/health
```

Expected response:
```json
{
  "status": "healthy",
  "timestamp": 1234567890.123,
  "checks": {
    "api": "ok",
    "clip_model": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

## Step 5: Update Frontend API Base URL

Update your mobile app to use the Modal URL:

```typescript
// In your frontend code
const API_BASE_URL = 'https://your-workspace--nyc-scan-api-fastapi-app.modal.run';
```

## Cost Estimation

**T4 GPU pricing:**
- Compute: $0.000164/second
- Average scan: 2-3 seconds = $0.0003-0.0005 per scan
- 1000 scans/day = ~$0.30-0.50/day = $9-15/month

**Free tier limits:**
- 50GB/month compute
- Enough for ~100,000 GPU seconds
- At 2 seconds per scan: ~50,000 scans/month

## Monitoring & Logs

View deployment logs:

```bash
modal tail nyc-scan-api
```

View on Modal dashboard:
1. Go to https://modal.com/dashboard
2. Select "nyc-scan-api"
3. View logs, metrics, and invocation history

## Updating Deployment

To redeploy after code changes:

```bash
modal deploy modal_app.py
```

Modal will automatically update the running version without downtime.

## Rolling Back

To revert to a previous deployment:

```bash
modal history nyc-scan-api
modal serve modal_app.py  # To test locally first
```

## Troubleshooting

### Secret not found error
Make sure the secret exists:
```bash
modal secret list
```

### GPU timeout
Increase timeout in `modal_app.py`:
```python
@app.function(timeout=120)  # Increase to 120 seconds
```

### Cold start issues
Cold starts (~2-5 seconds) are normal. Subsequent requests are fast. Consider:
- Keeping a warm instance with `modal run`
- Using Modal's scaling features

## Next Steps

1. Update your mobile app to use the Modal URL
2. Set up PostHog dashboard for analytics
3. Monitor performance and adjust GPU type if needed (T4 → L4 for faster inference)
4. Configure alerts in Sentry for error tracking

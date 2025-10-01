# Security Policy

## 🚨 Credential Leak Response

If API keys or secrets are accidentally committed:

### 1. Immediate Rotation

**Supabase:**
```bash
# Go to: https://supabase.com/dashboard/project/YOUR_PROJECT/settings/api
# Click "Reset API Key" and update .env
```

**Google Maps API:**
```bash
# Go to: https://console.cloud.google.com/apis/credentials
# Restrict API key to:
# - Referrer: your-domain.com/*
# - API: Street View Static API only
# - Delete old key, create new
```

**Cloudflare R2:**
```bash
# Go to: https://dash.cloudflare.com/
# R2 → Manage R2 API Tokens → Revoke
# Create new token with bucket-specific permissions
```

### 2. Remove from Git History

**Using BFG Repo Cleaner** (recommended):
```bash
brew install bfg
bfg --delete-files .env
bfg --replace-text passwords.txt  # List of secrets to remove
git reflog expire --expire=now --all
git gc --prune=now --aggressive
git push --force
```

**Or manually**:
```bash
git filter-branch --force --index-filter \
  'git rm --cached --ignore-unmatch backend/.env.example' \
  --prune-empty --tag-name-filter cat -- --all
git push --force
```

### 3. Verify Cleanup

```bash
# Check commit history
git log --all --oneline | head -20

# Search for leaked secrets
git log --all -p -S 'YOUR_SECRET_KEY'

# Confirm removal
git grep 'YOUR_SECRET_KEY' $(git rev-list --all)
```

## 🔒 Security Best Practices

### API Key Restrictions

**Google Maps API**:
- ✅ Restrict to Street View Static API only
- ✅ Set HTTP referrer restrictions
- ✅ Enable usage quotas (100 req/day for free tier)

**Cloudflare R2**:
- ✅ Use bucket-specific tokens
- ✅ Set read-only for public images
- ✅ Enable expiration on scan images (30 days)

**Supabase**:
- ✅ Use `postgres.PROJECT_ID` subdomain (pooler)
- ✅ Enable Row Level Security (RLS)
- ✅ Never expose `service_role` key publicly

### Environment Variables

**Development:**
```bash
# Use .env (never commit!)
cp backend/.env.example backend/.env
# Fill with real credentials
```

**Production (Fly.io):**
```bash
# Use secrets (encrypted at rest)
flyctl secrets set DATABASE_URL="..."
flyctl secrets set GOOGLE_MAPS_API_KEY="..."
flyctl secrets set R2_SECRET_ACCESS_KEY="..."
```

**Mobile App:**
```bash
# Use Expo environment variables
EXPO_PUBLIC_API_URL=https://nyc-scan.fly.dev
# NEVER include backend secrets in mobile app
```

## 📊 Monitoring

**Set up alerts**:
- Google Cloud Console → Quotas → Email alerts
- Cloudflare R2 → Usage notifications
- Supabase → Database > Logs → Unusual activity

## 🐛 Reporting Vulnerabilities

**DO NOT** open public issues for security vulnerabilities.

Instead:
1. Email: [your-email@example.com]
2. Include:
   - Description of vulnerability
   - Steps to reproduce
   - Potential impact
3. Expected response: 48 hours

## ✅ Security Checklist

- [ ] No secrets in `.env.example` (use fake values)
- [ ] `.env` is in `.gitignore`
- [ ] API keys have usage limits
- [ ] API keys are restricted by referrer/IP
- [ ] Supabase RLS policies enabled
- [ ] R2 buckets have proper ACLs
- [ ] Fly.io secrets are encrypted
- [ ] GitHub repo has branch protection
- [ ] Dependabot enabled for dependency updates

---

**Last Updated**: September 30, 2025

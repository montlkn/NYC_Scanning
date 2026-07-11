# Railway Migration — nyc-scan backend

Prep notes for moving the FastAPI backend off Render (`nyc-scanning.onrender.com`)
onto Railway, alongside the existing Railway search-DB (pgvector) service. This
is PREP ONLY — keep Render running until the Railway deploy is verified
end-to-end (search, scan, health) with production traffic.

## 1. Create the service

1. In the Railway project that already hosts the pgvector search DB, click
   **New → GitHub Repo** and select this repo.
2. Set **Root Directory** to `backend` (the repo root `railway.json` assumes
   this — `main.py` lives at `backend/main.py`, not repo root).
3. Railway auto-detects `railway.json` at the repo root for build/start
   commands and healthcheck path (`/health`). If Root Directory is set to
   `backend`, Railway also looks for `backend/railway.json` — if it doesn't
   find one there it falls back to the repo-root one; if that ordering
   doesn't work in the dashboard, copy `railway.json` into `backend/` as a
   fallback (safe: identical content, ignored by anything reading it from the
   repo root).
4. Build: `pip install -r requirements.txt` (Nixpacks auto-detects Python from
   `requirements.txt`; no Dockerfile needed, matching the current Render setup).
5. Start: `python main.py` — main.py already honors Railway's `$PORT` the same
   way it honors Render's (see `main.py`'s `os.getenv("PORT", ...)`).

## 2. Environment variables

Copy every value from the current Render service (Render dashboard → nyc-scan-api
→ Environment) into Railway's Variables tab. Full list, read from
`models/config.py` (`Settings`), `main.py`, and service modules:

**Required (app fails to boot / core features silently break without these):**
- `DATABASE_URL` — main Supabase Postgres (PostGIS) connection string
- `SUPABASE_URL`
- `SUPABASE_KEY` (anon key; `Settings.supabase_key`)
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_PUBLIC_URL`

**Required for full functionality (features degrade to [] / warnings without them):**
- `SEARCH_DB_URL` — Railway pgvector search DB (same project — can likely use
  Railway's private network URL instead of the public one once both services
  live in the same project; verify with `psql` before cutover)
- `FOOTPRINTS_DB_URL` — Railway PostGIS footprints DB (`Settings.footprints_db_url`)
- `SUPABASE_SERVICE_KEY` — service-role key, used by write paths
- `GROK_API_KEY` (or `XAI_API_KEY`) — lore generation + unified-search prose
  interpretation refinement (`services/grok.py`)
- `REDIS_URL` — caching; app runs without it but loses cache benefits
- `R2_BUCKET` (default `"building-images"` if unset)
- `R2_USER_IMAGES_BUCKET` / `R2_USER_IMAGES_PUBLIC_URL`

**Optional:**
- `GROK_TEXT_MODEL` (defaults to `grok-4-1-fast-non-reasoning`)
- `PERPLEXITY_API_KEY`
- `SENTRY_DSN` (has a hardcoded fallback in `main.py` — override if you want
  a separate Sentry project for Railway vs Render during dual-run)
- `POSTHOG_API_KEY`
- `CLIP_DEVICE` (`cpu` — matches `render.yaml`; no GPU on Railway starter tiers)
- `ENV` / `DEBUG`
- `PYTHON_VERSION` (Nixpacks var, e.g. `3.11`)

Confirm the exact set by diffing Render's live environment tab against this
list before cutover — this list was derived from static analysis of
`models/config.py` + `os.environ`/`os.getenv` call sites, not a live dump.

## 3. Healthcheck

`GET /health` — already implemented in `main.py`. Returns `200` with
`{"status": "healthy", ...}` when the footprints DB is reachable, `503`
`{"status": "degraded", ...}` otherwise. Point Railway's healthcheck at this
path (already set in `railway.json`).

There's also `GET /api/warm` (new — see `main.py`) which loads the embedding
model and probes the search DB. Not a healthcheck target (it's slow on cold
start), but useful to hit once after deploy to pre-warm before real traffic.

## 4. Cutover notes

1. Deploy to Railway, verify `/health` is green and `/api/warm` returns
   `{"status": "warm"}`.
2. Smoke-test manually: `/api/search?q=chrysler`,
   `/api/search/unified?q=art+deco+midtown`, a scan endpoint round-trip.
3. Point a SEPARATE iOS build config (or a feature flag) at the Railway URL
   and dogfood before flipping `SCAN_API_URL` in `jink/Secrets.xcconfig` for
   the main app.
4. Keep the Render service running (don't suspend/delete) until Railway has
   run clean for a full day of real traffic — Render's free tier spins down
   on idle anyway, so there's no meaningful cost to leaving it as a fallback.
5. Once verified, flip `SCAN_API_URL` and update DNS/any hardcoded
   `nyc-scanning.onrender.com` references, then decommission Render.

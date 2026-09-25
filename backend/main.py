"""
NYC Scan Backend - Point-and-Scan Building Identification
FastAPI application with computer vision-based building matching
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio
import time
import logging
import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
import posthog

from models.config import get_settings
from models.session import init_db, close_db
from models.footprints_session import init_footprints_engine, close_footprints_db, footprints_db_ok
from models.search_session import init_search_engine, close_search_db
from routers import (
    scan, scan_photo, buildings, stamps, vetting, rag, search, lore, websearch,
)
from utils.rate_limit import limiter, rate_limit_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Initialize Sentry for error tracking. Only for a real deploy -- there is
# no fallback DSN, because there used to be one, and it meant every local
# `curl localhost:8079` while debugging reported straight into the shared
# production Sentry project (tagged environment=development, but still the
# same issues list everyone triages).
sentry_dsn = os.getenv("SENTRY_DSN")
is_deployed = bool(os.getenv("RAILWAY_ENVIRONMENT"))
if sentry_dsn and is_deployed:
    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.1,  # 10% of transactions for performance monitoring
        environment="production",
        release="nyc-scan@1.0.0",
        send_default_pii=True,  # Include request headers and user data
    )
    logger.info("✅ Sentry initialized for error tracking")
elif sentry_dsn:
    logger.info("Sentry DSN set but not running under Railway -- skipping init (local/dev run)")
else:
    logger.warning("⚠️  SENTRY_DSN not set, error tracking disabled")

# Initialize PostHog for product analytics
posthog_api_key = os.getenv("POSTHOG_API_KEY")
if posthog_api_key:
    posthog.project_api_key = posthog_api_key
    posthog.host = 'https://app.posthog.com'
    logger.info("✅ PostHog initialized for analytics")
else:
    logger.warning("⚠️  POSTHOG_API_KEY not set, analytics disabled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown events
    """
    # Startup
    logger.info("🚀 Starting NYC Scan Backend...")
    logger.info(f"Environment: {settings.env}")
    logger.info(f"Debug Mode: {settings.debug}")

    # Say which external providers are actually wired up. Both of these gate
    # their features on a bare `if key:` and skip silently when unset, so a
    # missing variable produces a 200 with an empty field rather than an error.
    # That is indistinguishable from "this building has no lore" over HTTP, and
    # it cost a full debugging session to tell apart. The deploy log now answers
    # it directly.
    from services.openai_text import assert_configured as _assert_llm
    from services import brave_search as _brave
    _assert_llm()
    if _brave.is_configured():
        logger.info(f"[SEARCH] brave configured, max {_brave.MAX_QUERIES} queries/building")
    else:
        logger.warning(
            "[SEARCH] BRAVE_API_KEY is NOT set — the web tier is disabled; "
            "buildings with no LPC report or Wikipedia article fall back to a "
            "fields-only description."
        )

    # Initialize database connection
    logger.info("Initializing database connection...")
    await init_db()

    # Initialize footprints database (Railway)
    logger.info("Initializing footprints database connection (Railway)...")
    init_footprints_engine()

    # Initialize search database (dedicated pgvector service)
    logger.info("Initializing search database connection (pgvector)...")
    init_search_engine()

    # Eagerly warm the embedding model so the FIRST /search request isn't the
    # one paying the ~150MB fastembed load. Run in a thread executor so a slow
    # ONNX load doesn't block the event loop / startup probe.
    async def _warm_embeddings():
        try:
            from services.text_embeddings import _get_model
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _get_model)
        except Exception as e:
            logger.warning(f"Embedding model warm-up failed (will lazy-load on first use): {e}")

    asyncio.create_task(_warm_embeddings())

    # Install the corpus-derived query vocabulary (search_vocab, built by
    # scripts/build_search_vocab.py). The intent router's hardcoded seeds cover
    # 28 style words against 761 distinct style values; a miss swings the
    # buildings/venues corpus weights by 2.5x. Failure is non-fatal -- the
    # seeds remain in force.
    async def _install_vocab():
        try:
            from sqlalchemy import text as _sql_text
            from models.search_session import get_search_db
            from services.unified_search import install_derived_vocab
            async with get_search_db() as db:
                if db is None:
                    return
                rows = (await db.execute(_sql_text(
                    "SELECT kind, term FROM search_vocab"))).fetchall()
            # style only. The material ratios are not comparable (material is
            # not part of the embedded text, so df_source/df_text is not a
            # discriminativeness measure there) and "and" scored 1.02, which
            # would have classified every query containing "and" as style
            # intent. Material retrieval goes through the material_text
            # trigram leg instead, which needs no vocabulary.
            style = {t for k, t in rows if k == "style"}
            # STYLE ONLY -- deliberately not poi.
            #
            # Deriving POI nouns from venue category head nouns looked right
            # and shipped two regressions, caught in production:
            #   "chrystler building"      -> poi intent -> Chrystie Street venues
            #   "gothic church in harlem" -> poi intent -> St. Patrick's (Midtown), a grave
            # because "building" and "church" are both category heads. POI
            # intent drops the buildings corpus weight from 1.0 to 0.4, so a
            # building word landing in that set is strictly harmful -- the
            # opposite of the gap it was meant to close. Building types need
            # their own signal, not this one.
            #
            # Material is excluded too: material is not part of the embedded
            # text, so its df_source/df_text is not a discriminativeness
            # measure, and "and" scored 1.02.
            sizes = install_derived_vocab(style=style)
            logger.info(f"Derived search vocab installed: {sizes}")
        except Exception as e:
            logger.warning(f"Derived vocab unavailable, using seeds: {e}")

    asyncio.create_task(_install_vocab())

    # Re-fill the search rewrite cache after a deploy. Rows are versioned by
    # prompt (INTERP_VERSION), so a prompt change orphans every cached
    # rewrite and the next person to type each query would wait ~2.5s for
    # the model. This re-asks the model for the most-searched queries that
    # lack a current row. LLM calls only, no searches; one per second; the
    # advisory lock keeps multiple workers from doing it twice. On a deploy
    # that changed nothing it finds nothing to do and costs one query.
    async def _warm_search_rewrites():
        try:
            await asyncio.sleep(60)  # let the deploy settle and take traffic first
            from sqlalchemy import text as _sql_text
            from models.search_session import get_search_db
            from routers.search import _interpret_and_cache
            from services.openai_text import is_configured
            from services.unified_search import INTERP_VERSION
            if not is_configured():
                return
            async with get_search_db() as db:
                if db is None:
                    return
                got = (await db.execute(_sql_text("SELECT pg_try_advisory_lock(772201)"))).scalar()
                if not got:
                    return
                try:
                    rows = (await db.execute(_sql_text("""
                        SELECT lower(trim(l.query)) q
                          FROM search_query_log l
                          LEFT JOIN search_interpretation_cache c
                                 ON c.query = lower(trim(l.query))
                                AND (c.interpretation->>'v')::int = :v
                         WHERE length(trim(l.query)) >= 3 AND c.query IS NULL
                         GROUP BY 1 ORDER BY count(*) DESC LIMIT 200
                    """), {"v": INTERP_VERSION})).fetchall()
                    for (q,) in rows:
                        await _interpret_and_cache(q)
                        await asyncio.sleep(1.0)
                    logger.info(f"[search] warmed {len(rows)} rewrites for prompt v{INTERP_VERSION}")
                finally:
                    await db.execute(_sql_text("SELECT pg_advisory_unlock(772201)"))
        except Exception as e:
            logger.warning(f"[search] rewrite warm-up skipped: {e}")

    asyncio.create_task(_warm_search_rewrites())

    yield

    # Shutdown
    logger.info("Shutting down NYC Scan Backend...")
    await close_db()
    await close_footprints_db()
    await close_search_db()


# Initialize FastAPI app
app = FastAPI(
    title="NYC Scan API",
    description="Point-and-scan building identification using computer vision",
    version="1.0.0",
    lifespan=lifespan,
    debug=settings.debug,
    # The interactive docs publish every route and parameter to anyone with
    # the hostname. Development only.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

# Per-IP rate limiting. This API has no authentication, so the limiter is the
# only thing standing between a stranger with the hostname and unbounded
# inference/storage spend. See utils/rate_limit.py for how the numbers were
# chosen and why the client IP has to come out of X-Forwarded-For on Railway.
#
# The middleware applies LIMIT_DEFAULT to every route that carries no explicit
# @limiter.limit decorator; decorated routes use their own (stricter) limit
# instead. Health checks are exempted below so Railway's probe never trips it.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # The iOS client authenticates with bearer/anon keys in request HEADERS, not
    # cookies — so credentials mode is unused. And `allow_origins=["*"]` WITH
    # credentials is a CORS spec violation (browsers reject the response). Keeping
    # it False makes the wildcard valid and is the correct posture for a token-
    # in-header API.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add processing time to response headers"""
    start_time = time.time()
    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000
    response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    return response


# Exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc) if settings.debug else "An unexpected error occurred",
        }
    )


# Health check endpoint
@app.get("/")
@limiter.exempt
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "nyc-scan-backend",
        "version": "1.0.0",
        "environment": settings.env
    }


@app.get("/health")
@limiter.exempt  # Railway's platform probe hits this constantly; never limit it.
async def health_check():
    """Detailed health check.

    Actually probes the footprints DB (Railway) — the dependency that powers
    scans and is the one that drops connections. Previously this returned a
    hardcoded `"database": "ok"`, so the endpoint reported healthy even when
    Railway was down, defeating the point of a probe. Returns HTTP 503 +
    status "degraded" when the DB is unreachable so Render's probe and any
    monitor see the real state; the API process itself is still "ok".
    """
    db_ok = await footprints_db_ok()
    healthy = db_ok
    body = {
        "status": "healthy" if healthy else "degraded",
        "timestamp": time.time(),
        "checks": {
            "api": "ok",
            "footprints_db": "ok" if db_ok else "unreachable",
        },
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@app.get("/api/warm")
# Not a platform health check — it loads the embedding model and opens a DB
# connection, so it is worth a (loose) cap. A cron pre-warm calls it minutes
# apart; nothing legitimate needs more than a few a minute.
@limiter.limit("10/minute")
async def warm(request: Request):
    """Warms the embedding model + search DB connection. Call this from a
    Railway/Render cron or client pre-warm ping to avoid eating the cold-start
    cost on a real user's first search."""
    from services.text_embeddings import _get_model
    from models.search_session import get_search_db
    from sqlalchemy import text as sql_text

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _get_model)
    except Exception as e:
        logger.warning(f"/api/warm: embedding model load failed: {e}")
        return JSONResponse(status_code=503, content={"status": "cold"})

    try:
        async with get_search_db() as db:
            if db is not None:
                await db.execute(sql_text("SELECT 1"))
    except Exception as e:
        logger.warning(f"/api/warm: search DB probe failed: {e}")
        return JSONResponse(status_code=503, content={"status": "cold"})

    return {"status": "warm"}


# Include routers
app.include_router(scan.router, prefix="/api", tags=["scan"])
app.include_router(scan_photo.router, prefix="/api", tags=["scan"])
app.include_router(buildings.router, prefix="/api", tags=["buildings"])
# stamps + vetting are unmounted: no client calls them, the vetting routes
# took the acting user_id from the request BODY (anyone could verify, vote or
# earn XP as anyone), and /leaderboard handed out user ids that /users/{id}/*
# then answered for. Remount only behind utils.auth.verify_supabase_token.
app.include_router(rag.router, prefix="/api", tags=["rag"])
app.include_router(lore.router, prefix="/api", tags=["lore"])
app.include_router(search.router, prefix="/api", tags=["search"])
# Same /api/search prefix as above, different routes (POST /sources). Kept in
# its own module because it is a capped Brave fan-out for CLIENTS, not part of
# the vector-search ranking stack.
app.include_router(websearch.router, prefix="/api", tags=["search"])


if __name__ == "__main__":
    import uvicorn

    # Never use reload in production - it doubles memory usage. Render and
    # Railway both set a platform-identifying env var; treat either as prod
    # regardless of what DEBUG happens to be set to in that environment.
    is_paas = bool(os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT"))
    use_reload = settings.debug and not is_paas

    # Render (and most PaaS, including Railway) assigns a dynamic port via
    # $PORT. Honor it when present so the container actually binds where
    # the platform expects; without this, the process binds to
    # settings.api_port (8000), the health-check fails, and the platform
    # keeps the previous revision serving.
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=int(os.getenv("PORT", settings.api_port)),
        reload=use_reload,
        log_level="info" if settings.debug else "warning"
    )
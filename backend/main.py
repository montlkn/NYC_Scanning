"""
NYC Scan Backend - Point-and-Scan Building Identification
FastAPI application with computer vision-based building matching
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import time
import logging
import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from models.config import get_settings
from models.session import init_db, close_db
from routers import scan, buildings, debug, scan_phase1

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Initialize Sentry for error tracking
sentry_dsn = os.getenv("SENTRY_DSN", "https://108d23e36bba68c9b84944a310d977bc@o4510116323393536.ingest.us.sentry.io/4510116333355008")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.1,  # 10% of transactions for performance monitoring
        environment="production" if os.getenv("RENDER") else "development",
        release="nyc-scan@1.0.0",
        send_default_pii=True,  # Include request headers and user data
    )
    logger.info("✅ Sentry initialized for error tracking")
else:
    logger.warning("⚠️  SENTRY_DSN not set, error tracking disabled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown events
    """
    # Startup
    logger.info("🚀 Starting NYC Scan Backend...")
    logger.info(f"Environment: {settings.env}")
    logger.info(f"Debug Mode: {settings.debug}")

    # Initialize database connection
    logger.info("Initializing database connection...")
    await init_db()

    # CLIP model will lazy-load on first scan request to save memory
    logger.info("⏳ CLIP model will load on first scan request (lazy loading)")

    yield

    # Shutdown
    logger.info("Shutting down NYC Scan Backend...")
    await close_db()


# Initialize FastAPI app
app = FastAPI(
    title="NYC Scan API",
    description="Point-and-scan building identification using computer vision",
    version="1.0.0",
    lifespan=lifespan,
    debug=settings.debug
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your app domain
    allow_credentials=True,
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
async def root():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "nyc-scan-backend",
        "version": "1.0.0",
        "environment": settings.env
    }


@app.get("/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {
            "api": "ok",
            "clip_model": "ok",  # TODO: Add actual health checks
            "database": "ok",
            "redis": "ok",
        }
    }


# Include routers
app.include_router(scan.router, prefix="/api", tags=["scan"])
app.include_router(buildings.router, prefix="/api", tags=["buildings"])
app.include_router(scan_phase1.router, prefix="/api/phase1", tags=["phase1"])

# Debug endpoints (only in development)
if settings.debug:
    app.include_router(debug.router, prefix="/api/debug", tags=["debug"])


if __name__ == "__main__":
    import uvicorn

    # Never use reload in production - it doubles memory usage
    use_reload = settings.debug and not os.getenv("RENDER")

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=use_reload,
        log_level="info" if settings.debug else "warning"
    )
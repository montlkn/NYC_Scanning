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

from models.config import get_settings
from models.session import init_db, close_db
from routers import scan, buildings, debug
from services.clip_matcher import init_clip_model

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()


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

    # Initialize CLIP model on startup
    logger.info("Loading CLIP model...")
    init_clip_model()
    logger.info("✅ CLIP model loaded successfully")

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

# Debug endpoints (only in development)
if settings.debug:
    app.include_router(debug.router, prefix="/api/debug", tags=["debug"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        log_level="info" if settings.debug else "warning"
    )
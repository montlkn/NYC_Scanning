"""
Modal deployment configuration for NYC Scan API
Deploys FastAPI backend with GPU support for CLIP inference
"""

import modal


def download_clip_weights():
    """Bake CLIP weights into the image at build time — avoids HuggingFace download on cold start."""
    import open_clip
    open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')


# Define the image with all dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        # FastAPI Core
        "fastapi==0.109.0",
        "uvicorn[standard]==0.27.0",
        "python-multipart==0.0.6",
        "python-dotenv==1.0.0",
        # Database
        "sqlalchemy==2.0.25",
        "geoalchemy2==0.14.3",
        "psycopg2-binary==2.9.9",
        "psycopg[binary]==3.1.18",
        "asyncpg==0.29.0",
        "greenlet==3.0.3",
        # Supabase
        "supabase>=2.3.0",
        # Geospatial
        "shapely==2.0.3",
        # Image Processing & ML
        "pillow==10.2.0",
        "open-clip-torch==2.24.0",
        "torch==2.2.0",
        "torchvision==0.17.0",
        # HTTP & Storage
        "httpx>=0.24.0,<0.25.0",
        "aiohttp==3.9.3",
        "boto3==1.34.47",
        # Caching
        "redis==5.0.1",
        "hiredis==2.3.2",
        # Data Processing
        "pandas==2.2.0",
        "numpy==1.26.4",
        # Validation
        "pydantic==2.5.3",
        "pydantic-settings==2.1.0",
        # Error Tracking & Analytics
        "sentry-sdk[fastapi]==1.40.0",
        "posthog==3.5.0",
        # AI/LLM
        "google-generativeai>=0.3.0",
    )
    .env({"PYTHONPATH": "/root"})
    .run_function(download_clip_weights)
    .add_local_dir(
        "backend",
        remote_path="/root/backend",
        ignore=[
            "venv",
            "data",
            "__pycache__",
            "*.pyc",
            ".env",
            ".DS_Store",
            "*.jpg",
            "*.jpeg",
            "*.png",
            "tests",
            "scripts",
        ]
    )
)

# Image for scheduled jobs (includes scripts)
scheduled_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        # Database
        "sqlalchemy==2.0.25",
        "geoalchemy2==0.14.3",
        "psycopg2-binary==2.9.9",
        "psycopg[binary]==3.1.18",
        "asyncpg==0.29.0",
        "greenlet==3.0.3",
        # Image Processing & ML
        "pillow==10.2.0",
        "open-clip-torch==2.24.0",
        "torch==2.2.0",
        "torchvision==0.17.0",
        # HTTP
        "httpx>=0.24.0,<0.25.0",
        # Data Processing
        "numpy==1.26.4",
        # Validation
        "pydantic==2.5.3",
        "pydantic-settings==2.1.0",
    )
    .env({"PYTHONPATH": "/root"})
    .run_function(download_clip_weights)
    .add_local_dir(
        "backend",
        remote_path="/root/backend",
        ignore=[
            "venv",
            "data",
            "__pycache__",
            "*.pyc",
            ".env",
            ".DS_Store",
            "*.jpg",
            "*.jpeg",
            "*.png",
            "tests",
        ]
    )
)

app = modal.App("nyc-scan-api", image=image)


@app.function(
    gpu="T4",  # T4 GPU for fast CLIP inference (~2-3s vs 15-40s on CPU)
    memory=4096,
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    timeout=60,
    scaledown_window=60,  # 1 min idle keep-alive (~$0.05/session vs $0.25 at 300s)
    enable_memory_snapshot=True,
)
@modal.concurrent(max_inputs=10)  # Batch requests to share container costs
@modal.asgi_app()
def fastapi_app():
    """Deploy FastAPI application to Modal with T4 GPU for CLIP inference"""
    import sys
    # Add backend directory to path so relative imports work
    sys.path.insert(0, "/root/backend")

    # Import and return the FastAPI app
    from backend.main import app as fastapi_app_instance
    return fastapi_app_instance


@app.function(
    image=scheduled_image,
    gpu="T4",  # Need GPU for CLIP embeddings
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    timeout=3600,  # 1 hour max for batch processing
    schedule=modal.Cron("0 2 * * *"),  # Run daily at 2 AM UTC
)
async def daily_reembed_user_images():
    """
    Daily cron job to re-embed user-submitted images.

    This runs every day at 2 AM UTC to:
    - Process any images that failed initial embedding
    - Re-embed with updated model versions
    - Verify image quality
    - Clean up orphaned references
    """
    import sys
    sys.path.insert(0, "/root/backend")

    from scripts.reembed_user_images import main
    await main()


@app.function(
    image=scheduled_image,
    gpu="T4",
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    timeout=3600,
)
async def manual_reembed(force_all: bool = False):
    """
    Manually trigger re-embedding of user images.

    Usage:
        modal run modal_app.py::manual_reembed --force-all
    """
    import sys
    sys.path.insert(0, "/root/backend")

    from scripts.reembed_user_images import (
        reembed_user_images,
        get_user_image_stats,
        verify_user_image_quality
    )
    from models.config import get_settings
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()

    # Use psycopg3 (not asyncpg) — Supabase Session pooler rejects asyncpg auth
    database_url = settings.database_url
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    import ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    from sqlalchemy.pool import NullPool
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"sslmode": "require"}
    )
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        stats = await get_user_image_stats(db)
        print(f"Current stats: {stats}")

        result = await reembed_user_images(db, force_all=force_all)
        print(f"Re-embedding result: {result}")

        verify_result = await verify_user_image_quality(db)
        print(f"Verification result: {verify_result}")

    return result


@app.function(
    image=scheduled_image,
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    timeout=300,
)
async def create_tables():
    """
    Create scans and scan_feedback tables in the database.

    Usage:
        modal run modal_app.py::create_tables
    """
    import sys
    sys.path.insert(0, "/root/backend")

    from sqlalchemy.ext.asyncio import create_async_engine
    from models.config import get_settings
    from models.database import Base

    settings = get_settings()

    # Use psycopg3 — asyncpg fails auth with Supabase Session pooler
    database_url = settings.database_url
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    import ssl
    from sqlalchemy.pool import NullPool
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    engine = create_async_engine(
        database_url, echo=True, poolclass=NullPool,
        connect_args={"sslmode": "require"}
    )

    async with engine.begin() as conn:
        print("Creating tables...")
        await conn.run_sync(Base.metadata.create_all)
        print("✅ Tables created successfully!")

    await engine.dispose()
    return {"status": "success", "message": "Tables created"}



if __name__ == "__main__":
    print("Modal app configured. Deploy with: modal deploy modal_app.py")

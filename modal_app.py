"""
Modal deployment configuration for NYC Scan API
Deploys FastAPI backend with GPU support for CLIP inference
"""

import modal

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
        # Utilities
        "aiofiles==23.2.1",
        # Error Tracking & Analytics
        "sentry-sdk[fastapi]==1.40.0",
        "posthog==3.5.0",
    )
    .env({"PYTHONPATH": "/root"})
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

app = modal.App("nyc-scan-api", image=image)


@app.function(
    gpu="T4",  # For CLIP inference
    secrets=[modal.Secret.from_name("nyc-scan-secrets")],
    timeout=60,
)
@modal.asgi_app()
def fastapi_app():
    """Deploy FastAPI application to Modal with GPU support"""
    import sys
    # Add backend directory to path so relative imports work
    sys.path.insert(0, "/root/backend")

    # Import and return the FastAPI app
    from backend.main import app as fastapi_app_instance
    return fastapi_app_instance


if __name__ == "__main__":
    print("Modal app configured. Deploy with: modal deploy modal_app.py")

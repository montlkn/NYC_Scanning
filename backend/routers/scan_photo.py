"""
Slim photo-archive endpoint for the on-device scan flow.

Identification now happens entirely on the iOS client (footprint tiles +
GPS + ARKit heading); the client inserts its own `scans` row directly into
Supabase. The only backend job left on the scan path is archiving the photo
when the user explicitly opts in — which is async and fire-and-forget from
the client's perspective, so Render cold start no longer matters.
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from sqlalchemy import update
from datetime import datetime, timezone
import logging

from models.database import Scan
from models.session import AsyncSessionLocal
from utils.storage import upload_image
from utils.rate_limit import limiter, LIMIT_SCAN

logger = logging.getLogger(__name__)
router = APIRouter()

# These photos are keepers: the only way one reaches this endpoint is the user
# tapping SAVE on the scan sheet. They must NOT live under `scans/`, which an
# R2 lifecycle rule ("DeleteTemporaryScans") expires after 3 days — a holdover
# from the old pipeline, when the backend received photos for matching and they
# really were temporary. That rule silently deleted every saved photo, so the
# building sheet fell back to a placeholder days after the user saved one.
# The path now states the intent, instead of relying on nobody re-adding a
# lifecycle rule to the wrong prefix.
PHOTO_PREFIX = "contributions/"


@router.post("/scan-photo")
@limiter.limit(LIMIT_SCAN)
async def upload_scan_photo(
    request: Request,
    scan_id: str = Form(...),
    photo: UploadFile = File(...),
):
    """Store the user's opt-in scan photo on R2 and patch the scan row."""
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(status_code=400, detail="Empty photo")
    if len(photo_bytes) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo too large")

    try:
        photo_url = await upload_image(
            photo_bytes, f"{PHOTO_PREFIX}{scan_id}.jpg", create_thumbnail=True
        )
    except Exception as e:
        logger.error(f"scan-photo upload failed for {scan_id}: {e}")
        raise HTTPException(status_code=502, detail="Storage upload failed")

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Scan)
                .where(Scan.id == scan_id)
                .values(
                    user_photo_url=photo_url,
                    confirmed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
    except Exception as e:
        # Photo is on R2 either way; the row patch is best-effort.
        logger.error(f"scan-photo row patch failed for {scan_id}: {e}")

    return {"scan_id": scan_id, "photo_url": photo_url}

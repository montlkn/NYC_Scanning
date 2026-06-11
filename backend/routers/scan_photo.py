"""
Slim photo-archive endpoint for the on-device scan flow.

Identification now happens entirely on the iOS client (footprint tiles +
GPS + ARKit heading); the client inserts its own `scans` row directly into
Supabase. The only backend job left on the scan path is archiving the photo
when the user explicitly opts in — which is async and fire-and-forget from
the client's perspective, so Render cold start no longer matters.
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from sqlalchemy import update
from datetime import datetime, timezone
import logging

from models.database import Scan
from models.session import AsyncSessionLocal
from utils.storage import upload_image

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/scan-photo")
async def upload_scan_photo(
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
            photo_bytes, f"scans/{scan_id}.jpg", create_thumbnail=True
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

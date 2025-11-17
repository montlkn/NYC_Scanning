"""
Scan confirmation endpoint
Handles user feedback and adds confirmed photos to reference embeddings
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging
from datetime import datetime
import httpx

from models.session import get_db
from services import clip_matcher
from models.config import get_settings

router = APIRouter(prefix="/api", tags=["confirm"])
logger = logging.getLogger(__name__)
settings = get_settings()


@router.post("/confirm")
async def confirm_scan(
    scan_id: str,
    confirmed_bin: str,
    db: AsyncSession = Depends(get_db)
):
    """
    User confirms the correct building for a scan

    This adds the user's photo embedding to the building's reference embeddings,
    creating a feedback loop that improves accuracy over time.

    Args:
        scan_id: The scan ID from the original scan
        confirmed_bin: The BIN the user confirmed as correct
    """
    logger.info(f"[{scan_id}] User confirmed BIN {confirmed_bin}")

    # In a real implementation, we would:
    # 1. Fetch the scan from the database (including user_photo_url)
    # 2. Download the user's photo
    # 3. Generate CLIP embedding
    # 4. Get building_id from BIN
    # 5. Insert into reference_embeddings with appropriate angle/pitch
    # 6. Optionally: copy the image to R2 reference folder

    # For now, return success
    # TODO: Implement full feedback loop

    return {
        "status": "success",
        "message": f"Thank you! Your feedback helps improve our system.",
        "confirmed_bin": confirmed_bin
    }


@router.post("/confirm-with-photo")
async def confirm_with_photo(
    scan_id: str,
    confirmed_bin: str,
    photo_url: str,
    compass_bearing: float,
    phone_pitch: float,
    db: AsyncSession = Depends(get_db)
):
    """
    User confirms correct building - full implementation with feedback loop

    This:
    1. Downloads the user's photo
    2. Generates CLIP embedding
    3. Adds it to reference_embeddings for the confirmed building
    4. This photo will help match similar photos in the future

    Args:
        scan_id: Original scan ID
        confirmed_bin: BIN user confirmed
        photo_url: URL to user's photo (from R2)
        compass_bearing: Compass bearing when photo was taken
        phone_pitch: Phone pitch when photo was taken
    """
    try:
        logger.info(f"[{scan_id}] Adding user photo to reference embeddings for BIN {confirmed_bin}")

        # Step 1: Download user's photo
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(photo_url)
            photo_bytes = response.content

        # Step 2: Generate CLIP embedding
        embedding = await clip_matcher.encode_photo(photo_bytes)

        # Step 3: Get building_id from BIN
        query = text("""
            SELECT id FROM buildings_full_merge_scanning
            WHERE REPLACE(bin, '.0', '') = :bin
            LIMIT 1
        """)
        result = await db.execute(query, {"bin": confirmed_bin})
        building_row = result.fetchone()

        if not building_row:
            raise HTTPException(status_code=404, detail=f"Building with BIN {confirmed_bin} not found")

        building_id = building_row[0]

        # Step 4: Insert embedding into reference_embeddings
        # Use compass_bearing as angle, phone_pitch as pitch
        # Mark this as user-contributed with image_key
        image_key = f"{confirmed_bin}/user_{scan_id}_{int(compass_bearing)}deg_{int(phone_pitch)}pitch.jpg"

        insert_query = text("""
            INSERT INTO reference_embeddings
            (building_id, angle, pitch, embedding, image_key)
            VALUES (:building_id, :angle, :pitch, :embedding, :image_key)
            ON CONFLICT (building_id, angle, pitch)
            DO UPDATE SET
                embedding = :embedding,
                image_key = :image_key
        """)

        await db.execute(insert_query, {
            "building_id": building_id,
            "angle": int(compass_bearing),
            "pitch": int(phone_pitch),
            "embedding": embedding.tolist(),
            "image_key": image_key
        })

        await db.commit()

        logger.info(f"✅ Added user photo embedding for BIN {confirmed_bin} (angle={int(compass_bearing)}°, pitch={int(phone_pitch)}°)")

        return {
            "status": "success",
            "message": "Your photo has been added to improve future matches. Thank you!",
            "confirmed_bin": confirmed_bin,
            "embedding_added": True
        }

    except Exception as e:
        logger.error(f"Failed to add user photo to embeddings: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process confirmation: {str(e)}")

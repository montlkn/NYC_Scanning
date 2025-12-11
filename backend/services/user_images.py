"""
User image storage and re-embedding service.
Stores user-submitted images in R2 and adds them to the reference image database
to improve the ML model over time.
"""

import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional

from utils.storage import upload_image
from services.clip_matcher import encode_photo
from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def store_user_image(
    photo_bytes: bytes,
    scan_id: str,
    confirmed_bin: str,
    user_id: Optional[str] = None,
    gps_lat: float = None,
    gps_lng: float = None,
    compass_bearing: float = None,
) -> str:
    """
    Store user image in the user-images BUCKET (separate from building-images).
    Structure: {user_id}/{BIN}/{scan_id}_{bearing}deg_{timestamp}.jpg

    This keeps user contributions in a separate bucket for better organization.

    Args:
        photo_bytes: The image bytes
        scan_id: Unique scan ID
        confirmed_bin: The BIN of the building (required for folder structure)
        user_id: Optional user ID for organization
        gps_lat: Latitude where photo was taken
        gps_lng: Longitude where photo was taken
        compass_bearing: Direction camera was facing

    Returns:
        URL of the uploaded image
    """
    from datetime import datetime

    # Create folder structure: {user_id}/{BIN}/{scan_id}_{bearing}deg_{timestamp}.jpg
    user_folder = user_id if user_id else "anonymous"

    # Clean BIN to remove any decimal points (e.g., "1234567.0" -> "1234567")
    clean_bin = str(confirmed_bin).replace('.0', '').strip()

    # Add bearing and timestamp to filename for better organization
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    bearing_str = f"{int(compass_bearing)}deg" if compass_bearing else "0deg"

    key = f"{user_folder}/{clean_bin}/{scan_id}_{bearing_str}_{timestamp}.jpg"

    logger.info(f"[{scan_id}] Storing user image in user-images bucket: {key}")

    # Upload to user-images bucket (different from building-images)
    from utils.storage import upload_image_to_bucket

    image_url = await upload_image_to_bucket(
        photo_bytes,
        key,
        bucket=settings.r2_user_images_bucket,
        public_url=settings.r2_user_images_public_url or settings.r2_public_url,
        content_type='image/jpeg',
        make_public=True,
        create_thumbnail=True
    )

    logger.info(f"[{scan_id}] User image stored: {image_url}")

    return image_url


async def add_user_image_to_references(
    db: AsyncSession,
    photo_bytes: bytes,
    image_key: str,
    confirmed_bin: str,
    scan_id: str,
    gps_lat: float,
    gps_lng: float,
    compass_bearing: float,
    phone_pitch: float = 0.0,
) -> bool:
    """
    Add a confirmed user image to the reference_embeddings table with CLIP embedding.
    This improves the model by providing more diverse angles and lighting conditions.

    Args:
        db: Database session
        photo_bytes: The image bytes for embedding
        image_key: R2 object key where image is stored
        confirmed_bin: The BIN of the building (must be confirmed by user)
        scan_id: Original scan ID for tracking
        gps_lat: Latitude where photo was taken
        gps_lng: Longitude where photo was taken
        compass_bearing: Direction camera was facing
        phone_pitch: Phone pitch angle (default 0)

    Returns:
        True if successfully added, False otherwise
    """
    try:
        logger.info(f"[{scan_id}] Generating CLIP embedding for user image...")

        # Generate CLIP embedding for the user image
        embedding = await encode_photo(photo_bytes)
        embedding_list = embedding.tolist()

        logger.info(f"[{scan_id}] Embedding generated, shape: {len(embedding_list)}")

        # Clean BIN
        clean_bin = str(confirmed_bin).replace('.0', '').strip()

        # First, get the building_id from the buildings table
        building_query = text("""
            SELECT id FROM buildings_full_merge_scanning
            WHERE REPLACE(bin, '.0', '') = :bin
            LIMIT 1
        """)

        result = await db.execute(building_query, {"bin": clean_bin})
        building_row = result.fetchone()

        if not building_row:
            logger.error(f"[{scan_id}] Building with BIN {clean_bin} not found in database")
            return False

        building_id = building_row[0]

        # Insert into reference_embeddings table
        # Schema: building_id, angle, pitch, embedding, image_key
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
            'building_id': building_id,
            'angle': int(compass_bearing),
            'pitch': int(phone_pitch),
            'embedding': embedding_list,
            'image_key': image_key,
        })

        await db.commit()

        logger.info(f"[{scan_id}] User image added to reference_embeddings for BIN {clean_bin} (angle={int(compass_bearing)}°, pitch={int(phone_pitch)}°)")
        return True

    except Exception as e:
        logger.error(f"[{scan_id}] Failed to add user image to references: {e}", exc_info=True)
        await db.rollback()
        return False


async def process_confirmed_scan(
    db: AsyncSession,
    scan_id: str,
    photo_bytes: bytes,
    user_photo_url: str,
    confirmed_bin: str,
    gps_lat: float,
    gps_lng: float,
    compass_bearing: float,
    phone_pitch: float = 0.0,
    user_id: Optional[str] = None,
) -> dict:
    """
    Process a confirmed scan by storing the user's image in the building's folder
    and adding it to the reference database with CLIP embedding.
    This is called after the user confirms which building they scanned.

    Args:
        db: Database session
        scan_id: The scan ID
        photo_bytes: Original photo bytes
        user_photo_url: URL where photo is currently stored (will be moved)
        confirmed_bin: BIN of the building user confirmed
        gps_lat: Latitude of scan
        gps_lng: Longitude of scan
        compass_bearing: Compass bearing during scan
        phone_pitch: Phone pitch angle (default 0)
        user_id: Optional user ID

    Returns:
        Dict with status and details
    """
    result = {
        'stored_in_bin_folder': False,
        'user_image_url': None,
        'image_key': None,
        'added_to_references': False,
        'embedding_generated': False,
        'error': None
    }

    try:
        # Create the image key for R2 storage
        # Structure: user-images/{user_id}/{BIN}/{scan_id}.jpg
        user_folder = user_id if user_id else "anonymous"
        clean_bin = str(confirmed_bin).replace('.0', '').strip()
        image_key = f"user-images/{user_folder}/{clean_bin}/{scan_id}.jpg"

        # Store user image in the BIN folder structure
        user_image_url = await store_user_image(
            photo_bytes=photo_bytes,
            scan_id=scan_id,
            confirmed_bin=confirmed_bin,
            user_id=user_id,
            gps_lat=gps_lat,
            gps_lng=gps_lng,
            compass_bearing=compass_bearing,
        )
        result['stored_in_bin_folder'] = True
        result['user_image_url'] = user_image_url
        result['image_key'] = image_key

        # Add to reference_embeddings with embedding
        success = await add_user_image_to_references(
            db=db,
            photo_bytes=photo_bytes,
            image_key=image_key,
            confirmed_bin=confirmed_bin,
            scan_id=scan_id,
            gps_lat=gps_lat,
            gps_lng=gps_lng,
            compass_bearing=compass_bearing,
            phone_pitch=phone_pitch,
        )

        result['added_to_references'] = success
        result['embedding_generated'] = success

        if success:
            logger.info(f"[{scan_id}] Successfully processed confirmed scan for BIN {confirmed_bin}")
        else:
            result['error'] = 'Failed to add to reference_embeddings'

    except Exception as e:
        logger.error(f"[{scan_id}] Error processing confirmed scan: {e}", exc_info=True)
        result['error'] = str(e)

    return result

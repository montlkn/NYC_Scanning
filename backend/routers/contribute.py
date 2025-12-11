"""
Building contribution API endpoints
Allows users to contribute metadata for buildings not well-documented in the database
"""

from fastapi import APIRouter, Form, HTTPException, Depends, BackgroundTasks, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, List, Dict

from models.session import get_db
from models.database import UserContributedBuilding
from services.building_contribution import (
    reverse_geocode_google,
    lookup_bin_from_gps,
    get_building_metadata_from_pluto,
    get_building_height_from_building_dataset
)
from services.data_enrichment import enrich_building_with_fallback
from utils.storage import upload_image, upload_image_to_bucket
from models.config import get_settings
from services.clip_matcher import encode_photo

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


@router.post("/scan/get-address-options")
async def get_address_options_from_gps(
    scan_id: str = Form(...),
    gps_lat: float = Form(...),
    gps_lng: float = Form(...),
):
    """
    Step 1: Get address options from GPS coordinates.

    User scans a building → backend reverse geocodes → returns multiple address options
    User selects the correct address → proceeds to Step 2 (contribute-building)

    Returns:
    {
        'addresses': [
            {
                'address': '123 Main St, New York, NY 10001',
                'formatted_address': '123 Main St',
                'place_id': '...',
                'lat': 40.xxx,
                'lng': -73.xxx
            },
            ...
        ],
        'bin_bbl_lookup': {
            'bin': '1234567',
            'bbl': '1234567890',
            'distance_meters': 15.2
        }
    }
    """
    try:
        logger.info(f"[{scan_id}] Getting address options for GPS ({gps_lat}, {gps_lng})")

        # Reverse geocode to get address options
        addresses = await reverse_geocode_google(gps_lat, gps_lng)

        if not addresses:
            raise HTTPException(
                status_code=404,
                detail="Could not find any addresses near this location"
            )

        # Try to look up BIN/BBL from GPS
        bin_bbl_result = lookup_bin_from_gps(gps_lat, gps_lng, radius_meters=50)

        response = {
            'scan_id': scan_id,
            'addresses': addresses,
            'bin_bbl_lookup': None
        }

        if bin_bbl_result:
            bin_value, bbl = bin_bbl_result
            response['bin_bbl_lookup'] = {
                'bin': bin_value,
                'bbl': bbl,
                'message': 'Found building in NYC datasets'
            }

        logger.info(f"[{scan_id}] Found {len(addresses)} address options")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Error getting address options: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get address options: {str(e)}")


@router.post("/scan/contribute-building")
async def contribute_building(
    scan_id: str = Form(...),
    address: str = Form(...),
    bin: str = Form(None),  # From BIN/BBL lookup or manual entry
    bbl: str = Form(None),
    gps_lat: float = Form(...),
    gps_lng: float = Form(...),
    gps_accuracy: float = Form(None),
    compass_bearing: float = Form(...),
    phone_pitch: float = Form(0.0),
    # User-provided metadata (optional)
    building_name: str = Form(None),
    architect: str = Form(None),
    year_built: int = Form(None),
    architectural_style: str = Form(None),
    user_notes: str = Form(None),
    # User ID (nullable for anonymous)
    user_id: str = Form(None),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    """
    Step 2: Submit a building contribution with user-provided metadata.

    Flow:
    1. User provides address (from selection in Step 1) + optional metadata
    2. Backend creates contribution record with 'pending' status
    3. Backend triggers enrichment pipeline in background
    4. (Optional) Backend auto-fetches Street View images

    Returns:
    {
        'contribution_id': 123,
        'status': 'pending',
        'enrichment_triggered': true,
        'message': 'Thank you for your contribution!'
    }
    """
    try:
        logger.info(f"[{scan_id}] Receiving building contribution for {address}")

        # If BIN not provided, try to look it up
        if not bin:
            logger.info(f"[{scan_id}] BIN not provided, attempting lookup from GPS")
            bin_bbl_result = lookup_bin_from_gps(gps_lat, gps_lng, radius_meters=50)
            if bin_bbl_result:
                bin, bbl = bin_bbl_result
                logger.info(f"[{scan_id}] Found BIN={bin}, BBL={bbl} from GPS lookup")
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Could not determine BIN for this location. Building may not be in NYC datasets."
                )

        # Get initial photo URL from scan cache (or from scans table)
        scan_query = text("SELECT user_photo_url FROM scans WHERE id = :scan_id")
        result = await db.execute(scan_query, {"scan_id": scan_id})
        scan_row = result.fetchone()

        if not scan_row:
            raise HTTPException(status_code=404, detail="Scan not found")

        initial_photo_url = scan_row[0]

        # Try to get building_id if already in main database
        building_query = text("""
            SELECT id FROM buildings_full_merge_scanning
            WHERE REPLACE(bin, '.0', '') = :bin
            LIMIT 1
        """)
        result = await db.execute(building_query, {"bin": bin.replace('.0', '')})
        building_row = result.fetchone()
        building_id = building_row[0] if building_row else None

        # Get metadata from PLUTO if available
        pluto_metadata = None
        if bbl:
            pluto_metadata = get_building_metadata_from_pluto(bbl)
            if pluto_metadata:
                # Use PLUTO data as fallback if user didn't provide
                if not year_built and pluto_metadata.get('year_built'):
                    year_built = pluto_metadata['year_built']

        # Get height from BUILDING dataset
        height_feet = get_building_height_from_building_dataset(bin) if bin else None

        # Create contribution record
        contribution = UserContributedBuilding(
            bin=bin,
            bbl=bbl,
            building_id=building_id,
            address=address,
            building_name=building_name,
            gps_lat=gps_lat,
            gps_lng=gps_lng,
            gps_accuracy=gps_accuracy,
            year_built=year_built,
            architect=architect,
            architectural_style=architectural_style,
            height_feet=height_feet,
            user_notes=user_notes,
            submitted_by=user_id,
            initial_photo_url=initial_photo_url,
            initial_scan_id=scan_id,
            compass_bearing=compass_bearing,
            phone_pitch=phone_pitch,
            status='pending',
            enrichment_status='pending',
            created_at=datetime.now(timezone.utc)
        )

        db.add(contribution)
        await db.commit()
        await db.refresh(contribution)

        logger.info(f"[{scan_id}] Created contribution ID={contribution.id} for BIN={bin}")

        # Trigger enrichment in background
        if background_tasks:
            background_tasks.add_task(
                _enrich_contribution_background,
                contribution.id,
                address,
                building_name
            )

        return {
            'contribution_id': contribution.id,
            'bin': bin,
            'bbl': bbl,
            'status': 'pending',
            'enrichment_triggered': True,
            'message': 'Thank you for your contribution! We will enrich it with additional data.'
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Error creating contribution: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create contribution: {str(e)}")


async def _enrich_contribution_background(
    contribution_id: int,
    address: str,
    building_name: Optional[str]
):
    """
    Background task to enrich a building contribution using Exa/web search.
    Updates the contribution record with enriched data.
    """
    from models.session import get_db_context

    try:
        logger.info(f"Starting enrichment for contribution ID={contribution_id}")

        # Run enrichment
        enriched_data = await enrich_building_with_fallback(
            address=address,
            building_name=building_name
        )

        # Update contribution record with enriched data
        async with get_db_context() as db:
            update_query = text("""
                UPDATE user_contributed_buildings
                SET
                    architect = COALESCE(architect, :architect),
                    year_built = COALESCE(year_built, :year_built),
                    architectural_style = COALESCE(architectural_style, :architectural_style),
                    landmark_status = COALESCE(landmark_status, :landmark_status),
                    notable_features = COALESCE(notable_features, :notable_features),
                    enrichment_status = 'completed',
                    enrichment_source = :enrichment_source,
                    enrichment_data = :enrichment_data::jsonb,
                    enrichment_confidence = :confidence,
                    enrichment_completed_at = NOW()
                WHERE id = :contribution_id
            """)

            await db.execute(update_query, {
                'contribution_id': contribution_id,
                'architect': enriched_data.get('architect'),
                'year_built': enriched_data.get('year_built'),
                'architectural_style': enriched_data.get('architectural_style'),
                'landmark_status': enriched_data.get('landmark_status'),
                'notable_features': enriched_data.get('notable_features'),
                'enrichment_source': enriched_data.get('enrichment_source', 'exa'),
                'enrichment_data': str(enriched_data.get('raw_results', [])),
                'confidence': enriched_data.get('confidence', 0)
            })

            await db.commit()

        logger.info(
            f"Enrichment completed for contribution ID={contribution_id}, "
            f"confidence={enriched_data.get('confidence', 0):.2f}"
        )

    except Exception as e:
        logger.error(f"Error enriching contribution ID={contribution_id}: {e}", exc_info=True)


@router.get("/contributions/{contribution_id}")
async def get_contribution(
    contribution_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get details of a specific contribution"""
    try:
        contribution = await db.get(UserContributedBuilding, contribution_id)

        if not contribution:
            raise HTTPException(status_code=404, detail="Contribution not found")

        return {
            'id': contribution.id,
            'bin': contribution.bin,
            'bbl': contribution.bbl,
            'address': contribution.address,
            'building_name': contribution.building_name,
            'year_built': contribution.year_built,
            'architect': contribution.architect,
            'architectural_style': contribution.architectural_style,
            'status': contribution.status,
            'enrichment_status': contribution.enrichment_status,
            'enrichment_confidence': contribution.enrichment_confidence,
            'created_at': contribution.created_at.isoformat() if contribution.created_at else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting contribution: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get contribution")


@router.post("/contributions/photos")
async def contribute_photos_only(
    photos: List[UploadFile] = File(...),
    photo_angles: List[str] = Form([]),
    user_id: str = Form(None),
    gps_lat: float = Form(None),
    gps_lng: float = Form(None),
    building_bin: str = Form(None),
    contribution_type: str = Form("photos_only"),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    """
    Submit photo-only contribution for building recognition training.

    This is a lightweight contribution path that allows users to:
    - Take multiple photos from different angles
    - Earn XP for helping train CLIP embeddings
    - No metadata required

    Each photo earns 10 XP.

    Args:
        photos: List of photo files
        photo_angles: List of angle labels (front, left, right, detail)
        user_id: User ID (can be anonymous)
        gps_lat: GPS latitude
        gps_lng: GPS longitude
        building_bin: Building BIN if known
        contribution_type: Type of contribution (photos_only)

    Returns:
        {
            'contribution_id': 123,
            'photos_uploaded': 3,
            'xp_earned': 30,
            'status': 'success'
        }
    """
    try:
        contribution_uuid = str(uuid.uuid4())[:8]
        logger.info(f"[{contribution_uuid}] Receiving {len(photos)} photos for contribution")

        # Try to find BIN from GPS if not provided
        bin_value = building_bin
        bbl_value = None
        if not bin_value and gps_lat and gps_lng:
            bin_bbl_result = lookup_bin_from_gps(gps_lat, gps_lng, radius_meters=50)
            if bin_bbl_result:
                bin_value, bbl_value = bin_bbl_result
                logger.info(f"[{contribution_uuid}] Found BIN={bin_value} from GPS")

        # Upload photos to user-images bucket
        # Path structure: user-images/{user_id}/{BIN}/{scan_id}_{angle}_{timestamp}.jpg
        user_folder = user_id if user_id and user_id != 'anonymous' else 'anonymous'
        bin_folder = bin_value if bin_value and bin_value != 'unknown' else 'unknown'

        photo_urls = []
        for i, photo in enumerate(photos):
            try:
                # Read photo bytes
                photo_bytes = await photo.read()

                # Generate key with proper structure
                angle = photo_angles[i] if i < len(photo_angles) else f"angle_{i}"
                timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
                key = f"{user_folder}/{bin_folder}/{contribution_uuid}_{angle}_{timestamp}.jpg"

                # Upload to user-images bucket (NOT building-images)
                user_images_public_url = settings.r2_user_images_public_url or settings.r2_public_url
                url = await upload_image_to_bucket(
                    image_bytes=photo_bytes,
                    key=key,
                    bucket=settings.r2_user_images_bucket,
                    public_url=user_images_public_url,
                    content_type='image/jpeg',
                    make_public=True,
                    create_thumbnail=True
                )
                photo_urls.append({
                    'url': url,
                    'angle': angle,
                    'key': key
                })
                logger.info(f"[{contribution_uuid}] Uploaded photo {i+1} to user-images: {key}")

            except Exception as e:
                logger.error(f"[{contribution_uuid}] Failed to upload photo {i}: {e}")
                # Continue with other photos

        if not photo_urls:
            raise HTTPException(status_code=500, detail="Failed to upload any photos")

        # Create contribution record (minimal data for photo-only)
        contribution = UserContributedBuilding(
            bin=bin_value or 'unknown',
            bbl=bbl_value,
            gps_lat=gps_lat,
            gps_lng=gps_lng,
            submitted_by=user_id or 'anonymous',
            status='pending',
            enrichment_status='not_needed',
            user_notes=f"Photo-only contribution: {len(photo_urls)} photos ({', '.join([p['angle'] for p in photo_urls])})",
            initial_photo_url=photo_urls[0]['url'] if photo_urls else None,
            created_at=datetime.now(timezone.utc)
        )

        db.add(contribution)
        await db.commit()
        await db.refresh(contribution)

        # Store additional photo URLs in a separate table or JSON
        # For now, we'll store them in user_notes as JSON
        if len(photo_urls) > 1:
            import json
            additional_photos = json.dumps([p['url'] for p in photo_urls[1:]])
            await db.execute(
                text("""
                    UPDATE user_contributed_buildings
                    SET user_notes = :notes
                    WHERE id = :id
                """),
                {
                    'notes': f"Photos: {json.dumps(photo_urls)}",
                    'id': contribution.id
                }
            )
            await db.commit()

        xp_earned = len(photo_urls) * 10

        logger.info(
            f"[{contribution_uuid}] Photo contribution complete: "
            f"id={contribution.id}, photos={len(photo_urls)}, xp={xp_earned}"
        )

        # Generate embeddings in background for immediate re-scan matching
        if background_tasks and bin_value and bin_value != "unknown":
            logger.info(f"[{contribution_uuid}] Queuing embedding generation for BIN {bin_value}")
            background_tasks.add_task(
                _generate_embeddings_for_contribution,
                photo_urls=photo_urls,
                bin_value=bin_value,
                contribution_id=contribution.id
            )

        return {
            'contribution_id': contribution.id,
            'photos_uploaded': len(photo_urls),
            'photo_urls': [p['url'] for p in photo_urls],
            'xp_earned': xp_earned,
            'status': 'success',
            'message': f'Thank you! {len(photo_urls)} photos uploaded successfully.'
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing photo contribution: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process photos: {str(e)}")


async def _generate_embeddings_for_contribution(
    photo_urls: List[Dict],
    bin_value: str,
    contribution_id: int
):
    """
    Background task to generate CLIP embeddings for contributed photos.
    This makes re-scans find the contributed building immediately.
    """
    from models.session import get_db_context
    import httpx

    try:
        logger.info(f"[Embedding] Starting embedding generation for BIN {bin_value}, contribution {contribution_id}")

        # Download and encode each photo
        embeddings_to_save = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, photo_info in enumerate(photo_urls):
                try:
                    photo_url = photo_info['url']
                    angle = photo_info['angle']

                    logger.info(f"[Embedding] Encoding photo {i + 1}/{len(photo_urls)} ({angle})")

                    # Download photo
                    response = await client.get(photo_url)
                    photo_bytes = response.content

                    # Generate embedding
                    embedding = await encode_photo(photo_bytes)

                    embeddings_to_save.append({
                        'bin': bin_value,
                        'embedding': embedding.tolist(),
                        'source': 'user_contribution',
                        'contribution_id': contribution_id,
                        'angle': angle,
                        'image_url': photo_url
                    })

                except Exception as e:
                    logger.error(f"[Embedding] Failed to encode photo {i}: {e}")
                    continue

        if not embeddings_to_save:
            logger.warning(f"[Embedding] No embeddings generated for contribution {contribution_id}")
            return

        # Save embeddings to database
        async with get_db_context() as db:
            for emb_data in embeddings_to_save:
                try:
                    # Insert into reference_embeddings table
                    insert_query = text("""
                        INSERT INTO reference_embeddings
                        (bin, embedding, source, metadata, created_at)
                        VALUES (:bin, :embedding, :source, :metadata, NOW())
                    """)

                    await db.execute(insert_query, {
                        'bin': emb_data['bin'],
                        'embedding': str(emb_data['embedding']),  # Store as JSON string
                        'source': emb_data['source'],
                        'metadata': str({
                            'contribution_id': emb_data['contribution_id'],
                            'angle': emb_data['angle'],
                            'image_url': emb_data['image_url']
                        })
                    })

                except Exception as e:
                    logger.error(f"[Embedding] Failed to save embedding: {e}")

            await db.commit()

        logger.info(
            f"[Embedding] Successfully generated and saved {len(embeddings_to_save)} "
            f"embeddings for BIN {bin_value}, contribution {contribution_id}"
        )

    except Exception as e:
        logger.error(f"[Embedding] Background task failed: {e}", exc_info=True)


@router.get("/contributions/by-location")
async def get_contributions_by_location(
    gps_lat: float,
    gps_lng: float,
    radius_meters: float = 50.0,
    db: AsyncSession = Depends(get_db)
):
    """
    Get user contributions near a GPS location.

    Used when re-scanning to check if user has already contributed
    data for this location.

    Returns:
        {
            'contributions': [
                {
                    'id': 123,
                    'address': '...',
                    'photo_url': '...',
                    'created_at': '...',
                    ...
                }
            ]
        }
    """
    try:
        # Use PostGIS or simple bounding box for location search
        # Simple bounding box approximation: 1 degree lat ~ 111km, 1 degree lng ~ 85km at NYC
        lat_delta = radius_meters / 111000
        lng_delta = radius_meters / 85000

        query = text("""
            SELECT
                id, bin, bbl, address, building_name, year_built,
                architect, architectural_style, initial_photo_url,
                status, created_at, submitted_by, user_notes
            FROM user_contributed_buildings
            WHERE gps_lat BETWEEN :min_lat AND :max_lat
            AND gps_lng BETWEEN :min_lng AND :max_lng
            ORDER BY created_at DESC
            LIMIT 10
        """)

        result = await db.execute(query, {
            'min_lat': gps_lat - lat_delta,
            'max_lat': gps_lat + lat_delta,
            'min_lng': gps_lng - lng_delta,
            'max_lng': gps_lng + lng_delta,
        })

        rows = result.fetchall()

        contributions = []
        for row in rows:
            contributions.append({
                'id': row[0],
                'bin': row[1],
                'bbl': row[2],
                'address': row[3],
                'building_name': row[4],
                'year_built': row[5],
                'architect': row[6],
                'architectural_style': row[7],
                'photo_url': row[8],
                'status': row[9],
                'created_at': row[10].isoformat() if row[10] else None,
                'submitted_by': row[11],
                'notes': row[12],
            })

        return {
            'contributions': contributions,
            'count': len(contributions),
            'search_radius_meters': radius_meters
        }

    except Exception as e:
        logger.error(f"Error fetching contributions by location: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch contributions")

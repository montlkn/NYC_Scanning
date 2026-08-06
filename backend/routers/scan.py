"""
Scan confirmation + health-check endpoints.

Identification itself is fully on-device (footprint tiles + GPS + ARKit); the
client inserts its own `scans` row directly into Supabase. The backend-side
matching pipeline that used to live here (`POST /scan`, GPS+footprint
intersection + on-demand CLIP disambiguation) was removed 2026-08-06 — it had
no caller left, client or server. See services/scan_photo.py for the endpoint
that replaced it (async photo archiving after the client opts in).
"""

from fastapi import APIRouter, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from datetime import datetime, timezone
import logging

from models.database import Scan
from models.session import get_db
from pipeline import telemetry as pipeline_telemetry

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/scans/{scan_id}/confirm")
async def confirm_building_v2(
    scan_id: str,
    confirmed_bin: str = Form(..., description="BIN of confirmed building"),
    confirmation_time_ms: int = Form(None, description="Time taken to confirm (ms)"),
    user_id: str = Form(None, description="User ID for tracking"),
    verification_method: str = Form(
        "photo_banner",
        description="How the confirmation happened: map_picker | list_picker | photo_banner | auto_confirm",
    ),
    db: AsyncSession = Depends(get_db)
):
    """
    Confirm which building the user scanned.

    This:
    1. Updates scan record with confirmation
    2. If confirmed BIN was in top 3, stores user photo for future CLIP matching
    3. Tracks accuracy for analytics
    """
    try:
        logger.info(f"[{scan_id}] V2 confirmation: BIN {confirmed_bin}")

        # Fetch scan record
        scan = await db.get(Scan, scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        # Check if confirmed BIN was in top 3
        was_in_top_3 = (
            scan.candidate_bins and
            confirmed_bin in scan.candidate_bins[:3]
        )

        was_correct = scan.top_match_bin == confirmed_bin

        # Update scan record. verification_method is captured as gold-quality
        # signal for the flywheel — map_picker rows are user-tap-accurate ground
        # truth and seed the per-NYC fine-tune dataset. Tolerates the column
        # not existing yet (during the rollout window before the ALTER TABLE).
        update_values = {
            "confirmed_bin": confirmed_bin,
            "confirmed_at": datetime.now(timezone.utc),
            "confirmation_time_ms": confirmation_time_ms,
            "was_correct": was_correct,
            "verification_method": verification_method,
        }
        try:
            await db.execute(
                update(Scan).where(Scan.id == scan_id).values(**update_values)
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            if "verification_method" in str(e):
                update_values.pop("verification_method", None)
                await db.execute(
                    update(Scan).where(Scan.id == scan_id).values(**update_values)
                )
                await db.commit()
                logger.warning(
                    f"[{scan_id}] scans.verification_method column missing — "
                    "wrote confirmation without it. Run the Phase 7 ALTER TABLE."
                )
            else:
                raise

        # Track confirmation + pipeline telemetry
        pipeline_telemetry.log_confirmation(
            scan_id=scan_id,
            top3_bins=list(scan.candidate_bins[:3]) if scan.candidate_bins else [],
            confirmed_bin=confirmed_bin,
        )

        # Calculate rewards
        if was_in_top_3:
            rewards = {'xp': 10, 'message': 'Photo contribution accepted! +10 XP'}
        elif was_correct:
            rewards = {'xp': 5, 'message': 'Confirmed! +5 XP'}
        else:
            rewards = {'xp': 2, 'message': 'Feedback recorded! +2 XP'}

        return {
            'status': 'confirmed',
            'scan_id': scan_id,
            'confirmed_bin': confirmed_bin,
            'was_in_top_3': was_in_top_3,
            'was_correct': was_correct,
            'embedding_generated': False,
            'rewards': rewards
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{scan_id}] Confirmation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to confirm scan")


@router.get("/scan/health")
async def scan_health_check(db: AsyncSession = Depends(get_db)):
    """
    Health check for V2 scan system.

    Verifies:
    - Database connection
    - building_footprints table exists and has data
    - PostGIS functions are available
    """
    try:
        # Check footprints table
        from sqlalchemy import text

        result = await db.execute(
            text("SELECT COUNT(*) FROM building_footprints")
        )
        footprint_count = result.scalar()

        # Check PostGIS function
        result = await db.execute(
            text("""
                SELECT COUNT(*) FROM find_buildings_in_cone(
                    40.7128, -74.0060, 45, 100, 60, 5
                )
            """)
        )
        test_count = result.scalar()

        return {
            'status': 'healthy',
            'version': 'v2',
            'footprints_loaded': footprint_count,
            'test_query_results': test_count,
            'postgis_working': test_count is not None
        }

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                'status': 'unhealthy',
                'error': str(e),
                'version': 'v2',
                'footprints_loaded': 0
            }
        )

"""
Stamps and Achievements API Endpoints
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from models.session import get_db
from services import stamps

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/users/{user_id}/stamps")
async def get_user_stamps(
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all stamps for a user.

    Returns:
        - stamps: List of all stamps earned
        - stats: Achievement statistics
    """
    try:
        result = await stamps.get_user_stamps(db, user_id)
        return result

    except Exception as e:
        logger.error(f"Failed to get user stamps: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve stamps")


@router.get("/users/{user_id}/achievements")
async def get_user_achievements(
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get user achievement stats.

    Returns:
        - total_xp: Total XP earned
        - total_scans: Total scans performed
        - total_confirmations: Total confirmations
        - total_pioneer_contributions: Pioneer contributions
        - stamps: Stamp counts by type
    """
    try:
        result = await stamps.get_user_stamps(db, user_id)
        return result['stats']

    except Exception as e:
        logger.error(f"Failed to get user achievements: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve achievements")


@router.get("/leaderboard")
async def get_stamps_leaderboard(
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """
    Get stamps leaderboard.

    Query params:
        - limit: Number of users to return (default 20, max 100)

    Returns list of top users by stamp count.
    """
    try:
        limit = min(limit, 100)  # Cap at 100
        leaderboard = await stamps.get_leaderboard(db, limit)
        return {'leaderboard': leaderboard}

    except Exception as e:
        logger.error(f"Failed to get leaderboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve leaderboard")


@router.get("/stamp-types")
async def get_stamp_types():
    """
    Get all available stamp types and their descriptions.

    Returns stamp catalog with names, icons, descriptions, and XP values.
    """
    return {'stamp_types': stamps.STAMP_TYPES}

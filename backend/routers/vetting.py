"""
Vetting API Endpoints - Community verification of contributions
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import logging

from models.session import get_db
from services import vetting

logger = logging.getLogger(__name__)
router = APIRouter()


class VerifyRequest(BaseModel):
    user_id: str
    verification_type: str  # 'verified' or 'disputed'


@router.post("/contributions/{contribution_id}/verify")
async def verify_contribution(
    contribution_id: int,
    request: VerifyRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    User verifies or disputes a building contribution.

    Body:
        - user_id: User performing verification
        - verification_type: 'verified' or 'disputed'

    Returns:
        - success: Boolean
        - verified_count: Total users who verified
        - disputed_count: Total users who disputed
        - reliability_score: Score from 0-1
        - verification_status: 'highly_verified', 'verified', 'partially_verified', 'unverified'
        - xp_earned: XP awarded for this verification
    """
    try:
        result = await vetting.verify_contribution(
            db=db,
            contribution_id=contribution_id,
            user_id=request.user_id,
            verification_type=request.verification_type
        )

        if not result['success']:
            error = result.get('error', 'unknown_error')
            if error == 'verification_failed':
                raise HTTPException(
                    status_code=400,
                    detail="Cannot verify own contribution or contribution not found"
                )
            raise HTTPException(status_code=400, detail=error)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify contribution: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to verify contribution")


@router.get("/contributions/{contribution_id}/verifications")
async def get_contribution_verifications(
    contribution_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get verification details for a contribution.

    Returns:
        - verified_count: Number of users who verified
        - disputed_count: Number of users who disputed
        - reliability_score: Score from 0-1
        - verification_status: Status label
        - verifiers: List of users who verified/disputed
    """
    try:
        result = await vetting.get_contribution_verifications(db, contribution_id)

        if not result['success']:
            raise HTTPException(
                status_code=404,
                detail=result.get('error', 'Contribution not found')
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get verifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve verifications")


@router.get("/users/{user_id}/verifications")
async def get_user_verifications(
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all verifications performed by a user.

    Returns list of contributions user has verified/disputed.
    """
    try:
        result = await vetting.get_user_verifications(db, user_id)
        return result

    except Exception as e:
        logger.error(f"Failed to get user verifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve verifications")


@router.get("/buildings/{building_bin}/contributions")
async def get_building_contributions(
    building_bin: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all community contributions for a building.

    Returns list of contributions with verification stats.
    """
    try:
        result = await vetting.get_building_contributions(db, building_bin)
        return result

    except Exception as e:
        logger.error(f"Failed to get building contributions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve contributions")


@router.get("/contributions/{contribution_id}/badge")
async def get_verification_badge(
    contribution_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get badge/pill configuration for displaying verification status in UI.

    Returns:
        - badge_text: Text to display (e.g., "✓ 5 users")
        - badge_color: Hex color for badge
        - badge_icon: Icon to display
        - description: Tooltip text
        - verified_count: Number of verifications
        - reliability_score: Score from 0-1
    """
    try:
        result = await vetting.get_contribution_verifications(db, contribution_id)

        if not result['success']:
            # Return default unverified badge
            return vetting.get_verification_badge_config(0, 0.0)

        badge_config = vetting.get_verification_badge_config(
            result['verified_count'],
            result['reliability_score']
        )

        return {
            **badge_config,
            'verified_count': result['verified_count'],
            'disputed_count': result['disputed_count'],
            'reliability_score': result['reliability_score']
        }

    except Exception as e:
        logger.error(f"Failed to get verification badge: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get badge configuration")


class EditSuggestionRequest(BaseModel):
    user_id: str
    suggested_changes: dict
    reason: str
    source_url: str = None


class VoteRequest(BaseModel):
    user_id: str
    vote_type: str  # 'for' or 'against'


@router.post("/contributions/{contribution_id}/suggest-edit")
async def suggest_edit(
    contribution_id: int,
    request: EditSuggestionRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Propose an edit to a contribution (alternative to disputing).

    Body:
        - user_id: User proposing edit
        - suggested_changes: Dict with fields to change
        - reason: Explanation for the edit
        - source_url: Optional source citation

    Returns:
        - success: Boolean
        - suggestion_id: ID of created suggestion
        - xp_earned: XP awarded (+3 XP)
    """
    try:
        result = await vetting.propose_edit_suggestion(
            db=db,
            contribution_id=contribution_id,
            user_id=request.user_id,
            suggested_changes=request.suggested_changes,
            reason=request.reason,
            source_url=request.source_url
        )

        if not result['success']:
            raise HTTPException(
                status_code=404,
                detail=result.get('error', 'Contribution not found')
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to propose edit: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to propose edit")


@router.get("/contributions/{contribution_id}/edit-suggestions")
async def get_edit_suggestions(
    contribution_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get pending edit suggestions for a contribution.

    Returns list of suggested edits with vote counts.
    """
    try:
        result = await vetting.get_pending_edit_suggestions(db, contribution_id)
        return result

    except Exception as e:
        logger.error(f"Failed to get edit suggestions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve edit suggestions")


@router.post("/edit-suggestions/{suggestion_id}/vote")
async def vote_on_edit(
    suggestion_id: int,
    request: VoteRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Vote on an edit suggestion.

    Body:
        - user_id: User voting
        - vote_type: 'for' or 'against'

    Returns:
        - success: Boolean
        - votes_for: Total votes for
        - votes_against: Total votes against
        - auto_accepted: Whether edit was auto-accepted (3+ votes, 2:1 ratio)
        - xp_earned: XP awarded (+2 XP for 'for', +1 XP for 'against')
    """
    try:
        result = await vetting.vote_on_edit_suggestion(
            db=db,
            suggestion_id=suggestion_id,
            user_id=request.user_id,
            vote_type=request.vote_type
        )

        if not result['success']:
            raise HTTPException(
                status_code=400,
                detail=result.get('error', 'Invalid vote')
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to vote on edit: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to vote on edit")



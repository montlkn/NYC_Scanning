"""
Vetting Service - User verification of building contributions

Allows users to verify or dispute building information contributed by others,
creating a Wikipedia-style reliability system.
"""

import logging
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)


# XP rewards for vetting
VETTING_XP = {
    'verify': 5,      # +5 XP for verifying a contribution
    'dispute': 3,     # +3 XP for disputing (requires explanation)
    'milestone_10': 25,    # Bonus for 10 verifications
    'milestone_50': 100,   # Bonus for 50 verifications
}


async def verify_contribution(
    db: AsyncSession,
    contribution_id: int,
    user_id: str,
    verification_type: str  # 'verified' or 'disputed'
) -> Dict:
    """
    User verifies or disputes a building contribution.

    Args:
        db: Database session
        contribution_id: ID of contribution to verify
        user_id: User performing verification
        verification_type: 'verified' or 'disputed'

    Returns:
        Dict with verification result and updated stats
    """
    if verification_type not in ['verified', 'disputed']:
        return {'success': False, 'error': 'invalid_verification_type'}

    xp_reward = VETTING_XP['verify'] if verification_type == 'verified' else VETTING_XP['dispute']

    try:
        # Call database function
        query = text("""
            SELECT * FROM verify_contribution(
                :contribution_id,
                :user_id,
                :verification_type,
                :xp_reward
            )
        """)

        result = await db.execute(query, {
            'contribution_id': contribution_id,
            'user_id': user_id,
            'verification_type': verification_type,
            'xp_reward': xp_reward
        })

        row = result.fetchone()
        success, verified_count, disputed_count, reliability_score, is_new = row

        if not success:
            return {'success': False, 'error': 'verification_failed'}

        # Update user achievements
        verification_delta = 1 if verification_type == 'verified' else 0
        dispute_delta = 1 if verification_type == 'disputed' else 0

        await db.execute(
            text("""
                SELECT update_user_achievements(
                    :user_id, 0, 0, 0, 0, :verification_delta, :dispute_delta
                )
            """),
            {
                'user_id': user_id,
                'verification_delta': verification_delta,
                'dispute_delta': dispute_delta
            }
        )

        # Check for milestones
        milestone_stamps = await check_vetting_milestones(db, user_id)

        await db.commit()

        logger.info(
            f"Contribution {contribution_id} {verification_type} by user {user_id}. "
            f"Score: {reliability_score:.2f}, Verified: {verified_count}, Disputed: {disputed_count}"
        )

        return {
            'success': True,
            'is_new': is_new,
            'verified_count': verified_count,
            'disputed_count': disputed_count,
            'reliability_score': reliability_score,
            'verification_status': get_verification_status(reliability_score),
            'xp_earned': xp_reward if is_new else 0,
            'milestone_stamps': milestone_stamps
        }

    except Exception as e:
        logger.error(f"Failed to verify contribution: {e}", exc_info=True)
        await db.rollback()
        return {'success': False, 'error': str(e)}


async def get_contribution_verifications(
    db: AsyncSession,
    contribution_id: int
) -> Dict:
    """
    Get verification details for a contribution.

    Returns:
        Dict with verified_count, disputed_count, reliability_score, verifiers list
    """
    try:
        # Get contribution stats
        stats_query = text("""
            SELECT
                verified_count,
                disputed_count,
                reliability_score,
                last_verified_at
            FROM building_contributions
            WHERE id = :contribution_id
        """)

        result = await db.execute(stats_query, {'contribution_id': contribution_id})
        stats_row = result.fetchone()

        if not stats_row:
            return {'success': False, 'error': 'contribution_not_found'}

        verified_count, disputed_count, reliability_score, last_verified_at = stats_row

        # Get list of verifiers
        verifiers_query = text("""
            SELECT
                user_id,
                verification_type,
                verified_at
            FROM contribution_verifications
            WHERE contribution_id = :contribution_id
            ORDER BY verified_at DESC
        """)

        result = await db.execute(verifiers_query, {'contribution_id': contribution_id})
        verifiers_rows = result.fetchall()

        verifiers = [
            {
                'user_id': row[0],
                'verification_type': row[1],
                'verified_at': row[2].isoformat() if row[2] else None
            }
            for row in verifiers_rows
        ]

        return {
            'success': True,
            'verified_count': verified_count or 0,
            'disputed_count': disputed_count or 0,
            'reliability_score': reliability_score or 0.0,
            'verification_status': get_verification_status(reliability_score or 0.0),
            'last_verified_at': last_verified_at.isoformat() if last_verified_at else None,
            'verifiers': verifiers
        }

    except Exception as e:
        logger.error(f"Failed to get contribution verifications: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


async def get_user_verifications(
    db: AsyncSession,
    user_id: str
) -> Dict:
    """
    Get all verifications performed by a user.
    """
    try:
        query = text("""
            SELECT
                cv.contribution_id,
                cv.verification_type,
                cv.verified_at,
                bc.confirmed_bin,
                bc.address
            FROM contribution_verifications cv
            JOIN building_contributions bc ON cv.contribution_id = bc.id
            WHERE cv.user_id = :user_id
            ORDER BY cv.verified_at DESC
            LIMIT 50
        """)

        result = await db.execute(query, {'user_id': user_id})
        rows = result.fetchall()

        verifications = [
            {
                'contribution_id': row[0],
                'verification_type': row[1],
                'verified_at': row[2].isoformat() if row[2] else None,
                'building_bin': row[3],
                'address': row[4]
            }
            for row in rows
        ]

        return {
            'success': True,
            'verifications': verifications,
            'count': len(verifications)
        }

    except Exception as e:
        logger.error(f"Failed to get user verifications: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


async def get_building_contributions(
    db: AsyncSession,
    building_bin: str
) -> Dict:
    """
    Get all community contributions for a building.

    Returns list of contributions with verification stats and materials.
    """
    try:
        query = text("""
            SELECT
                id,
                user_id,
                address,
                architect,
                year_built,
                style,
                notes,
                mat_prim,
                mat_secondary,
                mat_tertiary,
                source_url,
                source_type,
                source_description,
                verified_count,
                disputed_count,
                reliability_score,
                created_at
            FROM building_contributions
            WHERE confirmed_bin = :building_bin
            ORDER BY created_at DESC
        """)

        result = await db.execute(query, {'building_bin': building_bin})
        rows = result.fetchall()

        contributions = []
        for row in rows:
            contributions.append({
                'id': row[0],
                'user_id': row[1],
                'address': row[2],
                'architect': row[3],
                'year_built': row[4],
                'style': row[5],
                'notes': row[6],
                'mat_prim': row[7],
                'mat_secondary': row[8],
                'mat_tertiary': row[9],
                'source_url': row[10],
                'source_type': row[11],
                'source_description': row[12],
                'verified_count': row[13] or 0,
                'disputed_count': row[14] or 0,
                'reliability_score': row[15] or 0.0,
                'created_at': row[16].isoformat() if row[16] else None
            })

        return {
            'success': True,
            'contributions': contributions,
            'count': len(contributions)
        }

    except Exception as e:
        logger.error(f"Failed to get building contributions: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


async def check_vetting_milestones(
    db: AsyncSession,
    user_id: str
) -> List[Dict]:
    """
    Check if user has reached vetting milestones.

    Returns list of milestone stamps to award.
    """
    try:
        # Import here to avoid circular dependency
        from services import stamps

        query = text("""
            SELECT total_verifications
            FROM user_achievements
            WHERE user_id = :user_id
        """)

        result = await db.execute(query, {'user_id': user_id})
        row = result.fetchone()

        if not row:
            return []

        total_verifications = row[0]
        milestone_stamps = []

        # Fact Checker: 10 verifications
        if total_verifications == 10:
            stamp = await stamps.award_stamp(
                db, user_id, 'fact_checker',
                metadata={'verifications': 10}
            )
            if stamp['awarded'] and stamp['is_new']:
                milestone_stamps.append(stamp)
                # Award bonus XP
                await db.execute(
                    text("SELECT update_user_achievements(:user_id, :xp, 0, 0, 0, 0, 0)"),
                    {'user_id': user_id, 'xp': VETTING_XP['milestone_10']}
                )
                logger.info(f"User {user_id} earned Fact Checker milestone!")

        # Truth Seeker: 50 verifications
        if total_verifications == 50:
            stamp = await stamps.award_stamp(
                db, user_id, 'truth_seeker',
                metadata={'verifications': 50}
            )
            if stamp['awarded'] and stamp['is_new']:
                milestone_stamps.append(stamp)
                # Award bonus XP
                await db.execute(
                    text("SELECT update_user_achievements(:user_id, :xp, 0, 0, 0, 0, 0)"),
                    {'user_id': user_id, 'xp': VETTING_XP['milestone_50']}
                )
                logger.info(f"User {user_id} earned Truth Seeker milestone!")

        return milestone_stamps

    except Exception as e:
        logger.error(f"Failed to check vetting milestones: {e}", exc_info=True)
        return []


def get_verification_status(reliability_score: float) -> str:
    """
    Convert reliability score to status label.

    Args:
        reliability_score: Score from 0.0 to 1.0

    Returns:
        'highly_verified', 'verified', 'partially_verified', or 'unverified'
    """
    if reliability_score >= 0.9:
        return 'highly_verified'
    elif reliability_score >= 0.7:
        return 'verified'
    elif reliability_score >= 0.5:
        return 'partially_verified'
    else:
        return 'unverified'


def get_verification_badge_config(verified_count: int, reliability_score: float) -> Dict:
    """
    Get badge/pill configuration for UI display.

    Args:
        verified_count: Number of users who verified
        reliability_score: Reliability score (0-1)

    Returns:
        Dict with badge_text, badge_color, badge_icon
    """
    status = get_verification_status(reliability_score)

    configs = {
        'highly_verified': {
            'text': f'✓ {verified_count} users',
            'color': '#10b981',  # green
            'icon': '✓✓',
            'description': 'Highly verified by community'
        },
        'verified': {
            'text': f'✓ {verified_count} users',
            'color': '#3b82f6',  # blue
            'icon': '✓',
            'description': 'Verified by community'
        },
        'partially_verified': {
            'text': f'⚠ {verified_count} users',
            'color': '#f59e0b',  # amber
            'icon': '⚠',
            'description': 'Partially verified'
        },
        'unverified': {
            'text': f'{verified_count} user' if verified_count == 1 else f'{verified_count} users',
            'color': '#6b7280',  # gray
            'icon': '?',
            'description': 'Unverified contribution'
        }
    }

    return configs.get(status, configs['unverified'])


async def propose_edit_suggestion(
    db: AsyncSession,
    contribution_id: int,
    user_id: str,
    suggested_changes: Dict,
    reason: str,
    source_url: Optional[str] = None
) -> Dict:
    """
    User proposes an edit to a contribution (alternative to disputing).

    Args:
        db: Database session
        contribution_id: ID of contribution to edit
        user_id: User proposing edit
        suggested_changes: Dict with fields to change
        reason: Explanation for the edit
        source_url: Optional source citation

    Returns:
        Dict with suggestion_id and success status
    """
    try:
        # Import json for JSONB handling
        import json

        query = text("""
            SELECT * FROM propose_edit_suggestion(
                :contribution_id,
                :user_id,
                :suggested_changes::jsonb,
                :reason
            )
        """)

        result = await db.execute(query, {
            'contribution_id': contribution_id,
            'user_id': user_id,
            'suggested_changes': json.dumps(suggested_changes),
            'reason': reason
        })

        row = result.fetchone()
        suggestion_id, success = row

        if not success:
            return {'success': False, 'error': 'contribution_not_found'}

        # Update user achievements
        await db.execute(
            text("""
                UPDATE user_achievements
                SET total_edit_suggestions = total_edit_suggestions + 1
                WHERE user_id = :user_id
            """),
            {'user_id': user_id}
        )

        await db.commit()

        logger.info(f"Edit suggestion {suggestion_id} proposed by user {user_id} for contribution {contribution_id}")

        return {
            'success': True,
            'suggestion_id': suggestion_id,
            'xp_earned': 3,  # Small XP for proposing edits
        }

    except Exception as e:
        logger.error(f"Failed to propose edit: {e}", exc_info=True)
        await db.rollback()
        return {'success': False, 'error': str(e)}


async def vote_on_edit_suggestion(
    db: AsyncSession,
    suggestion_id: int,
    user_id: str,
    vote_type: str  # 'for' or 'against'
) -> Dict:
    """
    User votes on an edit suggestion.

    Args:
        db: Database session
        suggestion_id: ID of edit suggestion
        user_id: User voting
        vote_type: 'for' or 'against'

    Returns:
        Dict with vote counts and auto-accept status
    """
    if vote_type not in ['for', 'against']:
        return {'success': False, 'error': 'invalid_vote_type'}

    try:
        query = text("""
            SELECT * FROM vote_on_edit_suggestion(
                :suggestion_id,
                :user_id,
                :vote_type
            )
        """)

        result = await db.execute(query, {
            'suggestion_id': suggestion_id,
            'user_id': user_id,
            'vote_type': vote_type
        })

        row = result.fetchone()
        votes_for, votes_against, auto_accepted = row

        await db.commit()

        logger.info(
            f"Vote on edit suggestion {suggestion_id}: {vote_type} by {user_id}. "
            f"For: {votes_for}, Against: {votes_against}, Auto-accepted: {auto_accepted}"
        )

        return {
            'success': True,
            'votes_for': votes_for,
            'votes_against': votes_against,
            'auto_accepted': auto_accepted,
            'xp_earned': 2 if vote_type == 'for' else 1
        }

    except Exception as e:
        logger.error(f"Failed to vote on edit: {e}", exc_info=True)
        await db.rollback()
        return {'success': False, 'error': str(e)}


async def get_pending_edit_suggestions(
    db: AsyncSession,
    contribution_id: int
) -> Dict:
    """
    Get pending edit suggestions for a contribution.
    """
    try:
        query = text("""
            SELECT
                id,
                user_id,
                suggested_address,
                suggested_architect,
                suggested_year_built,
                suggested_style,
                suggested_notes,
                suggested_mat_prim,
                suggested_mat_secondary,
                suggested_mat_tertiary,
                reason,
                votes_for,
                votes_against,
                created_at
            FROM edit_suggestions
            WHERE contribution_id = :contribution_id
              AND status = 'pending'
            ORDER BY votes_for DESC, created_at DESC
        """)

        result = await db.execute(query, {'contribution_id': contribution_id})
        rows = result.fetchall()

        suggestions = []
        for row in rows:
            suggestions.append({
                'id': row[0],
                'user_id': row[1],
                'suggested_changes': {
                    'address': row[2],
                    'architect': row[3],
                    'year_built': row[4],
                    'style': row[5],
                    'notes': row[6],
                    'mat_prim': row[7],
                    'mat_secondary': row[8],
                    'mat_tertiary': row[9],
                },
                'reason': row[10],
                'votes_for': row[11],
                'votes_against': row[12],
                'created_at': row[13].isoformat() if row[13] else None
            })

        return {
            'success': True,
            'suggestions': suggestions,
            'count': len(suggestions)
        }

    except Exception as e:
        logger.error(f"Failed to get edit suggestions: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}



"""
Stamps and Achievements Service

Manages user stamps, contributions, and achievement tracking.
"""

import logging
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime

logger = logging.getLogger(__name__)


# Stamp definitions
STAMP_TYPES = {
    'pioneer': {
        'name': 'Pioneer',
        'icon': '🏆',
        'description': 'Verified a challenging building',
        'xp': 25
    },
    'data_validator': {
        'name': 'Data Validator',
        'icon': '📍',
        'description': 'Contributed building information',
        'xp': 0  # XP varies based on fields
    },
    'master_validator': {
        'name': 'Master Validator',
        'icon': '⭐',
        'description': 'Earned 10 Data Validator stamps',
        'xp': 50
    },
    'database_legend': {
        'name': 'Database Legend',
        'icon': '👑',
        'description': 'Earned 25 Data Validator stamps',
        'xp': 100
    },
    'fact_checker': {
        'name': 'Fact Checker',
        'icon': '✓',
        'description': 'Verified 10 contributions',
        'xp': 25
    },
    'truth_seeker': {
        'name': 'Truth Seeker',
        'icon': '🔍',
        'description': 'Verified 50 contributions',
        'xp': 100
    }
}


async def award_stamp(
    db: AsyncSession,
    user_id: str,
    stamp_type: str,
    scan_id: Optional[str] = None,
    metadata: Optional[Dict] = None
) -> Dict:
    """
    Award a stamp to a user.

    Args:
        db: Database session
        user_id: User ID
        stamp_type: Type of stamp ('pioneer', 'data_validator', etc.)
        scan_id: Optional scan ID that earned the stamp
        metadata: Optional additional data

    Returns:
        Dict with stamp info and whether it's new
    """
    if stamp_type not in STAMP_TYPES:
        logger.error(f"Invalid stamp type: {stamp_type}")
        return {'awarded': False, 'error': 'invalid_stamp_type'}

    stamp_def = STAMP_TYPES[stamp_type]

    try:
        # Call database function to award stamp
        query = text("""
            SELECT * FROM award_stamp(
                :user_id,
                :stamp_type,
                :stamp_name,
                :stamp_icon,
                :scan_id,
                :metadata
            )
        """)

        result = await db.execute(query, {
            'user_id': user_id,
            'stamp_type': stamp_type,
            'stamp_name': stamp_def['name'],
            'stamp_icon': stamp_def['icon'],
            'scan_id': scan_id,
            'metadata': metadata or {}
        })

        row = result.fetchone()
        stamp_id, is_new = row

        await db.commit()

        logger.info(f"Stamp awarded: user={user_id}, type={stamp_type}, new={is_new}")

        return {
            'awarded': True,
            'stamp_id': stamp_id,
            'is_new': is_new,
            'stamp_type': stamp_type,
            'stamp_name': stamp_def['name'],
            'stamp_icon': stamp_def['icon'],
            'description': stamp_def['description']
        }

    except Exception as e:
        logger.error(f"Failed to award stamp: {e}", exc_info=True)
        await db.rollback()
        return {'awarded': False, 'error': str(e)}


async def record_contribution(
    db: AsyncSession,
    scan_id: str,
    user_id: str,
    confirmed_bin: str,
    contribution_data: Dict,
    was_in_top_3: bool
) -> Dict:
    """
    Record a building contribution and calculate rewards.

    Args:
        db: Database session
        scan_id: Scan ID
        user_id: User ID
        confirmed_bin: Confirmed building BIN
        contribution_data: Dict with fields like address, architect, year_built, etc.
        was_in_top_3: Whether confirmed BIN was in top 3 matches

    Returns:
        Dict with contribution record and rewards
    """
    try:
        # Extract contribution fields
        address = contribution_data.get('address', '').strip()
        architect = contribution_data.get('architect', '').strip()
        year_built = contribution_data.get('year_built')
        style = contribution_data.get('style', '').strip()
        notes = contribution_data.get('notes', '').strip()
        mat_prim = contribution_data.get('mat_prim', '').strip()
        mat_secondary = contribution_data.get('mat_secondary', '').strip()
        mat_tertiary = contribution_data.get('mat_tertiary', '').strip()

        # Calculate contribution type and XP
        base_fields_provided = sum([
            bool(address and len(address) > 5),
            bool(architect and len(architect) > 2),
            bool(year_built and 1800 <= year_built <= 2030),
            bool(style and len(style) > 2),
            bool(notes and len(notes) > 10)
        ])

        # Count materials fields (each material = 5 XP)
        materials_provided = sum([
            bool(mat_prim and len(mat_prim) > 2),
            bool(mat_secondary and len(mat_secondary) > 2),
            bool(mat_tertiary and len(mat_tertiary) > 2)
        ])

        fields_provided = base_fields_provided + materials_provided

        if fields_provided == 0:
            contribution_type = 'none'
            xp = 0
        elif fields_provided == 1 and address:
            contribution_type = 'address_only'
            xp = 10
        elif fields_provided <= 2:
            contribution_type = 'partial'
            xp = 15
        else:
            contribution_type = 'full'
            xp = 30

        # Add XP bonus for materials (5 XP per material)
        materials_xp = materials_provided * 5
        xp += materials_xp

        # Pioneer contribution bonus (not in top 3 but provided data)
        stamps_awarded = []
        if not was_in_top_3 and contribution_type != 'none':
            xp += 15  # Pioneer bonus
            stamps_awarded.append('pioneer')
            logger.info(f"Pioneer contribution detected: {contribution_type}, {fields_provided} fields")

        # Always award Data Validator stamp if any contribution
        if contribution_type != 'none':
            stamps_awarded.append('data_validator')

        # Insert contribution record
        insert_query = text("""
            INSERT INTO building_contributions (
                scan_id, user_id, confirmed_bin,
                address, architect, year_built, style, notes,
                mat_prim, mat_secondary, mat_tertiary,
                contribution_type, was_in_top_3, xp_awarded, stamps_awarded
            ) VALUES (
                :scan_id, :user_id, :confirmed_bin,
                :address, :architect, :year_built, :style, :notes,
                :mat_prim, :mat_secondary, :mat_tertiary,
                :contribution_type, :was_in_top_3, :xp_awarded, :stamps_awarded
            )
            RETURNING id
        """)

        result = await db.execute(insert_query, {
            'scan_id': scan_id,
            'user_id': user_id,
            'confirmed_bin': confirmed_bin,
            'address': address or None,
            'architect': architect or None,
            'year_built': year_built,
            'style': style or None,
            'notes': notes or None,
            'mat_prim': mat_prim or None,
            'mat_secondary': mat_secondary or None,
            'mat_tertiary': mat_tertiary or None,
            'contribution_type': contribution_type,
            'was_in_top_3': was_in_top_3,
            'xp_awarded': xp,
            'stamps_awarded': stamps_awarded
        })

        contribution_id = result.fetchone()[0]

        # Update user achievements
        pioneer_delta = 1 if not was_in_top_3 and contribution_type != 'none' else 0
        await db.execute(
            text("SELECT update_user_achievements(:user_id, :xp, 0, 1, :pioneer)"),
            {
                'user_id': user_id,
                'xp': xp,
                'pioneer': pioneer_delta
            }
        )

        # Award stamps
        awarded_stamps = []
        for stamp_type in stamps_awarded:
            stamp_result = await award_stamp(
                db, user_id, stamp_type, scan_id,
                metadata={'contribution_id': contribution_id, 'fields': fields_provided}
            )
            if stamp_result['awarded']:
                awarded_stamps.append(stamp_result)

        # Check for achievement milestones
        milestone_stamps = await check_milestones(db, user_id)
        awarded_stamps.extend(milestone_stamps)

        await db.commit()

        logger.info(f"Contribution recorded: id={contribution_id}, type={contribution_type}, xp={xp}")

        return {
            'success': True,
            'contribution_id': contribution_id,
            'contribution_type': contribution_type,
            'fields_provided': fields_provided,
            'xp_awarded': xp,
            'stamps_awarded': awarded_stamps,
            'is_pioneer': not was_in_top_3 and contribution_type != 'none'
        }

    except Exception as e:
        logger.error(f"Failed to record contribution: {e}", exc_info=True)
        await db.rollback()
        return {'success': False, 'error': str(e)}


async def update_user_achievements(
    db: AsyncSession,
    user_id: str,
    xp_delta: int = 0,
    scan_delta: int = 0,
    confirmation_delta: int = 0,
    pioneer_delta: int = 0
):
    """
    Update user achievement stats.
    """
    try:
        await db.execute(
            text("SELECT update_user_achievements(:user_id, :xp, :scan, :conf, :pioneer)"),
            {
                'user_id': user_id,
                'xp': xp_delta,
                'scan': scan_delta,
                'conf': confirmation_delta,
                'pioneer': pioneer_delta
            }
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to update achievements: {e}")
        await db.rollback()


async def check_milestones(db: AsyncSession, user_id: str) -> List[Dict]:
    """
    Check if user has reached achievement milestones.

    Returns list of milestone stamps awarded.
    """
    try:
        # Get user's stamp counts
        query = text("""
            SELECT
                pioneer_stamps,
                data_validator_stamps
            FROM user_achievements
            WHERE user_id = :user_id
        """)

        result = await db.execute(query, {'user_id': user_id})
        row = result.fetchone()

        if not row:
            return []

        pioneer_stamps, data_validator_stamps = row
        milestone_stamps = []

        # Master Validator: 10 Data Validator stamps
        if data_validator_stamps == 10:
            stamp = await award_stamp(db, user_id, 'master_validator')
            if stamp['awarded'] and stamp['is_new']:
                milestone_stamps.append(stamp)
                logger.info(f"User {user_id} earned Master Validator!")

        # Database Legend: 25 Data Validator stamps
        if data_validator_stamps == 25:
            stamp = await award_stamp(db, user_id, 'database_legend')
            if stamp['awarded'] and stamp['is_new']:
                milestone_stamps.append(stamp)
                logger.info(f"User {user_id} earned Database Legend!")

        return milestone_stamps

    except Exception as e:
        logger.error(f"Failed to check milestones: {e}", exc_info=True)
        return []


async def get_user_stamps(db: AsyncSession, user_id: str) -> Dict:
    """
    Get all stamps for a user.

    Returns:
        Dict with stamps list and stats
    """
    try:
        # Get stamps
        stamps_query = text("""
            SELECT
                stamp_type,
                stamp_name,
                stamp_icon,
                awarded_at,
                scan_id,
                metadata
            FROM user_stamps
            WHERE user_id = :user_id
            ORDER BY awarded_at DESC
        """)

        result = await db.execute(stamps_query, {'user_id': user_id})
        stamps_rows = result.fetchall()

        stamps = []
        for row in stamps_rows:
            stamps.append({
                'type': row[0],
                'name': row[1],
                'icon': row[2],
                'awarded_at': row[3].isoformat() if row[3] else None,
                'scan_id': row[4],
                'metadata': row[5]
            })

        # Get achievements stats
        stats_query = text("""
            SELECT
                total_xp,
                total_scans,
                total_confirmations,
                total_pioneer_contributions,
                pioneer_stamps,
                data_validator_stamps
            FROM user_achievements
            WHERE user_id = :user_id
        """)

        result = await db.execute(stats_query, {'user_id': user_id})
        stats_row = result.fetchone()

        if stats_row:
            stats = {
                'total_xp': stats_row[0],
                'total_scans': stats_row[1],
                'total_confirmations': stats_row[2],
                'total_pioneer_contributions': stats_row[3],
                'pioneer_stamps': stats_row[4],
                'data_validator_stamps': stats_row[5],
                'total_stamps': stats_row[4] + stats_row[5]
            }
        else:
            stats = {
                'total_xp': 0,
                'total_scans': 0,
                'total_confirmations': 0,
                'total_pioneer_contributions': 0,
                'pioneer_stamps': 0,
                'data_validator_stamps': 0,
                'total_stamps': 0
            }

        return {
            'stamps': stamps,
            'stats': stats
        }

    except Exception as e:
        logger.error(f"Failed to get user stamps: {e}", exc_info=True)
        return {'stamps': [], 'stats': {}}


async def get_leaderboard(db: AsyncSession, limit: int = 20) -> List[Dict]:
    """
    Get stamps leaderboard.

    Returns list of top users by stamp count.
    """
    try:
        query = text("""
            SELECT
                user_id,
                total_xp,
                total_pioneer_contributions,
                pioneer_stamps,
                data_validator_stamps,
                total_stamps,
                rank
            FROM stamps_leaderboard
            LIMIT :limit
        """)

        result = await db.execute(query, {'limit': limit})
        rows = result.fetchall()

        leaderboard = []
        for i, row in enumerate(rows, 1):
            leaderboard.append({
                'rank': i,
                'user_id': row[0],
                'total_xp': row[1],
                'total_pioneer_contributions': row[2],
                'pioneer_stamps': row[3],
                'data_validator_stamps': row[4],
                'total_stamps': row[5],
                'title': row[6]
            })

        return leaderboard

    except Exception as e:
        logger.error(f"Failed to get leaderboard: {e}", exc_info=True)
        return []

"""
Lore Generator — on-the-fly building description fallback.

Priority chain for a building with no storytelling:
  1. landmark_chunks table (free, fast, LPC-sourced)
  2. Wikipedia REST API (free, no key)
  3. Gemini generation from building fields (API call, cached to DB)

Usage:
    lore = await generate_building_lore(session, bin_val, building_name, ...)
"""

import os
import logging
import re
import httpx
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.footprints_session import get_footprints_db

logger = logging.getLogger(__name__)

FLOAT_PATTERN = re.compile(r'^\d+\.\d+$')


async def _get_raw_chunks(bin_val: str, building_name: Optional[str]) -> Optional[str]:
    """Query landmark_chunks on Railway for the best available raw text for this building."""
    try:
        async with get_footprints_db() as railway_db:
            if railway_db is None:
                return None

            # Try by BIN first
            result = await railway_db.execute(
                text("""
                    SELECT chunk_text FROM landmark_chunks
                    WHERE bin = :bin
                    ORDER BY chunk_index ASC
                    LIMIT 3
                """),
                {'bin': bin_val}
            )
            rows = result.fetchall()

            # Fall back to name match via source_file
            if not rows and building_name:
                result = await railway_db.execute(
                    text("""
                        SELECT chunk_text FROM landmark_chunks
                        WHERE source_file ILIKE :name
                        ORDER BY chunk_index ASC
                        LIMIT 3
                    """),
                    {'name': f'%{building_name.strip()}%'}
                )
                rows = result.fetchall()

        if not rows:
            return None

        chunks = [r[0] for r in rows if r[0]]
        combined = '\n\n'.join(chunks)
        if len(combined) > 3000:
            combined = combined[:3000].rsplit(' ', 1)[0] + '…'
        return combined

    except Exception as e:
        logger.warning(f"landmark_chunks lookup failed for BIN {bin_val}: {e}")
        return None


async def _synthesize_with_grok(
    raw_text: str,
    building_name: Optional[str],
    address: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
) -> Optional[str]:
    """Use Grok to synthesize raw LPC/Wikipedia chunks into punchy, grounded copy."""
    from services.grok import grok_text

    meta_parts = []
    if building_name and building_name != '0':
        meta_parts.append(building_name)
    if address:
        meta_parts.append(address)
    if year_built:
        meta_parts.append(str(year_built))
    if style:
        meta_parts.append(str(style))
    if architect:
        meta_parts.append(f"architect: {architect}")
    meta_line = ', '.join(meta_parts) if meta_parts else 'NYC building'

    system = (
        "You are an architecture writer for Jink, a NYC discovery app. Write "
        "in the voice of a knowledgeable friend, not a textbook. Strict "
        "grounding rules: every factual claim (year, architect, style, "
        "designation, tenant, history) must come from the source material or "
        "a verifiable web search of THIS specific building. Do not invent "
        "names, dates, or events. If the source material doesn't have "
        "anything beyond generic district designation, say so plainly using "
        "only the verified building fields — don't pad with filler.\n\n"
        "Hard bans: 'rose amid', 'quiet sentinel', 'bustling streets', "
        "'whisper of jazz', 'sentinel on a street', 'time capsule', "
        "'frozen in time', 'turn-of-the-century dreams'. No clichés. No "
        "markdown, no bullets, no headers."
    )
    user = (
        "Write 3-4 punchy sentences about this NYC building. Lead with the "
        "most specific, verifiable, building-specific fact in the source. "
        "Skip designation dates, LP numbers, and district-level language "
        "unless they are the building's defining feature. Focus on: who "
        "built it and why, what makes it architecturally specific, any "
        "verifiable history or named tenant.\n\n"
        f"Building: {meta_line}\n"
        f"Source material:\n{raw_text}"
    )
    result = await grok_text(system=system, user=user, max_tokens=300, temperature=0.3)
    if result and len(result) > 30:
        logger.info(f"Grok synthesised lore for '{building_name or address}'")
        return result.strip()
    return None


# Back-compat alias — old callers still use _synthesize_with_gemini name.
_synthesize_with_gemini = _synthesize_with_grok


async def _wikipedia_fetch(query: str) -> Optional[str]:
    """Fetch Wikipedia summary for a single query string. Returns extract or None."""
    title = query.strip().replace(' ', '_')
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers={'User-Agent': 'JinkApp/1.0'})
    if resp.status_code == 200:
        data = resp.json()
        extract = data.get('extract', '')
        if extract and len(extract) > 50:
            return extract
    return None


async def _get_lore_from_wikipedia(
    building_name: Optional[str],
    address: Optional[str] = None
) -> Optional[str]:
    """
    Fetch building description from Wikipedia.
    Tries building name first, then falls back to address (street name only).
    """
    # Try building name
    if building_name and building_name != '0':
        try:
            result = await _wikipedia_fetch(building_name)
            if result:
                logger.info(f"Wikipedia hit for name '{building_name}'")
                return result
        except Exception as e:
            logger.warning(f"Wikipedia lookup failed for name '{building_name}': {e}")

    # Fallback: try address — strip unit/apt noise, just "123 Main Street Manhattan"
    if address:
        try:
            # Use address as-is (Wikipedia often has articles for NYC street addresses)
            result = await _wikipedia_fetch(address)
            if result:
                logger.info(f"Wikipedia hit for address '{address}'")
                return result
        except Exception as e:
            logger.warning(f"Wikipedia lookup failed for address '{address}': {e}")

    return None


async def _get_lore_from_grok(
    building_name: Optional[str],
    address: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
    materials: Optional[str],
) -> Optional[str]:
    """Use Grok (with web search) to generate grounded lore from building fields.
    Last-resort fallback when no LPC chunks and no Wikipedia hit existed.
    Search is enabled so Grok can verify named tenants, recent history, etc.
    """
    from services.grok import grok_text

    fields = []
    if building_name and building_name != '0':
        fields.append(f"Name: {building_name}")
    if address:
        fields.append(f"Address: {address}")
    if year_built:
        fields.append(f"Year built: {year_built}")
    if style:
        fields.append(f"Architectural style: {style}")
    if architect:
        fields.append(f"Architect: {architect}")
    if materials:
        fields.append(f"Primary materials: {materials}")
    if not fields:
        return None

    system = (
        "You are an architecture writer for Jink, a NYC discovery app. Write "
        "in the voice of a knowledgeable friend, not a textbook. Strict "
        "grounding rules: every factual claim must come from the building "
        "fields below or a verifiable web search of THIS specific address. "
        "Do not invent names, dates, events, or details. If after searching "
        "you find nothing specific, write a concise description using only "
        "the verified fields — no filler.\n\n"
        "Hard bans: 'rose amid', 'quiet sentinel', 'bustling streets', "
        "'whisper of jazz', 'sentinel on a street', 'time capsule', "
        "'frozen in time'. No clichés. No markdown."
    )
    user = (
        "Write 3-4 punchy sentences about this NYC building. Search the web "
        "to find documented history of this exact address. Only state facts "
        "you can ground in the fields or a verified web source.\n\n"
        + '\n'.join(fields)
    )
    result = await grok_text(system=system, user=user, max_tokens=300, temperature=0.3)
    if result and len(result) > 30:
        logger.info(f"Grok generated lore for '{building_name or address}'")
        return result.strip()
    return None


# Back-compat alias — old callers still use _get_lore_from_gemini name.
_get_lore_from_gemini = _get_lore_from_grok


async def _cache_storytelling(session: AsyncSession, bin_val: str, lore: str):
    """Write generated lore back to buildings_full_merge_scanning."""
    try:
        await session.execute(
            text("""
                UPDATE buildings_full_merge_scanning
                SET storytelling = :lore
                WHERE REPLACE(bin, '.0', '') = :bin
            """),
            {'lore': lore, 'bin': bin_val}
        )
        await session.commit()
        logger.info(f"Cached lore to DB for BIN {bin_val}")
    except Exception as e:
        logger.warning(f"Failed to cache lore for BIN {bin_val}: {e}")
        await session.rollback()


async def generate_building_lore(
    session: AsyncSession,
    bin_val: str,
    building_name: Optional[str] = None,
    address: Optional[str] = None,
    year_built: Optional[str] = None,
    style: Optional[str] = None,
    architect: Optional[str] = None,
    materials: Optional[str] = None,
    cache_to_db: bool = True
) -> Optional[str]:
    """
    Generate or retrieve building lore via a three-tier fallback chain.

    Returns lore string or None if all sources fail.
    Caches result back to DB by default so next scan is instant.
    """
    # 1. Landmark chunks (LPC-sourced, free) — stored on Railway → synthesize with Grok
    raw = await _get_raw_chunks(bin_val, building_name)
    if raw:
        lore = await _synthesize_with_grok(raw, building_name, address, year_built, style, architect)
        if not lore:
            lore = raw  # fallback to raw if synthesis fails
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return lore

    # 2. Wikipedia (free, no key) — tries name then address → synthesize
    if building_name or address:
        raw = await _get_lore_from_wikipedia(building_name, address)
        if raw:
            lore = await _synthesize_with_grok(raw, building_name, address, year_built, style, architect)
            if not lore:
                lore = raw
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore)
            return lore

    # 3. Grok generation from building fields + web search (pure generation, last resort)
    lore = await _get_lore_from_grok(building_name, address, year_built, style, architect, materials)
    if lore:
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return lore

    return None

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


async def _synthesize_with_gemini(
    raw_text: str,
    building_name: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str]
) -> Optional[str]:
    """Use Gemini to synthesize raw PDF/Wikipedia chunks into punchy app-quality copy."""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — skipping synthesis")
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')

        meta_parts = []
        if building_name and building_name != '0':
            meta_parts.append(building_name)
        if year_built:
            meta_parts.append(year_built)
        if style:
            meta_parts.append(style)
        if architect:
            meta_parts.append(f"architect: {architect}")
        meta_line = ', '.join(meta_parts) if meta_parts else 'Unknown NYC building'

        prompt = (
            "You are writing for Jink, a stylish NYC architecture discovery app. "
            "Given raw source material about a building, extract the most interesting, "
            "surprising, and historically rich details. Write 3-4 punchy sentences that "
            "feel like a knowledgeable friend telling you about this place — not a textbook. "
            "Lead with the most fascinating fact. Skip boilerplate like designation dates, "
            "LP numbers, legal language. Focus on: who built it and why, what made it "
            "architecturally daring or significant, any surprising history or cultural resonance.\n\n"
            f"Building: {meta_line}\n"
            f"Source material:\n{raw_text}"
        )

        response = model.generate_content(prompt)
        result = response.text.strip() if response.text else None
        if result and len(result) > 30:
            logger.info(f"Synthesized lore for '{building_name}'")
            return result
    except Exception as e:
        logger.warning(f"Gemini synthesis failed: {e}")
    return None


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


async def _get_lore_from_gemini(
    building_name: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
    materials: Optional[str]
) -> Optional[str]:
    """Use Gemini Flash to generate 2-3 sentences of architectural lore."""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — skipping Gemini lore generation")
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')

        fields = []
        if building_name and building_name != '0':
            fields.append(f"Name: {building_name}")
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

        prompt = (
            "You are writing for Jink, a stylish NYC architecture discovery app. "
            "Write 3-4 punchy sentences about this NYC building that feel like a knowledgeable "
            "friend telling you about this place — not a textbook. Lead with the most fascinating "
            "fact. Focus on: who built it and why, what made it architecturally daring or "
            "significant, any surprising history or cultural resonance.\n\n"
            + '\n'.join(fields)
        )

        response = model.generate_content(prompt)
        text_out = response.text.strip() if response.text else None
        if text_out and len(text_out) > 30:
            logger.info(f"Gemini generated lore for '{building_name}'")
            return text_out
    except Exception as e:
        logger.warning(f"Gemini lore generation failed: {e}")
    return None


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
    # 1. Landmark chunks (LPC-sourced, free) — stored on Railway → synthesize with Gemini
    raw = await _get_raw_chunks(bin_val, building_name)
    if raw:
        lore = await _synthesize_with_gemini(raw, building_name, year_built, style, architect)
        if not lore:
            lore = raw  # fallback to raw if synthesis fails
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return lore

    # 2. Wikipedia (free, no key) — tries name then address → synthesize
    if building_name or address:
        raw = await _get_lore_from_wikipedia(building_name, address)
        if raw:
            lore = await _synthesize_with_gemini(raw, building_name, year_built, style, architect)
            if not lore:
                lore = raw
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore)
            return lore

    # 3. Gemini generation from building fields (pure generation, last resort)
    lore = await _get_lore_from_gemini(building_name, year_built, style, architect, materials)
    if lore:
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return lore

    return None

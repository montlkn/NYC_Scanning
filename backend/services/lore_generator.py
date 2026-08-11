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
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.footprints_session import get_footprints_db

logger = logging.getLogger(__name__)

FLOAT_PATTERN = re.compile(r'^\d+\.\d+$')


async def _get_block_context(session: AsyncSession, bin_val: str) -> Optional[str]:
    """Computed facts about how this building sits among its ACTUAL neighbours.

    This is lore no model and no web search can produce, because it requires the
    whole city at once: "sixty years older than anything on its block" is only
    knowable by comparing against every surrounding footprint.

    It deliberately does NOT use the precomputed `*_surprise_*` /
    `local_era_contrast` / `neighbor_count_r*` columns. Those compare against the
    curated 35k rather than the real block, and `neighbor_count_r300` holds
    exactly ONE distinct value across all 35,382 rows — so any claim derived from
    them ("older than its block") may simply be false. `building_footprints`
    carries 1.08M real NYC footprints with construction years, which is the
    honest comparison set.

    Also counts the architect's other work IN OUR CATALOGUE — phrased as such,
    since it is not a claim about their whole career.
    """
    try:
        row = (await session.execute(text("""
            WITH me AS (
              SELECT bin, centroid, construction_year, height_roof
              FROM building_footprints
              WHERE replace(bin,'.0','') = :bin AND centroid IS NOT NULL
              LIMIT 1
            )
            SELECT
              (SELECT construction_year FROM me)                     AS my_year,
              (SELECT height_roof FROM me)                           AS my_height,
              count(*)                                               AS neighbours,
              round(avg(f.construction_year)
                    FILTER (WHERE f.construction_year > 1700))       AS avg_year,
              min(f.construction_year)
                    FILTER (WHERE f.construction_year > 1700)        AS oldest,
              round(max(f.height_roof))                              AS tallest
            FROM building_footprints f, me
            WHERE f.bin <> me.bin
              AND ST_DWithin(f.centroid::geography, me.centroid::geography, 100)
        """), {"bin": bin_val})).fetchone()
    except Exception as e:
        logger.warning(f"block context failed for BIN {bin_val}: {e}")
        return None

    if not row or not row[2]:
        return None
    my_year, my_height, neighbours, avg_year, oldest, tallest = row

    bits = []
    if my_year and avg_year and neighbours >= 5:
        gap = int(my_year) - int(avg_year)
        if abs(gap) >= 20:
            bits.append(
                f"built {abs(gap)} years {'after' if gap > 0 else 'before'} the "
                f"average of its {neighbours} neighbours within 100m "
                f"(this {int(my_year)}, block average {int(avg_year)})"
            )
    if my_height and tallest and float(my_height) > float(tallest) * 1.5:
        bits.append(
            f"taller than everything within 100m "
            f"({int(float(my_height))}ft vs {int(float(tallest))}ft next tallest)"
        )
    if not bits:
        return None
    return "; ".join(bits)


async def _get_architect_catalogue_count(
    session: AsyncSession, architect: Optional[str]
) -> Optional[int]:
    """How many other buildings in OUR catalogue share this architect."""
    if not architect or architect.strip().lower() in ("", "not determined", "unknown"):
        return None
    try:
        row = (await session.execute(text("""
            SELECT count(*) FROM buildings_full_merge_scanning
            WHERE btrim(lower(architect)) = btrim(lower(:a))
        """), {"a": architect})).fetchone()
    except Exception:
        return None
    return int(row[0]) if row and row[0] and int(row[0]) > 1 else None


async def _get_raw_chunks_detailed(
    bin_val: str, building_name: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Same lookup as `_get_raw_chunks`, but also reports HOW specific the text
    is and which report it came from: (text, specificity, source_file).

    `specificity` is 'building' for per-building entries extracted from a
    designation report, or None for the district-level blurb that is shared
    across every building in a historic district. Callers need the distinction:
    district text is legitimate grounding but must never be presented as though
    it were about this building specifically, and it is the signal for whether
    the cheap tier actually earned its keep."""
    try:
        async with get_footprints_db() as railway_db:
            if railway_db is None:
                return None, None, None

            # Try by BIN first. Building-specific chunks MUST outrank
            # district-level ones: most buildings in a historic district share a
            # single generic district blurb, so ordering by chunk_index alone
            # hands every building on the block the same paragraph and the lore
            # reads identically across a whole neighbourhood.
            #
            # BINs are compared with the '.0' suffix stripped — these are stored
            # numeric-as-text, and an unstripped compare silently matches
            # nothing.
            result = await railway_db.execute(
                text("""
                    SELECT chunk_text, specificity, source_file
                    FROM landmark_chunks
                    WHERE replace(bin, '.0', '') = replace(:bin, '.0', '')
                    ORDER BY (specificity = 'building') DESC NULLS LAST,
                             chunk_index ASC
                    LIMIT 3
                """),
                {'bin': bin_val}
            )
            rows = result.fetchall()

            # Fall back to name match via source_file
            if not rows and building_name:
                result = await railway_db.execute(
                    text("""
                        SELECT chunk_text, specificity, source_file
                        FROM landmark_chunks
                        WHERE source_file ILIKE :name
                        ORDER BY (specificity = 'building') DESC NULLS LAST,
                                 chunk_index ASC
                        LIMIT 3
                    """),
                    {'name': f'%{building_name.strip()}%'}
                )
                rows = result.fetchall()

        if not rows:
            return None, None, None

        # Only mix chunks of the SAME specificity. Concatenating a building's own
        # record with the district blurb lets the model attribute district-wide
        # description to this building, which is exactly the failure the
        # specificity split exists to prevent.
        top_spec = rows[0][1]
        chosen = [r for r in rows if r[1] == top_spec]
        chunks = [r[0] for r in chosen if r[0]]
        combined = '\n\n'.join(chunks)
        if len(combined) > 3000:
            combined = combined[:3000].rsplit(' ', 1)[0] + '…'
        return combined, top_spec, chosen[0][2]

    except Exception as e:
        logger.warning(f"landmark_chunks lookup failed for BIN {bin_val}: {e}")
        return None, None, None


async def _get_raw_chunks(bin_val: str, building_name: Optional[str]) -> Optional[str]:
    """Back-compat wrapper: text only."""
    text_, _spec, _src = await _get_raw_chunks_detailed(bin_val, building_name)
    return text_


async def _synthesize_with_grok(
    raw_text: str,
    building_name: Optional[str],
    address: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
    block_context: Optional[str] = None,
    architect_count: Optional[int] = None,
) -> Optional[str]:
    """Synthesize raw LPC/Wikipedia chunks into punchy, grounded copy.

    Runs on gpt-5.6-luna when OPENAI_API_KEY is set, falling back to Grok
    otherwise. Synthesis is rewriting, not researching — the facts arrive as
    source text — so it belongs on the cheapest capable model.

    Note the Grok fallback passes `search_enabled=False`. It defaults to True,
    which meant tier-1 synthesis over already-free LPC text was still asking a
    model to run live web search, defeating the point of the cheap tier."""
    from services.grok import grok_text
    from services.openai_text import openai_text, is_configured

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
        "markdown, no bullets, no headers.\n\n"
        "The source is OCR'd from scanned reports and contains scanning "
        "errors — 'Buiiding' for 'Building', 'Centw:y' for 'Century', "
        "'AROITTECT' for 'ARCHITECT', stray punctuation inside words. Read "
        "through them and write the corrected form. Never reproduce a garbled "
        "spelling, and never quote the source verbatim. If a proper name is "
        "too corrupted to reconstruct with confidence, omit it rather than "
        "guessing at it."
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

    # Computed facts, appended separately from the source text so the model can
    # tell them apart. These are measured from the full city footprint dataset
    # and our own catalogue — they are TRUE, and no web search could produce
    # them — but they are only worth a sentence when the contrast is striking.
    extras = []
    if block_context:
        extras.append(f"How it sits on its block (measured, verified): {block_context}.")
    if architect_count:
        extras.append(
            f"This architect has {architect_count} other buildings in Jink's "
            f"catalogue (say 'in this app' or similar — it is NOT a claim about "
            f"their whole career)."
        )
    if extras:
        user += (
            "\n\nAdditional verified facts:\n" + "\n".join(extras) +
            "\nUse at most ONE of these, and only if it is genuinely surprising. "
            "Never pad with it."
        )
    result = None
    if is_configured():
        result = await openai_text(system=system, user=user, max_tokens=300)
        if result and len(result) > 30:
            logger.info(f"luna synthesised lore for '{building_name or address}'")
            return result.strip()

    # Fallback: Grok, explicitly WITHOUT search. The source text is already in
    # the prompt; a search here would bill the expensive path to restate facts
    # we already hold.
    result = await grok_text(system=system, user=user, max_tokens=300,
                             temperature=0.3, search_enabled=False)
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
        return
    # Item 4: fold the new lore into the search index immediately so the building
    # becomes searchable by its story without waiting for a full re-embed. Best
    # effort — a failure here must never break lore generation.
    await _reindex_building(session, bin_val)


async def _reindex_building(session: AsyncSession, bin_val: str):
    """Re-embed a single building into building_search_index after its lore changes.

    Reuses the batch embedder's build_text / build_snippet so the embedded prose
    stays identical to scripts/embed_buildings.py. The storytelling column is the
    LAST clause of build_text, so the freshly-cached lore is now part of the
    vector. Pure local embed ($0); upsert mirrors the script's ON CONFLICT shape.
    """
    try:
        # Pull the full source row (same columns the batch script embeds).
        from scripts.embed_buildings import (
            SOURCE_COLUMNS, build_text, build_snippet, _parse_int,
            _parse_float, _clean,
        )
        from services.text_embeddings import embed_texts
        from models.search_session import get_search_db

        result = await session.execute(
            text(f"""
                SELECT {SOURCE_COLUMNS}
                FROM buildings_full_merge_scanning
                WHERE REPLACE(bin, '.0', '') = :bin
                LIMIT 1
            """),
            {'bin': bin_val},
        )
        row = result.mappings().first()
        if not row:
            return
        row = dict(row)

        txt = build_text(row)
        if not txt:
            return
        vec = embed_texts([txt])[0]
        vec_literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        landmark = _clean(row.get("landmark"))

        async with get_search_db() as sdb:
            if sdb is None:
                return
            await sdb.execute(
                text("""
                    INSERT INTO building_search_index
                        (bin, bbl, text, snippet, embedding, year_built,
                         is_landmark, lat, lng, updated_at)
                    VALUES (:bin, :bbl, :text, :snippet, CAST(:embedding AS vector),
                            :year_built, :is_landmark, :lat, :lng, now())
                    ON CONFLICT (bin) DO UPDATE SET
                        bbl = EXCLUDED.bbl, text = EXCLUDED.text,
                        snippet = EXCLUDED.snippet, embedding = EXCLUDED.embedding,
                        year_built = EXCLUDED.year_built,
                        is_landmark = EXCLUDED.is_landmark,
                        lat = EXCLUDED.lat, lng = EXCLUDED.lng, updated_at = now()
                """),
                {
                    'bin': bin_val,
                    'bbl': (_clean(row.get("bbl")).removesuffix(".0") or None),
                    'text': txt,
                    'snippet': build_snippet(row),
                    'embedding': vec_literal,
                    'year_built': _parse_int(row.get("year_built")),
                    'is_landmark': bool(landmark and landmark != "0"),
                    'lat': _parse_float(row.get("geocoded_lat")),
                    'lng': _parse_float(row.get("geocoded_lng")),
                },
            )
            await sdb.commit()
        logger.info(f"Re-indexed BIN {bin_val} into search index after lore update")
    except Exception as e:
        logger.warning(f"Search re-index skipped for BIN {bin_val}: {e}")


@dataclass
class LoreResult:
    """Lore plus the provenance needed to render honest citations and to measure
    how often each tier actually fires.

    `tier` is the whole point. Tier 3 is the only one that costs money (agentic
    web search, measured at $0.09-0.55 per building with a long tail), so the
    tier-3 rate across real traffic is the number that decides whether a
    dedicated search provider is worth adding. Nothing else can tell us that.
    """
    text: str
    tier: str                      # landmark_chunks | wikipedia | web_search
    specificity: Optional[str]     # 'building' | None (district-level)
    source: Optional[str]          # report URL / article / None
    synthesized: bool              # False when raw source text was served as-is


async def generate_building_lore_detailed(
    session: AsyncSession,
    bin_val: str,
    building_name: Optional[str] = None,
    address: Optional[str] = None,
    year_built: Optional[str] = None,
    style: Optional[str] = None,
    architect: Optional[str] = None,
    materials: Optional[str] = None,
    cache_to_db: bool = True
) -> Optional[LoreResult]:
    """
    Generate or retrieve building lore via a three-tier fallback chain, and say
    which tier answered.

    Tier order is by cost, cheapest first: LPC designation reports (free, and
    the best-grounded source we have) → Wikipedia (free) → web search (paid,
    last resort).
    """
    # 1. Landmark chunks (LPC-sourced, free) — stored on Railway → synthesize
    raw, spec, src = await _get_raw_chunks_detailed(bin_val, building_name)
    if raw:
        block_ctx = await _get_block_context(session, bin_val)
        arch_n = await _get_architect_catalogue_count(session, architect)
        lore = await _synthesize_with_grok(raw, building_name, address, year_built,
                                           style, architect, block_ctx, arch_n)
        if lore:
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore)
            return LoreResult(text=lore, tier="landmark_chunks", specificity=spec,
                              source=src, synthesized=True)
        # Synthesis failed (missing/failing API key, timeout). Do NOT serve the
        # raw chunk: a designation report opens with hearing boilerplate — "Six
        # witnesses spoke in favor of designation. There were no speakers in
        # opposition." — in OCR'd 1981 typescript. Shipping that as a building's
        # story is worse than having none, and it CACHES, so one bad key
        # poisons the storytelling column for every building it touches.
        # Fall through to the next tier instead.
        logger.warning(
            f"synthesis failed for BIN {bin_val}; skipping raw landmark text"
        )

    # 2. Wikipedia (free, no key) — tries name then address → synthesize
    if building_name or address:
        raw = await _get_lore_from_wikipedia(building_name, address)
        if raw:
            block_ctx = await _get_block_context(session, bin_val)
            arch_n = await _get_architect_catalogue_count(session, architect)
            lore = await _synthesize_with_grok(raw, building_name, address, year_built,
                                               style, architect, block_ctx, arch_n)
            # A Wikipedia extract is at least written prose, so serving it raw is
            # tolerable where raw LPC typescript is not. It is still marked
            # unsynthesized so the cost/quality split stays visible.
            synthesized = bool(lore)
            if not lore:
                lore = raw
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore)
            return LoreResult(text=lore, tier="wikipedia", specificity="building",
                              source=None, synthesized=synthesized)

    # 3. Web search from building fields (paid, last resort)
    lore = await _get_lore_from_grok(building_name, address, year_built, style, architect, materials)
    if lore:
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return LoreResult(text=lore, tier="web_search", specificity="building",
                          source=None, synthesized=True)

    return None


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
    """Back-compat wrapper returning just the lore string."""
    res = await generate_building_lore_detailed(
        session, bin_val, building_name, address, year_built, style,
        architect, materials, cache_to_db
    )
    return res.text if res else None

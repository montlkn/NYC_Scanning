"""
Lore Generator — on-the-fly building description fallback.

Priority chain for a building with no storytelling, cheapest first. Every tier
that produces prose runs it through gpt-5.6-luna with NO tools attached; the
only tier that touches the open web is 3, where the query count is a constant
we set rather than a number a model chooses.

  1. landmark_chunks table   (free — LPC designation reports, best-grounded)
  2. Wikipedia REST API      (free, no key)
  3. Brave web search        (paid, capped at brave_search.MAX_QUERIES)
  4. fields-only description (no research; states what the DB already holds)

Tiers 3 and 4 both degrade silently when their key is missing, so `main.py`
logs the configuration state at startup. See `services/openai_text.py` for why
no tool is ever attached to the synthesis call.

Usage:
    lore = await generate_building_lore(session, bin_val, building_name, ...)
"""

import asyncio
import math
import os
import logging
import re
import httpx
from dataclasses import dataclass, field
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.footprints_session import get_footprints_db

logger = logging.getLogger(__name__)

FLOAT_PATTERN = re.compile(r'^\d+\.\d+$')


def _is_complete(text: Optional[str]) -> bool:
    """Reject a truncated generation before it can be cached.

    The old gate was `len(result) > 30`, which passed a 32-character fragment —
    "At 1,454 feet tall including its" — straight into the storytelling column,
    where it is served forever as that building's story. A cut-off generation is
    indistinguishable from a good one by length alone, so require a closing
    sentence mark as well. Cheaper to regenerate than to cache a fragment.
    """
    if not text:
        return False
    t = text.strip()
    return len(t) >= 120 and t[-1] in '.!?"”’'


_LORE_CATEGORIES: Optional[list[str]] = None


async def _get_lore_categories() -> Optional[list[str]]:
    """Distinct lore categories, read once per process from the search index.

    Read from the data rather than written here so the Brave story sweep tracks
    whatever categories actually exist — add one to the index and the sweep
    picks it up with no code change.
    """
    global _LORE_CATEGORIES
    if _LORE_CATEGORIES is not None:
        return _LORE_CATEGORIES
    try:
        from models.search_session import get_search_db
        async with get_search_db() as sdb:
            if sdb is None:
                return None
            rows = (await sdb.execute(text(
                "SELECT DISTINCT category FROM layer_search_index "
                "WHERE layer='lore' AND category IS NOT NULL"
            ))).fetchall()
        _LORE_CATEGORIES = [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.warning(f"lore category lookup failed: {e}")
        return None
    return _LORE_CATEGORIES


# Enrichment context is NICE-TO-HAVE, never load-bearing: block comparisons,
# the architect's catalogue count, nearby lore. The narrative reads fine without
# any of them, so none of them should be allowed to dominate the response.
#
# Measured 2026-08-12: a forced regeneration took 20.2s while the CLIENT does the
# same synthesis on the same model in 2.9s. The gap is not the LLM and it was not
# the citation query (overlapping that changed nothing) -- it is these lookups.
# `_get_block_context` runs ST_DWithin across 1.08M footprints and
# `_get_nearby_lore` scans the search index, both on a remote Railway database.
#
# Bounding them converts an unpredictable multi-second stall into a known
# ceiling. A timeout costs one sentence of colour; it does not cost the lore.
CONTEXT_TIMEOUT_S = 3.0


async def _bounded(coro, label: str):
    """Await `coro`, giving up after CONTEXT_TIMEOUT_S. Returns None on timeout
    or error -- every caller already treats None as "no extra context"."""
    try:
        return await asyncio.wait_for(coro, timeout=CONTEXT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"[lore] {label} exceeded {CONTEXT_TIMEOUT_S}s; continuing without it"
        )
        return None
    except Exception as e:
        logger.warning(f"[lore] {label} failed: {e}")
        return None


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


async def _get_nearby_lore(session: AsyncSession, bin_val: str,
                           radius_m: int = 150, limit: int = 3) -> Optional[str]:
    """Lore events and plaques near this building, from the indexed layers.

    We already hold 1,806 lore events and 105 plaques with coordinates and
    sources — the register the app actually wants ("This delightful East Village
    faux carriage house was built in 1891 for sculptors, not horses"). A
    designation report will never contain that, so without this the writeup is
    all fabric and no story.

    These are NEARBY, not necessarily ABOUT this building — a plaque 100m away
    belongs to a different address. The prompt says so explicitly and permits
    only a relational mention, because presenting a neighbour's history as this
    building's is precisely the mis-attribution the rest of this pipeline works
    to avoid.
    """
    try:
        row = (await session.execute(text("""
            SELECT ST_Y(centroid::geometry) AS lat, ST_X(centroid::geometry) AS lng
            FROM building_footprints
            WHERE replace(bin,'.0','') = :bin AND centroid IS NOT NULL LIMIT 1
        """), {"bin": bin_val})).fetchone()
    except Exception as e:
        logger.warning(f"nearby-lore centroid lookup failed for {bin_val}: {e}")
        return None
    if not row or row[0] is None:
        return None
    lat, lng = float(row[0]), float(row[1])

    # The search DB has NO PostGIS — `geography` does not exist there, unlike
    # the buildings DB. A ST_DWithin query fails, and because this helper
    # swallows errors it would fail SILENTLY: no nearby lore, ever, with nothing
    # in the logs to say why. So distance is plain arithmetic, which at a 150m
    # radius is well within a metre of the true value, over an index of ~1,900
    # rows where a full scan costs nothing.
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = max(m_per_deg_lat * math.cos(math.radians(lat)), 1.0)
    dlat = radius_m / m_per_deg_lat
    dlng = radius_m / m_per_deg_lng

    try:
        from models.search_session import get_search_db
        async with get_search_db() as sdb:
            if sdb is None:
                return None
            rows = (await sdb.execute(text("""
                SELECT title, snippet, layer, year,
                       sqrt(
                         power((lat - :lat) * :mlat, 2) +
                         power((lng - :lng) * :mlng, 2)
                       ) AS d
                FROM layer_search_index
                WHERE layer IN ('lore','plaque')
                  AND lat IS NOT NULL AND lng IS NOT NULL
                  AND lat BETWEEN :lat - :dlat AND :lat + :dlat
                  AND lng BETWEEN :lng - :dlng AND :lng + :dlng
                ORDER BY d ASC
                LIMIT :lim
            """), {"lat": lat, "lng": lng, "mlat": m_per_deg_lat,
                   "mlng": m_per_deg_lng, "dlat": dlat, "dlng": dlng,
                   "lim": limit})).fetchall()
    except Exception as e:
        logger.warning(f"nearby-lore lookup failed for {bin_val}: {e}")
        return None

    if not rows:
        return None
    bits = []
    for title, snippet, layer, year, dist in rows:
        label = (title or snippet or "").strip()
        if not label:
            continue
        bits.append(f"[{layer}, {int(dist)}m away] {label[:180]}")
    return "\n".join(bits) if bits else None


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


async def _synthesize(
    raw_text: str,
    building_name: Optional[str],
    address: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
    block_context: Optional[str] = None,
    architect_count: Optional[int] = None,
    nearby_lore: Optional[str] = None,
) -> Optional[str]:
    """Synthesize raw LPC/Wikipedia/Brave source text into punchy, grounded copy.

    Runs on gpt-5.6-luna. Synthesis is rewriting, not researching — the facts
    arrive as source text — so it belongs on the cheapest capable model, with
    no tools attached.

    Returns None when OPENAI_API_KEY is unset. Callers must treat that as "no
    lore" rather than substituting raw source: a designation report opens with
    hearing boilerplate in OCR'd typescript, and this result CACHES."""
    from services.openai_text import openai_text

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
        "names, dates, or events.\n\n"
        "NEVER narrate the absence of information. Do not write 'the provided "
        "source does not document', 'records are unclear', 'little is known', "
        "or any variant — shipped live as \"Beyond those building fields, the "
        "provided source does not document its original sponsor, purpose, or "
        "later tenants.\" The reader came for the building, not a report on our "
        "sourcing. If the material is thin, write two good sentences about what "
        "IS known and stop. Length is never the goal.\n\n"
        # Formatting parity with the CLIENT's narrative prompt. Both write into
        # the same cached column and the app renders both through the same
        # markdown view, so a difference here reads as the app randomly getting
        # worse. This prompt said "no markdown" while the client asks for bold
        # and italic — so the moment the backend started answering first, every
        # well-known building silently lost its formatting. That was the most
        # visible half of what looked like a total quality collapse.
        "Format with light markdown: **bold** for proper nouns (building "
        "names, people, organisations) and _italic_ for architectural terms "
        "and styles. Flowing prose only — no bullets, no headers, no lists.\n\n"
        "Hard bans: 'rose amid', 'quiet sentinel', 'bustling streets', "
        "'whisper of jazz', 'sentinel on a street', 'time capsule', "
        "'frozen in time', 'turn-of-the-century dreams'. No clichés.\n\n"
        "The source is OCR'd from scanned reports and contains scanning "
        "errors — 'Buiiding' for 'Building', 'Centw:y' for 'Century', "
        "'AROITTECT' for 'ARCHITECT', stray punctuation inside words. Read "
        "through them and write the corrected form. Never reproduce a garbled "
        "spelling, and never quote the source verbatim. If a proper name is "
        "too corrupted to reconstruct with confidence, omit it rather than "
        "guessing at it."
    )
    user = (
        "Write 4-6 sentences about this NYC building.\n\n"
        # The app renders architect / style / year in a DATA PANEL directly
        # above this prose. Restating them is the biggest waste in the budget:
        # the model opened every piece with "built in 1880-81 and designed by
        # Frederick Weber" while the reader was looking at exactly that three
        # lines higher — so the measured block context and the nearby lore,
        # which arrive last, never fit.
        "The reader can ALREADY SEE the architect, style and year in a table "
        "above your text. Do not open by restating them and do not spend a "
        "sentence listing them. Use them as raw material instead: a style name "
        "is a fact, but what that style was TRYING to do on this street is a "
        "story.\n\n"
        "Lead with whatever is most surprising or most specific — who built it "
        "and why, who lived or worked there, what happened here, what it used "
        "to be. Skip designation dates, LP numbers and district-level language "
        "unless they are the building's defining feature.\n\n"
        # A designation report is a HISTORICAL document written in the present
        # tense. The Graham Home's report says the building "now operates as the
        # Bull Shippers Plaza Motor Inn" -- true when it was written, false since
        # 2001, when it became condominiums. The model repeated it verbatim as
        # current fact. Any "now/currently/today" in the source describes the
        # year the report was filed, not the year the reader is standing there.
        "CRITICAL: the source may be a designation report written decades ago, "
        "in the present tense. Never repeat its 'now', 'currently', 'today', "
        "'is used as' or 'operates as' as though it were true today — you have "
        "no idea what the building is used for now, and stating a former use as "
        "current is the worst error you can make here. Put such facts firmly in "
        "the past ('was converted to', 'by the 1970s it housed') or leave them "
        "out.\n\n"
        "If the source genuinely holds nothing beyond the fabric, write well "
        "about the fabric — but never pad. Atmosphere standing in for fact is "
        "obvious and worthless: a sentence like 'its romantic force lies in the "
        "way the residence preserves the scale of an earlier Brooklyn' says "
        "nothing and must never be written.\n\n"
        f"Building (context you may draw on, NOT content to recite): {meta_line}\n"
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
    if nearby_lore:
        extras.append(
            "Lore recorded NEARBY (each line gives its distance). These are "
            "NOT about this building unless the address plainly matches — they "
            "belong to neighbouring sites. You may mention one only "
            "relationally ('around the corner from...', 'on the same block "
            "as...'), and must never restate it as this building's own "
            "history:\n" + nearby_lore
        )
    if extras:
        user += (
            "\n\nAdditional verified material:\n" + "\n".join(extras) +
            "\nYou may use ONE measured fact AND ONE nearby-lore mention — they "
            "are different kinds of material and do not compete. A single "
            "'ONE of these' budget made the model always pick the measured "
            "fact, so the lore never appeared at all. Take either only when it "
            "genuinely earns its sentence; never pad."
        )
    subject = building_name or address
    result = await openai_text(system=system, user=user, max_tokens=1200)
    if _is_complete(result):
        logger.info(f"luna synthesised lore for '{subject}'")
        return result.strip()

    # Truncation used to fall through to Grok at a SMALLER budget, which could
    # only truncate again — it papered over the failure rather than fixing it.
    # The real cause is that `max_output_tokens` covers reasoning tokens too, so
    # the fix is more headroom, not another model. One retry only: a second
    # truncation means the prompt is wrong, and retrying forever would bill for
    # it. Returning None is safe — the caller falls to the next tier and nothing
    # incomplete is ever cached.
    if result:
        logger.warning(
            f"luna returned truncated lore ({len(result)} chars) for "
            f"'{subject}'; retrying with a larger output budget"
        )
        result = await openai_text(system=system, user=user, max_tokens=2400)
        if _is_complete(result):
            logger.info(f"luna synthesised lore for '{subject}' on retry")
            return result.strip()
        logger.warning(f"luna still truncated for '{subject}'; giving up")
    return None


async def _wikipedia_fetch(query: str) -> Optional[tuple[str, Optional[str]]]:
    """Fetch a Wikipedia summary. Returns (extract, article_url) or None.

    The URL was previously discarded, which left every Wikipedia-tier answer
    with `source=None` and therefore NO citation at all — on what turns out to
    be a very common tier (both VIA 57 West and 56 Leonard land here). The
    response carries the canonical article link already, so the citation was
    free and simply thrown away. `canonical` is preferred over the request URL
    because Wikipedia resolves redirects: asking for "Jenga Tower" should cite
    the article it actually served.
    """
    title = query.strip().replace(' ', '_')
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers={'User-Agent': 'JinkApp/1.0'})
    if resp.status_code == 200:
        data = resp.json()
        extract = data.get('extract', '')
        if extract and len(extract) > 50:
            page_url = (
                ((data.get('content_urls') or {}).get('desktop') or {}).get('page')
                or url
            )
            return extract, page_url
    return None


async def _get_lore_from_wikipedia(
    building_name: Optional[str],
    address: Optional[str] = None
) -> Optional[tuple[str, Optional[str]]]:
    """
    Fetch building description from Wikipedia. Returns (extract, article_url).
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


async def _describe_from_fields(
    building_name: Optional[str],
    address: Optional[str],
    year_built: Optional[str],
    style: Optional[str],
    architect: Optional[str],
    materials: Optional[str],
) -> Optional[str]:
    """Last resort: describe the building from the DB fields we already hold.

    This used to be Grok with `search_enabled=True` — an AGENTIC search where the
    model chose how many queries to run. That is the path that reached 13–24
    searches on one building and once billed $0.55, and replacing it is the whole
    reason the Brave tier above exists. Restoring search here in any form would
    re-open exactly that leak, one tier lower and less visibly.

    So this tier no longer discovers anything. It states what the database
    already knows, in the app's voice. That is a real reduction in reach:
    a building with no designation report, no Wikipedia article, and no Brave
    result now gets a description rather than researched history. The trade is
    deliberate — an uncapped bill for the long tail was the thing we were
    actually paying for, and the tail is where agentic search performed worst
    (nothing to find, so it searched hardest).

    The prompt therefore forbids implying research happened. Without that, a
    model handed six fields and asked for "punchy" prose invents a tenant.
    """
    from services.openai_text import openai_text

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
        "in the voice of a knowledgeable friend, not a textbook.\n\n"
        "You have NO web access and NO sources beyond the fields given below. "
        "Every factual claim must come from those fields. Do not invent or "
        "infer names, dates, events, tenants, architects, or history — not even "
        "plausible ones. Do not imply you researched anything.\n\n"
        "If the fields are thin, write less. Two accurate sentences beat four "
        "padded ones. Describing what a style and era MEAN in general terms is "
        "allowed and encouraged; asserting anything specific that is not in the "
        "fields is not.\n\n"
        "NEVER narrate the absence of information — no 'records do not show', "
        "no 'the source does not document', no apologising for what you lack. "
        "Write what is known and stop.\n\n"
        "Format with light markdown: **bold** for proper nouns and _italic_ "
        "for architectural terms and styles. Flowing prose only.\n\n"
        "Hard bans: 'rose amid', 'quiet sentinel', 'bustling streets', "
        "'whisper of jazz', 'sentinel on a street', 'time capsule', "
        "'frozen in time'. No clichés. No markdown."
    )
    user = (
        "Write 2-4 punchy sentences about this NYC building, using ONLY the "
        "fields below.\n\n"
        + '\n'.join(fields)
    )
    result = await openai_text(system=system, user=user, max_tokens=900,
                               cache_key="jink-lore-fields")
    if _is_complete(result):
        logger.info(f"luna described '{building_name or address}' from fields")
        return result.strip()
    return None


async def _cache_storytelling(session: AsyncSession, bin_val: str, lore: str,
                              sources: Optional[list[str]] = None):
    """Write generated lore back to buildings_full_merge_scanning.

    Citations are written WITH the text, in `storytelling_sources`. They used to
    be computed during generation and discarded, so a cache hit returned prose
    with no citations at all — and a cache hit is every read after the first,
    which meant citations effectively never reached the app despite the chain
    producing them correctly every time.
    """
    import json as _json
    try:
        await session.execute(
            text("""
                UPDATE buildings_full_merge_scanning
                SET storytelling = :lore,
                    -- COALESCE so a regeneration that finds no citations does
                    -- not erase ones an earlier run did find.
                    storytelling_sources =
                      COALESCE(CAST(:sources AS jsonb), storytelling_sources)
                WHERE REPLACE(bin, '.0', '') = :bin
            """),
            {'lore': lore, 'bin': bin_val,
             'sources': _json.dumps(sources) if sources else None}
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


def _merge_sources(*groups) -> list[str]:
    """Combine citation lists, order-preserving, without near-duplicates.

    The tier's own citation and the architect lookup are produced by different
    code paths, so `search`'s internal dedupe never sees them together. Observed
    live: VIA 57 West cited en.wikipedia.org/wiki/Via_57_West AND
    /wiki/VIA_57_West, one article rendered twice in a four-line SOURCES block.
    Reuses the search dedupe key so "same page, different clothes" means the
    same thing everywhere.
    """
    from services.brave_search import _dedupe_key
    seen, out = set(), []
    for group in groups:
        for u in group or []:
            if not u:
                continue
            k = _dedupe_key(u)
            if k in seen:
                continue
            seen.add(k)
            out.append(u)
    return out


@dataclass
class LoreResult:
    """Lore plus the provenance needed to render honest citations and to measure
    how often each tier actually fires.

    `tier` is the whole point. Tier 3 (`brave_search`) is the only one that
    costs anything beyond tokens, and its price is now a constant we set rather
    than a number a model chose — the agentic path it replaced measured
    $0.09-0.55 per building with a long tail. The tier-3 rate across real
    traffic is what decides whether the search subscription earns its keep, and
    the tier-4 (`fields_only`) rate is what it costs in reach: those are the
    buildings that get a description instead of history. Nothing else can tell
    us either number.
    """
    text: str
    tier: str                      # landmark_chunks | wikipedia | brave_search | fields_only
    specificity: Optional[str]     # 'building' | None (district-level)
    source: Optional[str]          # primary: report URL / article / None
    synthesized: bool              # False when raw source text was served as-is
    # Every citation, primary first. `source` stays the single primary for
    # existing callers; this carries the extras — chiefly the architect's own
    # page, which the tier chain cannot reach on its own because it
    # short-circuits at whichever tier answers first.
    sources: list[str] = field(default_factory=list)


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
        block_ctx = await _bounded(_get_block_context(session, bin_val), 'block_context')
        arch_n = await _bounded(_get_architect_catalogue_count(session, architect), 'architect_count')
        near = await _bounded(_get_nearby_lore(session, bin_val), 'nearby_lore')
        # The designation report is the primary citation; the firm's own page is
        # the one thing it cannot supply, and this tier short-circuits before
        # Brave just as the Wikipedia one does.
        from services import brave_search as _bs
        cite_task = asyncio.create_task(
            _bs.architect_citation(architect, building_name or address)
        )
        extra = await cite_task
        # Snippets go INTO the prompt, not just into the citation list. They
        # used to be cited without ever being shown to the model, so SOURCES
        # named pages the prose was not written from -- the exact wrong-citation
        # defect this pipeline exists to prevent. Feeding them also fixes the
        # thinness: a designation report describes fabric, while the pages an
        # architect query surfaces (Brownstoner, Architizer, the firm itself)
        # carry what actually happened in the building.
        from services import brave_search as _bs2
        extra_text = _bs2.as_source_text(extra, max_chars=1500)
        raw_plus = raw if not extra_text else (
            raw + "\n\nAdditional web sources about this building:\n" + extra_text
        )
        lore = await _synthesize(raw_plus, building_name, address, year_built,
                                           style, architect, block_ctx, arch_n, near)
        if lore:
            # Citation lookup starts BEFORE synthesis and is awaited after, so
            # its Brave round trip overlaps the LLM call instead of following
            # it. Citation-only -- it never feeds the prompt -- so nothing
            # depends on the ordering.
            #
            # The three context helpers above are deliberately NOT gathered:
            # they share one AsyncSession, and SQLAlchemy forbids concurrent
            # operations on a single session. Parallelising them needs separate
            # sessions, which is a bigger change than this latency is worth.
            all_sources = _merge_sources([src], _bs2.source_urls(extra, limit=2))
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore, all_sources)
            return LoreResult(text=lore, tier="landmark_chunks", specificity=spec,
                              source=src, synthesized=True, sources=all_sources)
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
        wiki = await _get_lore_from_wikipedia(building_name, address)
        if wiki:
            raw, wiki_url = wiki
            block_ctx = await _bounded(_get_block_context(session, bin_val), 'block_context')
            arch_n = await _bounded(_get_architect_catalogue_count(session, architect), 'architect_count')
            near = await _bounded(_get_nearby_lore(session, bin_val), 'nearby_lore')
            # Same overlap as the landmark tier: the citation query runs while
            # the model writes.
            from services import brave_search as _bs
            cite_task = asyncio.create_task(
                _bs.architect_citation(architect, building_name or address)
            )
            extra = await cite_task
            from services import brave_search as _bs2
            extra_text = _bs2.as_source_text(extra, max_chars=1500)
            raw_plus = raw if not extra_text else (
                raw + "\n\nAdditional web sources about this building:\n" + extra_text
            )
            lore = await _synthesize(raw_plus, building_name, address, year_built,
                                               style, architect, block_ctx, arch_n, near)
            # A Wikipedia extract is at least written prose, so serving it raw is
            # tolerable where raw LPC typescript is not. It is still marked
            # unsynthesized so the cost/quality split stays visible.
            synthesized = bool(lore)
            if not lore:
                lore = raw
            # One extra query for the firm's own page. This tier answers a lot
            # of the buildings people actually care about, and short-circuiting
            # here meant they cited Wikipedia and nothing else.
            all_sources = _merge_sources([wiki_url], _bs2.source_urls(extra, limit=2))
            if cache_to_db:
                await _cache_storytelling(session, bin_val, lore, all_sources)
            return LoreResult(text=lore, tier="wikipedia", specificity="building",
                              source=wiki_url, synthesized=synthesized,
                              sources=all_sources)

    # 3. Paid search — the ONLY tier that hits the open web, and the only one
    # that costs money beyond tokens. N literal queries in, N billed, always.
    # The agentic path this replaced let the MODEL decide how many searches to
    # run, which reached 13-24 per building and once cost $0.55 for one.
    #
    # When no Brave key is set this tier is SKIPPED SILENTLY and the chain drops
    # to a fields-only description. That is a quiet loss of reach, so the miss
    # is logged — an unconfigured key used to be indistinguishable from a
    # building the web simply had nothing on.
    from services import brave_search
    if not brave_search.is_configured():
        logger.warning(
            f"BRAVE_API_KEY not set — skipping web tier for BIN {bin_val}; "
            f"falling through to a fields-only description"
        )
    if brave_search.is_configured():
        cats = await _get_lore_categories()
        queries = brave_search.build_queries(building_name, address, architect,
                                             year_built, categories=cats)
        results = brave_search.filter_relevant(
            await brave_search.search(queries), building_name, address,
        )
        raw = brave_search.as_source_text(results)
        if raw:
            block_ctx = await _bounded(_get_block_context(session, bin_val), 'block_context')
            arch_n = await _bounded(_get_architect_catalogue_count(session, architect), 'architect_count')
            near = await _bounded(_get_nearby_lore(session, bin_val), 'nearby_lore')
            lore = await _synthesize(raw, building_name, address,
                                               year_built, style, architect,
                                               block_ctx, arch_n, near)
            if lore:
                urls = brave_search.source_urls(results)
                if cache_to_db:
                    await _cache_storytelling(session, bin_val, lore, urls)
                # No extra architect query here: this tier already RAN the
                # architect query as part of its fan-out, so the firm page is
                # in `urls` if it exists. Asking again would bill twice.
                return LoreResult(text=lore, tier="brave_search",
                                  specificity="building",
                                  source=urls[0] if urls else None,
                                  synthesized=True, sources=urls)
            logger.warning(f"brave results found but synthesis failed for {bin_val}")

    # 4. Fields only. Named `fields_only`, not `web_search`, because it no
    # longer searches anything — the old name would have kept reporting web
    # coverage the chain had stopped providing, and `tier` is the metric the
    # spend decision rests on.
    lore = await _describe_from_fields(building_name, address, year_built,
                                       style, architect, materials)
    if lore:
        if cache_to_db:
            await _cache_storytelling(session, bin_val, lore)
        return LoreResult(text=lore, tier="fields_only", specificity="building",
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

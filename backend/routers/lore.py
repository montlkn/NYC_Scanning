"""
Lore Router — building lore via the tiered chain, cheapest source first.

Why this exists
───────────────
`services/lore_generator.generate_building_lore` implements a four-tier chain
(LPC designation reports → Wikipedia → capped Brave search → fields-only) but
nothing called it:
the scan router's two call sites were removed with the matching-pipeline
cleanup, leaving it dead code. Meanwhile the iOS client generates lore by
calling an agentic web-search model directly for EVERY building — the most
expensive tier, unconditionally, even for the ~13k buildings whose designation
report we already hold.

This endpoint puts the chain back in front of that path.

Provenance is returned alongside the text on purpose:
  * `tier` — which source answered: `landmark_chunks` | `wikipedia` |
    `brave_search` | `fields_only` | `cache`. Only `brave_search` costs money
    beyond tokens, so its rate over real traffic decides whether the search
    subscription earns its keep; the `fields_only` rate is the matching cost in
    reach, since those buildings get a description rather than history.
  * `specificity` — 'building' means the text is about THIS building; null
    means it is the district-level blurb shared by every building in the
    historic district. The client must not present the latter as though it
    were specific, and the distinction is invisible in the prose itself.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from models.session import get_db
from services.lore_generator import generate_building_lore_detailed, get_comparative
from utils.rate_limit import limiter, LIMIT_INFERENCE

router = APIRouter(prefix="/lore", tags=["lore"])
logger = logging.getLogger(__name__)


@router.get("/{bin_val}")
@limiter.limit(LIMIT_INFERENCE)
async def get_building_lore(
    request: Request,
    bin_val: str,
    refresh: bool = Query(False, description="Bypass the cached storytelling column"),
    generate: bool = Query(
        True,
        description="When false, serve cache only — never run the billed chain",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Lore for one building, by BIN.

    Serves the cached `storytelling` column when present unless `refresh=true`,
    otherwise runs the tier chain and caches the result.

    `generate=false` is the PREFETCH contract: answer from cache or say nothing,
    and never spend. The map warms lore for the strongest picks whenever its
    canon refreshes — on pan AND zoom — which at ~4 Brave queries plus an LLM
    call per building was real money for buildings nobody tapped. One tester
    produced 287 search requests before launch. A prefetch wants an instant tap
    for something already generated; it never justified paying to generate.
    """
    # BINs are stored numeric-as-text and reach us with a '.0' suffix from
    # several callers; normalise once here so every lookup below agrees.
    bin_clean = (bin_val or "").replace(".0", "").strip()
    if not bin_clean.isdigit():
        raise HTTPException(status_code=400, detail="bin must be numeric")

    try:
        row = (await db.execute(sql_text("""
            SELECT building_name, address, year_built, style, architect,
                   mat_primary, storytelling, storytelling_sources,
                   storytelling_comparative, comparative_basis
            FROM buildings_full_merge_scanning
            WHERE replace(bin, '.0', '') = :bin
            LIMIT 1
        """), {"bin": bin_clean})).fetchone()
    except Exception as e:
        logger.warning(f"lore lookup failed for BIN {bin_clean}: {e}")
        raise HTTPException(status_code=503, detail="building lookup unavailable")

    if not row:
        raise HTTPException(status_code=404, detail="building not found")

    (name, address, year_built, style, architect, materials,
     cached, cached_sources, cached_comparative, comparative_basis) = row

    if cached and not refresh:
        return {
            "bin": bin_clean,
            "lore": cached,
            "tier": "cache",
            "specificity": None,
            # Citations stored alongside the text. Before they were persisted,
            # a cache hit returned prose with NO citations -- and a cache hit is
            # every read after the first, so the chain's citations never reached
            # a user despite being produced correctly each time.
            "source": (cached_sources or [None])[0],
            "sources": cached_sources or [],
            "synthesized": True,
            # Second pass, generated AFTER the building's own lore and cached
            # separately: the comparison depends on how much of the block has
            # been written, so it must be able to improve without rewriting
            # (and re-paying for) the narrative it sits beside.
            "comparative": await get_comparative(
                db, bin_clean, name, cached, cached_comparative, comparative_basis
            ),
        }

    # Cache miss on a cache-only request. Return the same shape a declined chain
    # returns, so the caller needs no special case, and spend nothing: no Brave
    # queries, no LLM call, no `storytelling` write. Returning early here rather
    # than passing a flag downward keeps the guarantee readable — there is no
    # path from this branch into the billed chain.
    if not generate:
        return {
            "bin": bin_clean,
            "lore": None,
            "tier": None,
            "specificity": None,
            "source": None,
            "sources": [],
            "synthesized": False,
            "comparative": None,
        }

    result = await generate_building_lore_detailed(
        db, bin_clean,
        building_name=name,
        address=address,
        year_built=str(year_built) if year_built is not None else None,
        style=style,
        architect=architect,
        materials=materials,
    )
    if not result:
        # Every tier declined. That is a real outcome, not an error — the
        # client shows the building without lore rather than a failure state.
        return {
            "bin": bin_clean,
            "lore": None,
            "tier": None,
            "specificity": None,
            "source": None,
            "sources": [],
            "synthesized": False,
            "comparative": None,
        }

    logger.info(
        f"[LORE] bin={bin_clean} tier={result.tier} "
        f"specificity={result.specificity} synthesized={result.synthesized}"
    )
    return {
        "bin": bin_clean,
        "lore": result.text,
        "tier": result.tier,
        "specificity": result.specificity,
        "source": result.source,
        # Every citation, primary first. `source` stays for older clients.
        "sources": result.sources or ([result.source] if result.source else []),
        "synthesized": result.synthesized,
        "comparative": await get_comparative(
            db, bin_clean, name, result.text, cached_comparative, comparative_basis
        ),
    }

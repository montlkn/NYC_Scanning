"""
Semantic building search over the Railway `building_search_index` (pgvector).

The query is embedded with the SAME bge-small model as the corpus, then ranked
by cosine distance with optional era/geo filters. Returns BINs + score + snippet;
the iOS app hydrates full building rows from Supabase by BIN.

This is vector SEARCH (ranking) — NOT lore RAG. Lore generation stays
client-side Grok web-search; see routers/rag.py for the separate lore-grounding
retrieval (Phase 2b).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Query
from sqlalchemy import text

from models.footprints_session import get_footprints_db
from services.text_embeddings import embed_query

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger(__name__)


def _vec_literal(vec: List[float]) -> str:
    """pgvector text literal: '[0.1,0.2,...]' for ::vector casting in raw SQL."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


@router.get("")
async def search_buildings(
    q: str = Query(..., description="Natural-language search query"),
    limit: int = Query(30, ge=1, le=100),
    lat: Optional[float] = Query(None, description="Center latitude for geo filter/sort"),
    lng: Optional[float] = Query(None, description="Center longitude for geo filter/sort"),
    radius_m: Optional[float] = Query(None, description="Geo radius filter in meters"),
    year_from: Optional[int] = Query(None, description="Earliest year_built (era filter)"),
    year_to: Optional[int] = Query(None, description="Latest year_built (era filter)"),
) -> List[dict]:
    """Semantic search → ranked building BINs. Empty list on any failure (the
    client falls back to its local hint-index / Supabase ILIKE path)."""
    try:
        qvec = embed_query(q)
    except Exception as e:  # model load / inference failure must not 500 the app
        logger.error(f"[search] query embedding failed: {e}", exc_info=True)
        return []

    params: dict = {"qvec": _vec_literal(qvec), "limit": limit}
    filters: List[str] = []

    if year_from is not None:
        filters.append("year_built >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        filters.append("year_built <= :year_to")
        params["year_to"] = year_to

    geo_select = ""
    if lat is not None and lng is not None:
        params["lat"] = lat
        params["lng"] = lng
        geo_select = (
            ", ST_Distance(geog, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) AS dist_m"
        )
        if radius_m is not None:
            params["radius_m"] = radius_m
            filters.append(
                "geog IS NOT NULL AND ST_DWithin("
                "geog, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :radius_m)"
            )

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    sql = f"""
        SELECT bin, snippet, 1 - (embedding <=> :qvec::vector) AS score{geo_select}
        FROM building_search_index
        {where}
        ORDER BY embedding <=> :qvec::vector
        LIMIT :limit
    """

    try:
        async with get_footprints_db() as db:
            if db is None:
                logger.warning("[search] footprints DB not configured (FOOTPRINTS_DB_URL)")
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[search] query failed: {e}", exc_info=True)
        return []

    return [
        {
            "bin": str(r[0]).replace(".0", "") if r[0] else None,
            "snippet": r[1],
            "score": round(float(r[2]), 4) if r[2] is not None else None,
        }
        for r in rows
    ]

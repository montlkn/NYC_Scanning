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

from models.search_session import get_search_db
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
        # Haversine (meters) — the search DB has no PostGIS. acos arg is clamped
        # to [-1, 1] for numerical safety.
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        geo_select = f", {haversine} AS dist_m"
        if radius_m is not None:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # Use CAST(:qvec AS vector), NOT :qvec::vector — SQLAlchemy's text() parser
    # treats `::` as the start of a named param and mangles the bound vector
    # (psycopg then sees a literal ":qvec" and errors "syntax error at or near
    # ':'"). CAST(...) is colon-free and binds cleanly.
    sql = f"""
        SELECT bin, snippet, 1 - (embedding <=> CAST(:qvec AS vector)) AS score{geo_select}
        FROM building_search_index
        {where}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    """

    try:
        async with get_search_db() as db:
            if db is None:
                logger.warning("[search] search DB not configured (SEARCH_DB_URL)")
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


@router.get("/venues")
async def search_venues(
    q: str = Query(..., description="Natural-language venue query, e.g. 'dimly lit speakeasy'"),
    limit: int = Query(20, ge=1, le=100),
    lat: Optional[float] = Query(None, description="Center latitude for geo sort/filter"),
    lng: Optional[float] = Query(None, description="Center longitude for geo sort/filter"),
    radius_m: Optional[float] = Query(None, description="Geo radius filter in meters"),
    year_from: Optional[int] = Query(None, description="Host-building earliest year_built"),
    year_to: Optional[int] = Query(None, description="Host-building latest year_built"),
) -> List[dict]:
    """Semantic VENUE search over `venues` (FSQ places), returning the venue plus
    its host-building provenance (bin/year). This is the moat: "original
    midcentury bar" ranks high because each venue's embedding text carries its
    building's era. Empty list on any failure (client falls back to MKLocalSearch)."""
    try:
        qvec = embed_query(q)
    except Exception as e:
        logger.error(f"[venues] query embedding failed: {e}", exc_info=True)
        return []

    params: dict = {"qvec": _vec_literal(qvec), "limit": limit}
    filters: List[str] = []

    # Era filter applies to the HOST BUILDING's year — "original midcentury bar".
    if year_from is not None:
        filters.append("building_year >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        filters.append("building_year <= :year_to")
        params["year_to"] = year_to

    geo_select = ""
    if lat is not None and lng is not None:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        geo_select = f", {haversine} AS dist_m"
        if radius_m is not None:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    sql = f"""
        SELECT fsq_id, name, category, snippet,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score,
               lat, lng, bin, bbl, building_year{geo_select}
        FROM venues
        {where}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    """

    try:
        async with get_search_db() as db:
            if db is None:
                logger.warning("[venues] search DB not configured (SEARCH_DB_URL)")
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[venues] query failed: {e}", exc_info=True)
        return []

    return [
        {
            "fsq_id": r[0],
            "name": r[1],
            "category": r[2],
            "snippet": r[3],
            "score": round(float(r[4]), 4) if r[4] is not None else None,
            "lat": r[5],
            "lng": r[6],
            "bin": str(r[7]).replace(".0", "") if r[7] else None,
            "bbl": str(r[8]).replace(".0", "") if r[8] else None,
            "building_year": r[9],
        }
        for r in rows
    ]


@router.get("/layers")
async def search_layers(
    q: str = Query(..., description="Natural-language query, e.g. '1977 blackout'"),
    limit: int = Query(30, ge=1, le=100),
    lat: Optional[float] = Query(None, description="Center latitude for geo sort/filter"),
    lng: Optional[float] = Query(None, description="Center longitude for geo sort/filter"),
    radius_m: Optional[float] = Query(None, description="Geo radius filter in meters"),
    layer: Optional[str] = Query(None, description="Restrict to one layer: lore|plaque|contribution"),
) -> List[dict]:
    """Semantic search over the OTHER map layers (lore events, plaques, community
    contributions) in `layer_search_index`. Returns prefixed ids + coords so the
    iOS app can light up + filter the matching map layer. Empty list on any
    failure (search simply doesn't surface those layers)."""
    try:
        qvec = embed_query(q)
    except Exception as e:
        logger.error(f"[layers] query embedding failed: {e}", exc_info=True)
        return []

    params: dict = {"qvec": _vec_literal(qvec), "limit": limit}
    filters: List[str] = []

    if layer:
        filters.append("layer = :layer")
        params["layer"] = layer

    geo_select = ""
    if lat is not None and lng is not None:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        geo_select = f", {haversine} AS dist_m"
        if radius_m is not None:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    sql = f"""
        SELECT id, layer, title, snippet,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score,
               lat, lng, year, category{geo_select}
        FROM layer_search_index
        {where}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    """

    try:
        async with get_search_db() as db:
            if db is None:
                logger.warning("[layers] search DB not configured (SEARCH_DB_URL)")
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[layers] query failed: {e}", exc_info=True)
        return []

    return [
        {
            "id": r[0],
            "layer": r[1],
            "title": r[2],
            "snippet": r[3],
            "score": round(float(r[4]), 4) if r[4] is not None else None,
            "lat": r[5],
            "lng": r[6],
            "year": r[7],
            "category": r[8],
        }
        for r in rows
    ]

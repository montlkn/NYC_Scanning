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


# Filler words that carry no proper-noun / style signal. The LEXICAL (trigram)
# pool only exists to recover names, architects, materials and styles, so prose
# stopwords are pure noise there — and worse, they false-match building names
# ("buildings that LOOK like wedding cakes" trigram-hit "Look Building"). We
# strip them from q_lex ONLY; the vector path keeps the full query for semantics.
_LEX_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "that", "this", "these", "those", "is", "are", "was", "were", "be", "been",
    "it", "its", "as", "by", "from", "into", "like", "look", "looks", "looking",
    "feel", "feels", "feeling", "kind", "sort", "type", "very", "really", "some",
    "any", "all", "me", "show", "find", "buildings", "building", "place",
    "places", "something", "somewhere", "near", "around",
})


def _lexical_query(q: str) -> str:
    """Strip prose stopwords so the trigram pool keys on distinctive terms only.

    Falls back to the full query if stripping leaves nothing (e.g. a query that
    is entirely stopwords) so the lexical pool never goes empty.
    """
    kept = [w for w in q.split() if w.lower().strip(".,!?;:'\"") not in _LEX_STOPWORDS]
    return " ".join(kept) if kept else q


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
    haversine_b = ""  # b-aliased (final SELECT); set when geo provided
    if lat is not None and lng is not None:
        params["lat"] = lat
        params["lng"] = lng
        # Haversine (meters) — the search DB has no PostGIS. acos arg is clamped
        # to [-1, 1] for numerical safety. Two forms: unaliased for the CTE
        # radius filter (queries `building_search_index` directly), b-aliased for
        # the final SELECT's dist_m (joined as `b`).
        def _hav(col_lat: str, col_lng: str) -> str:
            return (
                "6371000 * acos(GREATEST(-1, LEAST(1, "
                f"cos(radians(:lat)) * cos(radians({col_lat})) * cos(radians({col_lng}) - radians(:lng)) "
                f"+ sin(radians(:lat)) * sin(radians({col_lat})))))"
            )
        haversine = _hav("lat", "lng")
        haversine_b = _hav("b.lat", "b.lng")
        geo_select = f", {haversine} AS dist_m"
        if radius_m is not None:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # Hybrid ranking: fuse semantic cosine with a lexical (trigram) score over the
    # indexed `text` column. Pure vector search is strong on style/material
    # CONCEPTS but weak on PROPER NOUNS — "chrysler" returned RCA Building, "neil
    # denari" returned unrelated brownstones, because bge-small weights a name
    # equally with the surrounding spec-sheet tokens. `text` already contains the
    # name + architect (it's the embedded string), so word_similarity() catches
    # the proper noun and lifts the right row. word_similarity (not similarity)
    # measures the query against the BEST-MATCHING substring of `text`, so a short
    # name query isn't penalised by the long descriptive text around it.
    #
    # Fusion weights: vector leads (0.7) so concept queries are unchanged; lexical
    # (0.3) is enough that a strong name/architect match overtakes a loosely-
    # related semantic neighbour. Requires pg_trgm + a GIN trigram index on
    # `text` (migration 20260619_hybrid_trigram.sql) — without the extension this
    # SELECT errors and the whole endpoint returns [] (client falls back), so the
    # extension MUST be present before deploy.
    params["q_lex"] = _lexical_query(q)
    # Columns are qualified for the final join: b.* = table row, wl.lex = lateral
    # word_similarity. Keep in sync with the SELECT below.
    fused = "(0.7 * (1 - (b.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex)"

    # Candidate pool = UNION of two recall paths, each using its own index:
    #   • vector top-N  (HNSW)         — concept recall ("art deco lobbies")
    #   • trigram top-N (GIN pg_trgm)  — proper-noun recall ("chrysler")
    # A pure vector pool was the bug: the Chrysler Building's cosine is near the
    # noise floor, so it never entered a cosine-ordered top-200 and the trigram
    # boost couldn't reach it (its word_similarity is 1.0). Pulling a lexical
    # candidate set in parallel guarantees a strong name match is always scored.
    pool = min(max(limit * 4, 40), 200)
    params["pool"] = pool
    # word_similarity floor for the lexical candidate set. 0.3 admits a clear
    # name match ("chrysler" → "Chrysler Building" scores ~1.0) while rejecting
    # incidental trigram overlap. word_similarity(query, text) — arg order
    # matters: it measures the SHORT query against the best substring of the
    # LONG text, so a 1-word name isn't diluted by the surrounding description.
    params["lex_floor"] = 0.3

    # Typo tolerance: a misspelled proper noun ("chrylser", "guggenhiem") can
    # fall under the word_similarity floor and miss the lexical pool entirely.
    # A third recall path uses similarity() — full-string trigram overlap, which
    # degrades gracefully under a transposition/typo — at a lower floor, and the
    # fused score takes max(word_similarity, similarity) so a clean exact match
    # is never penalised but a fuzzy one can still surface. Same GIN index, no
    # re-embed. Floor 0.2 admits a 1-char typo on a short name while rejecting
    # noise. The pool is small, so the extra CTE is cheap.
    params["fuzzy_floor"] = 0.2

    # Use CAST(:qvec AS vector), NOT :qvec::vector — SQLAlchemy's text() parser
    # treats `::` as the start of a named param and mangles the bound vector
    # (psycopg then sees a literal ":qvec" and errors "syntax error at or near
    # ':'"). CAST(...) is colon-free and binds cleanly.
    # UNION the candidate BINs ONLY (not the rows) — UNION over the embedding
    # vector column throws "could not identify an ordering operator for type
    # vector" because pgvector has no hash/sort opclass for UNION's dedup. We
    # collect distinct BINs from the two recall paths, then join back to the
    # table once to fetch+score the row data.
    sql = f"""
        WITH vec_pool AS (
            SELECT bin
            FROM building_search_index
            {where}
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :pool
        ),
        lex_pool AS (
            SELECT bin
            FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}word_similarity(lower(:q_lex), lower(text)) > :lex_floor
            ORDER BY word_similarity(lower(:q_lex), lower(text)) DESC
            LIMIT :pool
        ),
        fuzzy_pool AS (
            SELECT bin
            FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}similarity(lower(:q_lex), lower(text)) > :fuzzy_floor
            ORDER BY similarity(lower(:q_lex), lower(text)) DESC
            LIMIT :pool
        ),
        pool AS (
            SELECT bin FROM vec_pool
            UNION
            SELECT bin FROM lex_pool
            UNION
            SELECT bin FROM fuzzy_pool
        )
        SELECT b.bin, b.snippet,
               {fused} AS score{(', ' + haversine_b + ' AS dist_m' if geo_select else '')}
        FROM building_search_index b
        JOIN pool USING (bin)
        CROSS JOIN LATERAL (
            SELECT greatest(
                word_similarity(lower(:q_lex), lower(b.text)),
                similarity(lower(:q_lex), lower(b.text))
            ) AS lex
        ) wl
        ORDER BY score DESC
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

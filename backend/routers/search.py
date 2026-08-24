"""
Semantic building search over the Railway `building_search_index` (pgvector).

The query is embedded with the SAME bge-small model as the corpus, then ranked
by cosine distance with optional era/geo filters. Returns BINs + score + snippet;
the iOS app hydrates full building rows from Supabase by BIN.

This is vector SEARCH (ranking) — NOT lore RAG. Lore generation stays
client-side; see routers/rag.py for the separate lore-grounding
retrieval (Phase 2b).
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Query, Request
from sqlalchemy import text

from models.search_session import get_search_db
from services.text_embeddings import embed_query
from utils.rate_limit import limiter, LIMIT_SEARCH
from services.openai_text import openai_text
from services.unified_search import (
    HARD_RADIUS_INTENTS,
    RankedHit,
    apply_nudges,
    build_facets,
    build_header,
    build_why,
    classify_intent,
    classify_intent_detailed,
    corpus_weights,
    coverage_adjustment,
    apply_relevance_floor,
    dedupe_near_identical,
    exact_name_bonus,
    architect_match_bonus,
    fame_boost,
    FAME_BOOST_INTENTS,
    fold_ordinals,
    hedged_style_penalty,
    house_number_bonus,
    infer_matched_field,
    poi_category_adjustment,
    query_style_tokens,
    style_name_decoy_penalty,
    venue_style_affinity,
    profile_similarity,
    proximity_decay_bonus,
    reciprocal_rank_fusion,
    RRF_SCALE,
    W_LEG_FAME,
)

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
    out = " ".join(kept) if kept else q
    # Ordinal fold ("1 south first" -> "1 south 1st"). The corpus stores the
    # numeral spelling, so without this the trigram pool can never retrieve an
    # ordinal-street address and the row simply never becomes a candidate —
    # a retrieval miss no downstream reranking can repair.
    return fold_ordinals(out) or out


# word_similarity()/similarity() alone false-match short generic tokens as
# SUBSTRINGS: "bar" trigram-hits "decoy", "deco" hits "decorating"/"Decoy
# workspace". Confirmed live against the venues table (word_similarity(lower
# ('deco'), ...) scores "High Style Deco" AND "Decoration Day" both >= 0.3).
# Fix: require at least one WHOLE WORD from q_lex to appear in the matched
# text (Postgres \m...\M word-boundary regex), in addition to the similarity
# floor. This is an extra AND-ed predicate, not a replacement — similarity
# still ranks within the whole-word-filtered set so multi-word queries keep
# their fuzzy/typo tolerance on the OTHER tokens.
_WORD_BOUNDARY_MIN_LEN = 3  # tokens shorter than this (e.g. "a", "16") are too
# short to build a meaningful word-boundary predicate and are skipped when
# constructing the pattern; if ALL tokens are short, the whole-word filter is
# omitted entirely (see _word_boundary_pattern's None return) rather than
# blocking legitimate short-name queries.


def _word_boundary_pattern(q_lex: str) -> Optional[str]:
    """Build a Postgres regex alternation of \\m<token>\\M for each q_lex token
    with len >= _WORD_BOUNDARY_MIN_LEN. Returns None if no token qualifies
    (caller should skip the whole-word predicate in that case)."""
    import re as _re
    toks = [t for t in q_lex.split() if len(t) >= _WORD_BOUNDARY_MIN_LEN]
    if not toks:
        return None
    return "|".join(rf"\m{_re.escape(t.lower())}\M" for t in toks)


# Raised from 0.3: a bare word_similarity floor of 0.3 still admits a single
# short token (e.g. "bar", "deco") matching an unrelated substring at scores
# up to 1.0 (see comment above) — the word-boundary regex is the real fix,
# but a higher floor also helps on the LONGER end (fewer weak multi-word
# partial matches slipping into the candidate pool). 0.45 keeps the confirmed
# multi-word matches (e.g. "art deco bar" -> "Patricia Shea Fine Art" 0.615,
# "Art Deco Building" 0.77) while cutting the previous single-short-token noise.
LEX_FLOOR = 0.45


@router.get("")
@limiter.limit(LIMIT_SEARCH)
async def search_buildings(
    request: Request,
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
            # Fix: dist_m was computed in the SQL (geo_select/haversine_b) but
            # previously dropped here when geo params were supplied.
            "dist_m": round(float(r[3]), 1) if geo_select and len(r) > 3 and r[3] is not None else None,
        }
        for r in rows
    ]


@router.get("/venues/nearby")
@limiter.limit(LIMIT_SEARCH)
async def venues_nearby(
    request: Request,
    lat: float = Query(..., description="Center latitude"),
    lng: float = Query(..., description="Center longitude"),
    categories: str = Query(
        ...,
        description="Comma-separated FSQ category names, e.g. 'Bar,Cocktail Bar,Pub'",
    ),
    radius_m: float = Query(800, ge=50, le=5000),
    limit: int = Query(30, ge=1, le=100),
    require_building: bool = Query(
        False,
        description="Only venues geo-joined to a host building (the provenance moat)",
    ),
) -> List[dict]:
    """Venues of a given CATEGORY near a point, ordered by distance.

    Distinct from `/venues`, which is a semantic search: this one does no
    embedding at all. "Give me the bars within 800m" is a filter, not a
    similarity question, and routing it through pgvector was actively harmful —
    HNSW returns at most `hnsw.ef_search` candidates (default 40), so a category
    query silently capped at ~40 rows however large the LIMIT, and which 40 you
    got depended on the embedding of the word you happened to type. Asking for
    'bar' near Midtown returned nothing at all, because 'bar' as a SENTENCE is
    not close to a bar's embedded description.

    Ordering by distance also means the caller gets the nearest venues rather
    than the most semantically bar-like ones, which is what a route builder
    actually wants.

    `category` is matched case-insensitively and exactly. The values are FSQ's
    own human-readable labels — Bar, Cocktail Bar, Wine Bar, Pub, Night Club,
    Diner, Bakery, Coffee Shop, Breakfast Spot, Pizzeria, Deli — so the caller
    picks the vocabulary and this endpoint stays free of hardcoded taste.
    """
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    if not cats:
        return []

    haversine = (
        "6371000 * acos(GREATEST(-1, LEAST(1, "
        "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
        "+ sin(radians(:lat)) * sin(radians(lat)))))"
    )
    params: dict = {
        "lat": lat,
        "lng": lng,
        "radius_m": radius_m,
        "limit": limit,
        "cats": [c.lower() for c in cats],
    }
    where = [
        "lat IS NOT NULL",
        "lng IS NOT NULL",
        "lower(category) = ANY(:cats)",
        f"{haversine} <= :radius_m",
    ]
    if require_building:
        where.append("bin IS NOT NULL")

    sql = f"""
        SELECT fsq_id, name, category, snippet, lat, lng,
               bin, bbl, building_year, building_style,
               {haversine} AS dist_m
        FROM venues
        WHERE {" AND ".join(where)}
        ORDER BY dist_m ASC
        LIMIT :limit
    """

    try:
        async with get_search_db() as db:
            if db is None:
                logger.warning("[venues/nearby] search DB not configured (SEARCH_DB_URL)")
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[venues/nearby] query failed: {e}", exc_info=True)
        return []

    return [
        {
            "fsq_id": r[0],
            "name": r[1],
            "category": r[2],
            "snippet": r[3],
            "lat": r[4],
            "lng": r[5],
            "bin": str(r[6]).replace(".0", "") if r[6] else None,
            "bbl": str(r[7]).replace(".0", "") if r[7] else None,
            "building_year": r[8],
            "building_style": r[9],
            "dist_m": round(float(r[10]), 1) if r[10] is not None else None,
        }
        for r in rows
    ]


@router.get("/venues/categories")
@limiter.limit(LIMIT_SEARCH)
async def venue_categories(request: Request) -> List[dict]:
    """Every category present in the corpus, with counts.

    Exists so callers never hardcode a category vocabulary they cannot verify.
    A client that guesses 'Bars' instead of 'Bar' gets silence; this endpoint is
    how it finds out the real spelling.
    """
    sql = """
        SELECT category, count(*) AS n
        FROM venues
        WHERE category IS NOT NULL
        GROUP BY category
        ORDER BY n DESC
    """
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            rows = (await db.execute(text(sql))).fetchall()
    except Exception as e:
        logger.error(f"[venues/categories] query failed: {e}", exc_info=True)
        return []
    return [{"category": r[0], "count": r[1]} for r in rows]


@router.get("/venues")
@limiter.limit(LIMIT_SEARCH)
async def search_venues(
    request: Request,
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
               lat, lng, bin, bbl, building_year,
               instagram, website, tel{geo_select}
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
            "instagram": r[10],
            "website": r[11],
            "tel": r[12],
        }
        for r in rows
    ]


@router.get("/layers")
@limiter.limit(LIMIT_SEARCH)
async def search_layers(
    request: Request,
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


# ---------------------------------------------------------------------------
# Unified search — retrieval legs (internal). Each returns hydrated dicts for
# fusion by services/unified_search.py, sharing ONE query embedding across all
# three corpora (embedded once by the caller and passed in as `qvec`/`qvec_lit`).
# Every leg is wrapped try/except and returns [] on failure, matching the
# existing silent-fallback contract — a broken leg never breaks the others.
# ---------------------------------------------------------------------------

async def _leg_buildings(
    qvec_lit: str,
    q_lex: str,
    limit: int,
    lat: Optional[float],
    lng: Optional[float],
    radius_m: Optional[float],
    year_from: Optional[int],
    year_to: Optional[int],
    borough: Optional[str] = None,
    material: Optional[str] = None,
    style_family: Optional[str] = None,
    user_vec_lit: Optional[str] = None,
    soft_radius: bool = False,
    fame_weight: float = 0.0,
) -> List[dict]:
    """Buildings leg for /unified — mirrors search_buildings()'s hybrid CTE but
    also returns name/style/year/landmark fields needed for `why`/header/facets.
    building_search_index has no dedicated `name`/`style` column: `text` is the
    embedded string and `snippet` is "{name/address} — {style}" by convention
    (see scripts/seed_venues.py::building_style for the same parsing pattern
    used on venues). We reuse that convention here rather than inventing a join
    to a table this DB doesn't have.

    soft_radius: when True (style/architect/lore/prose intents — see
    HARD_RADIUS_INTENTS in unified_search.py), radius_m is NOT applied as a
    WHERE filter — dist_m is still computed/returned for the caller to apply
    proximity_decay_bonus() as a soft scoring nudge instead. "surface results
    close to you but city-wide shouldn't be out of the question if it's
    closer to the search term" — a hard radius filter can't express that."""
    params: dict = {"qvec": qvec_lit, "limit": limit, "q_lex": q_lex}
    filters: List[str] = []
    if year_from is not None:
        filters.append("year_built >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        filters.append("year_built <= :year_to")
        params["year_to"] = year_to

    geo = lat is not None and lng is not None
    haversine_b = ""
    if geo:
        params["lat"] = lat
        params["lng"] = lng

        def _hav(col_lat: str, col_lng: str) -> str:
            return (
                "6371000 * acos(GREATEST(-1, LEAST(1, "
                f"cos(radians(:lat)) * cos(radians({col_lat})) * cos(radians({col_lng}) - radians(:lng)) "
                f"+ sin(radians(:lat)) * sin(radians({col_lat})))))"
            )
        haversine = _hav("lat", "lng")
        haversine_b = _hav("b.lat", "b.lng")
        if radius_m is not None and not soft_radius:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    # Real WHERE clauses against the new enriched columns (20260710_index_enrich.sql:
    # style_family/borough/material). Applied to `where` only in the ENRICHED
    # branch below — the fallback branch (old schema, no such columns) can't use
    # them, so filtering there degrades to no-op rather than crashing.
    enriched_filters: List[str] = []
    if borough:
        enriched_filters.append("lower(b.borough) = lower(:borough)")
        params["borough"] = borough
    if material:
        enriched_filters.append("lower(b.material) = lower(:material)")
        params["material"] = material
    if style_family:
        # ILIKE substring, not equality — style_family here is the parsed
        # style string (no normalized taxonomy exists), same semantics as the
        # pre-existing post-fetch substring filter this replaces for enriched rows.
        enriched_filters.append("lower(b.style_family) LIKE :style_family")
        params["style_family"] = f"%{style_family.replace('_', ' ').lower()}%"

    pool = min(max(limit * 4, 40), 200)
    params["pool"] = pool
    # Fame slice: vector-rank the top ~3% most famous rows (1000 of ~35k) and
    # admit the best 40. Both are pool-size tuning knobs, not relevance rules.
    params["fame_candidates"] = 1000
    params["fame_pool"] = 40
    # Leg-level fame ordering weight. The leg score is on the raw fused scale
    # (~0.5–0.9 with small gaps between deco-tagged rows), so 0.15 × fame
    # (Chrysler ≈ +0.12) reorders within the pool without drowning relevance.
    params["fame_w"] = fame_weight
    params["lex_floor"] = LEX_FLOOR
    params["fuzzy_floor"] = 0.2
    fused = "(0.7 * (1 - (b.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex)"

    # Word-boundary guard on the lex_pool (proper-noun recall) ONLY — see
    # _word_boundary_pattern's docstring. fuzzy_pool intentionally skips this:
    # it exists specifically for typo tolerance ("chrylser"), where a whole-
    # word match by definition won't be found.
    word_boundary = _word_boundary_pattern(q_lex)
    lex_word_boundary_clause = ""
    if word_boundary:
        params["lex_wb"] = word_boundary
        lex_word_boundary_clause = " AND lower(text) ~ :lex_wb"

    # Personalization dot product computed IN SQL via pgvector's negative
    # inner-product operator (<#>): dot(a,b) = -(a <#> b). Only meaningful when
    # both the enriched `profile` column exists AND a user_vec_lit was passed;
    # the pure-Python profile_similarity() in unified_search.py exists for
    # unit-testing this same math, not for the hot path (avoids round-tripping
    # a 9-float vector per row out of SQL just to redo the dot product in
    # Python).
    if user_vec_lit:
        params["uvec"] = user_vec_lit

    def _sql(enriched: bool) -> str:
        select_extra = (
            ", b.style_family AS b_style_family, b.borough AS b_borough, "
            "b.material AS b_material, b.photo_url AS b_photo_url, "
            # architect: backfilled from LPC gpmc-yuvp (26,430 rows). Until now
            # there was no architect column, so the `architect` intent and
            # infer_matched_field's architect slot had nothing to read.
            "b.architect AS b_architect"
            + (", -(b.profile <#> CAST(:uvec AS vector)) AS b_personalization"
               if (enriched and user_vec_lit) else "")
            if enriched else ""
        )
        return f"""
        WITH vec_pool AS (
            SELECT bin FROM building_search_index {where}
            ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :pool
        ),
        lex_pool AS (
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}word_similarity(lower(:q_lex), lower(text)) > :lex_floor{lex_word_boundary_clause}
            ORDER BY word_similarity(lower(:q_lex), lower(text)) DESC LIMIT :pool
        ),
        fuzzy_pool AS (
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}similarity(lower(:q_lex), lower(text)) > :fuzzy_floor
            ORDER BY similarity(lower(:q_lex), lower(text)) DESC LIMIT :pool
        ),
        fame_pool AS (
            -- Fame-aware retrieval slice: among the corpus's most famous rows
            -- (fame = normalized final_score, see backfill_fame.py), the ones
            -- nearest the query vector ALWAYS enter the candidate set, so
            -- "art deco" can surface the Chrysler Building even when the
            -- general vector pool fills up with obscure-but-closer matches.
            -- Ranking is still decided downstream (fused score + fame_boost).
            SELECT f.bin FROM (
                SELECT bin, embedding FROM building_search_index
                {where + (' AND ' if where else 'WHERE ')}fame IS NOT NULL
                ORDER BY fame DESC LIMIT :fame_candidates
            ) f
            ORDER BY f.embedding <=> CAST(:qvec AS vector) LIMIT :fame_pool
        ),
        pool AS (
            SELECT bin FROM vec_pool UNION SELECT bin FROM lex_pool
            UNION SELECT bin FROM fuzzy_pool UNION SELECT bin FROM fame_pool
        )
        SELECT b.bin, b.bbl, b.snippet, b.year_built, b.is_landmark, b.fame, b.lat, b.lng,
               {fused} AS score,
               wl.lex AS lex_score
               {select_extra}
               {(', ' + haversine_b + ' AS dist_m') if geo else ''}
        FROM building_search_index b
        JOIN pool USING (bin)
        CROSS JOIN LATERAL (
            SELECT greatest(
                word_similarity(lower(:q_lex), lower(b.text)),
                similarity(lower(:q_lex), lower(b.text))
            ) AS lex
        ) wl
        {('WHERE ' + ' AND '.join(enriched_filters)) if (enriched and enriched_filters) else ''}
        -- fame participates in the LEG's ordering (not just the post-RRF
        -- boost): the leg's LIMIT would otherwise cut a high-fame row (the
        -- Chrysler Building for "art deco") on raw fused score before the
        -- boost ever sees it. :fame_w is 0 for non-fame intents.
        ORDER BY ({fused} + :fame_w * coalesce(b.fame, 0)) DESC
        LIMIT :limit
    """

    enriched = True
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(_sql(True)), params)
            rows = result.fetchall()
    except Exception as e:
        # Graceful degradation: style_family/borough/material/photo_url columns
        # don't exist until 20260710_index_enrich.sql runs. Fall back to the
        # pre-enrichment SELECT (no filters, no photo_url) rather than 500ing —
        # same contract as the trigram-migration fallback pattern elsewhere in
        # this file.
        logger.info(f"[unified/buildings] enriched columns unavailable ({e}); falling back")
        enriched = False
        try:
            async with get_search_db() as db:
                if db is None:
                    return []
                result = await db.execute(text(_sql(False)), params)
                rows = result.fetchall()
        except Exception as e2:
            logger.error(f"[unified/buildings] query failed: {e2}", exc_info=True)
            return []

    has_personalization = enriched and bool(user_vec_lit)
    hits = []
    for r in rows:
        snippet = r[2] or ""
        name = snippet.split("—", 1)[0].strip() if "—" in snippet else snippet
        parsed_style = snippet.split("—", 1)[1].strip() if "—" in snippet else None
        # Architect IS a real column now (LPC backfill), so a token match can
        # be attributed to it honestly instead of being reported as a name
        # match — "buildings by Cass Gilbert" now says "matched: architect".
        extra_offset = 10  # index of first enriched column, when present
        style_family = r[extra_offset] if enriched else None
        borough_val = r[extra_offset + 1] if enriched else None
        material_val = r[extra_offset + 2] if enriched else None
        photo_url = r[extra_offset + 3] if enriched else None
        architect_val = r[extra_offset + 4] if enriched else None
        personalization_dot = float(r[extra_offset + 5]) if has_personalization and r[extra_offset + 5] is not None else None
        dist_idx = extra_offset + (6 if has_personalization else 5) if enriched else extra_offset
        hits.append({
            "type": "building",
            "id": str(r[0]).replace(".0", "") if r[0] else None,
            "bin": str(r[0]).replace(".0", "") if r[0] else None,
            "bbl": str(r[1]).replace(".0", "") if r[1] else None,
            "name": name or None,
            "snippet": snippet or None,
            "year": r[3],
            "style": style_family or parsed_style or None,
            "architect": architect_val,
            "borough": borough_val,
            "material": material_val,
            "category": None,
            "landmark": bool(r[4]) if r[4] is not None else None,
            "fame": float(r[5]) if r[5] is not None else None,
            "lat": r[6],
            "lng": r[7],
            "score": float(r[8]) if r[8] is not None else 0.0,
            "matched_field": (
                infer_matched_field(q_lex, name=name, style=style_family or parsed_style,
                                    architect=architect_val)
                if (r[9] or 0) > 0.5 else "semantic"
            ),
            "dist_m": round(float(r[dist_idx]), 1) if geo and len(r) > dist_idx and r[dist_idx] is not None else None,
            "photo_url": photo_url,
            "lore_status": None,
            "personalization_dot": personalization_dot,
        })
    return hits


async def _leg_venues(
    qvec_lit: str, q_lex: str, limit: int,
    lat: Optional[float], lng: Optional[float], radius_m: Optional[float],
    year_from: Optional[int], year_to: Optional[int],
    soft_radius: bool = False,
) -> List[dict]:
    """Venues leg. Trigram fusion mirrors the buildings CTE, reading migration
    20260710_unified_search.sql's GIN index on name||snippet. If that migration
    hasn't run yet, word_similarity()/similarity() still work (pg_trgm was
    already installed by 20260619) just without the index — slower, not
    broken — so no fallback branch is needed here.

    soft_radius: see _leg_buildings' docstring — radius_m skips the WHERE
    filter and dist_m becomes a scoring-only signal instead."""
    params: dict = {"qvec": qvec_lit, "limit": limit, "q_lex": q_lex}
    filters: List[str] = []
    if year_from is not None:
        filters.append("building_year >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        filters.append("building_year <= :year_to")
        params["year_to"] = year_to

    geo = lat is not None and lng is not None
    if geo:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        if radius_m is not None and not soft_radius:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    pool = min(max(limit * 4, 40), 200)
    params["pool"] = pool
    params["lex_floor"] = LEX_FLOOR
    fused = "(0.7 * (1 - (v.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex)"
    haversine_v = ""
    if geo:
        haversine_v = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(v.lat)) * cos(radians(v.lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(v.lat)))))"
        )

    # Word-boundary guard — see _word_boundary_pattern's docstring and
    # _leg_buildings' identical pattern. Venues has no separate fuzzy/typo
    # pool, so this is the ONLY trigram recall path here; skip the guard
    # entirely (None) when q_lex has no token long enough to build one.
    word_boundary = _word_boundary_pattern(q_lex)
    lex_word_boundary_clause = ""
    if word_boundary:
        params["lex_wb"] = word_boundary
        lex_word_boundary_clause = "AND lower(name || ' ' || coalesce(snippet, '')) ~ :lex_wb"

    def _sql(labels_col: bool) -> str:
      return f"""
        WITH vec_pool AS (
            SELECT fsq_id FROM venues {where}
            ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :pool
        ),
        lex_pool AS (
            SELECT fsq_id FROM venues
            {where + (' AND ' if where else 'WHERE ')}
            word_similarity(lower(:q_lex), lower(name || ' ' || coalesce(snippet, ''))) > :lex_floor
            {lex_word_boundary_clause}
            ORDER BY word_similarity(lower(:q_lex), lower(name || ' ' || coalesce(snippet, ''))) DESC
            LIMIT :pool
        ),
        pool AS (
            SELECT fsq_id FROM vec_pool UNION SELECT fsq_id FROM lex_pool
        )
        SELECT v.fsq_id, v.name, v.category, v.snippet, v.lat, v.lng,
               v.bin, v.bbl, v.building_year, v.building_style, v.photo_url,
               {fused} AS score, wl.lex AS lex_score
               {(', v.category_labels') if labels_col else ''}
               {(', ' + haversine_v + ' AS dist_m') if geo else ''}
        FROM venues v
        JOIN pool USING (fsq_id)
        CROSS JOIN LATERAL (
            SELECT word_similarity(lower(:q_lex), lower(v.name || ' ' || coalesce(v.snippet, ''))) AS lex
        ) wl
        ORDER BY score DESC
        LIMIT :limit
    """

    # category_labels only exists after 20260731_venues_category_labels.sql.
    # Probe for it exactly like the buildings leg probes its enriched columns —
    # WITHOUT this, an unmigrated DB would fail the whole hybrid query and
    # silently degrade every POI search to pure-vector.
    labels_col = True
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(_sql(True)), params)
            rows = result.fetchall()
    except Exception:
        labels_col = False
        try:
            async with get_search_db() as db:
                if db is None:
                    return []
                result = await db.execute(text(_sql(False)), params)
                rows = result.fetchall()
        except Exception as e:
            # Covers BOTH the pre-existing trigram-migration-missing case AND a
            # not-yet-migrated venues.photo_url column (20260710_index_enrich.sql)
            # — either way, graceful degradation to the pure-vector leg (no
            # photo_url, no trigram fusion) rather than a 500.
            logger.warning(f"[unified/venues] hybrid query failed ({e}); falling back to pure vector")
            return await _leg_venues_vector_only(qvec_lit, limit, lat, lng, radius_m, year_from, year_to, soft_radius=soft_radius)

    # Column layout: 13 fixed (0-12), then category_labels when present, then
    # dist_m when a location was supplied. Indices shift with labels_col.
    labels_idx = 13 if labels_col else None
    dist_idx = 14 if labels_col else 13

    hits = []
    for r in rows:
        hits.append({
            "type": "venue",
            "id": r[0],
            "bin": str(r[6]).replace(".0", "") if r[6] else None,
            "bbl": str(r[7]).replace(".0", "") if r[7] else None,
            "name": r[1],
            "snippet": r[3],
            "year": r[8],
            "style": r[9],
            "category": r[2],
            "landmark": None,
            "lat": r[4],
            "lng": r[5],
            "score": float(r[11]) if r[11] is not None else 0.0,
            "matched_field": (
                infer_matched_field(q_lex, name=r[1], style=r[9], category=r[2])
                if (r[12] or 0) > 0.5 else "semantic"
            ),
            "dist_m": (
                round(float(r[dist_idx]), 1)
                if geo and len(r) > dist_idx and r[dist_idx] is not None else None
            ),
            "category_labels": (
                list(r[labels_idx])
                if labels_idx is not None and len(r) > labels_idx and r[labels_idx] else None
            ),
            "photo_url": r[10],
            "lore_status": None,
        })
    return hits


async def _leg_venues_vector_only(
    qvec_lit: str, limit: int,
    lat: Optional[float], lng: Optional[float], radius_m: Optional[float],
    year_from: Optional[int], year_to: Optional[int],
    soft_radius: bool = False,
) -> List[dict]:
    """Fallback path if the hybrid trigram migration hasn't run (pg_trgm/index
    missing) — pure vector, matching the pre-existing /search/venues shape."""
    params: dict = {"qvec": qvec_lit, "limit": limit}
    filters: List[str] = []
    if year_from is not None:
        filters.append("building_year >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        filters.append("building_year <= :year_to")
        params["year_to"] = year_to
    geo = lat is not None and lng is not None
    if geo:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        if radius_m is not None and not soft_radius:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
        SELECT fsq_id, name, category, snippet,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score,
               lat, lng, bin, bbl, building_year, building_style
        FROM venues {where}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    """
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[unified/venues] vector-only fallback failed: {e}", exc_info=True)
        return []
    return [
        {
            "type": "venue", "id": r[0], "bin": str(r[7]).replace(".0", "") if r[7] else None,
            "bbl": str(r[8]).replace(".0", "") if r[8] else None, "name": r[1], "snippet": r[3],
            "year": r[9], "style": r[10], "category": r[2], "landmark": None,
            "lat": r[5], "lng": r[6], "score": float(r[4]) if r[4] is not None else 0.0,
            "matched_field": "semantic", "dist_m": None, "photo_url": None, "lore_status": None,
        }
        for r in rows
    ]


def _layer_matched_field(q_lex: str, *, title: Optional[str], category: Optional[str]) -> str:
    """Layers leg equivalent of infer_matched_field, but labels a title hit
    "title" (not "name") to match this corpus's existing field name, and
    falls back to "title" (not "semantic") on no-overlap since this branch
    only runs when the lex_score already cleared 0.5 — some field DID match,
    title is just the best guess when token attribution is ambiguous."""
    label = infer_matched_field(q_lex, name=title, category=category, default="title")
    return "title" if label == "name" else label


async def _leg_layers(
    qvec_lit: str, q_lex: str, limit: int,
    lat: Optional[float], lng: Optional[float], radius_m: Optional[float],
    layer: Optional[str],
    soft_radius: bool = False,
) -> List[dict]:
    """Lore/plaque/contribution leg. `layer_search_index.category` doubles as
    both a lore category and lore_status carrier in some ingests — inspected
    the migration (20260617_layers.sql) and it has no dedicated lore_status
    column, so lore_status is populated from `category` only when it looks
    like a status token (extant/demolished/unbuilt/transformed per
    forgotten_city_layer memory); otherwise left null rather than guessed.

    soft_radius: see _leg_buildings' docstring."""
    params: dict = {"qvec": qvec_lit, "limit": limit, "q_lex": q_lex}
    filters: List[str] = []
    if layer:
        filters.append("layer = :layer")
        params["layer"] = layer

    geo = lat is not None and lng is not None
    if geo:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        if radius_m is not None and not soft_radius:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    pool = min(max(limit * 4, 40), 200)
    params["pool"] = pool
    params["lex_floor"] = LEX_FLOOR
    fused = "(0.7 * (1 - (l.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex)"
    haversine_l = ""
    if geo:
        haversine_l = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(l.lat)) * cos(radians(l.lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(l.lat)))))"
        )

    # Word-boundary guard — see _word_boundary_pattern's docstring.
    word_boundary = _word_boundary_pattern(q_lex)
    lex_word_boundary_clause = ""
    if word_boundary:
        params["lex_wb"] = word_boundary
        lex_word_boundary_clause = "AND lower(coalesce(title,'') || ' ' || coalesce(snippet,'')) ~ :lex_wb"

    sql = f"""
        WITH vec_pool AS (
            SELECT id FROM layer_search_index {where}
            ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :pool
        ),
        lex_pool AS (
            SELECT id FROM layer_search_index
            {where + (' AND ' if where else 'WHERE ')}
            word_similarity(lower(:q_lex), lower(coalesce(title,'') || ' ' || coalesce(snippet,''))) > :lex_floor
            {lex_word_boundary_clause}
            ORDER BY word_similarity(lower(:q_lex), lower(coalesce(title,'') || ' ' || coalesce(snippet,''))) DESC
            LIMIT :pool
        ),
        pool AS (
            SELECT id FROM vec_pool UNION SELECT id FROM lex_pool
        )
        SELECT l.id, l.layer, l.title, l.snippet, l.lat, l.lng, l.year, l.category,
               l.lore_status, l.photo_url,
               {fused} AS score, wl.lex AS lex_score
               {(', ' + haversine_l + ' AS dist_m') if geo else ''}
        FROM layer_search_index l
        JOIN pool USING (id)
        CROSS JOIN LATERAL (
            SELECT word_similarity(lower(:q_lex), lower(coalesce(l.title,'') || ' ' || coalesce(l.snippet,''))) AS lex
        ) wl
        ORDER BY score DESC
        LIMIT :limit
    """
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        # Covers the pre-existing trigram-migration-missing case AND a
        # not-yet-migrated lore_status/photo_url column (20260710_index_enrich.sql).
        logger.warning(f"[unified/layers] hybrid query failed ({e}); falling back to pure vector")
        return await _leg_layers_vector_only(qvec_lit, limit, lat, lng, radius_m, layer, soft_radius=soft_radius)

    _STATUS_TOKENS = {"extant", "demolished", "unbuilt", "transformed"}
    hits = []
    for r in rows:
        category = r[7]
        # Prefer the new lore_status column when the backfill has populated it;
        # fall back to the old category-token heuristic for rows ingested
        # before 20260710_index_enrich.sql / the updated embed_layers.py ran.
        lore_status = r[8] or (category if (category and category.lower() in _STATUS_TOKENS) else None)
        layer_val = r[1]
        hit_type = layer_val if layer_val in ("lore", "plaque", "contribution") else "lore"
        hits.append({
            "type": hit_type,
            "id": r[0],
            "bin": None,
            "bbl": None,
            "name": r[2],
            "snippet": r[3],
            "year": r[6],
            "style": None,
            "category": category,
            "landmark": None,
            "lat": r[4],
            "lng": r[5],
            "score": float(r[10]) if r[10] is not None else 0.0,
            "matched_field": (
                _layer_matched_field(q_lex, title=r[2], category=category)
                if (r[11] or 0) > 0.5 else "semantic"
            ),
            "dist_m": round(float(r[12]), 1) if geo and len(r) > 12 and r[12] is not None else None,
            "photo_url": r[9],
            "lore_status": lore_status,
        })
    return hits


async def _leg_layers_vector_only(
    qvec_lit: str, limit: int,
    lat: Optional[float], lng: Optional[float], radius_m: Optional[float],
    layer: Optional[str],
    soft_radius: bool = False,
) -> List[dict]:
    params: dict = {"qvec": qvec_lit, "limit": limit}
    filters: List[str] = []
    if layer:
        filters.append("layer = :layer")
        params["layer"] = layer
    geo = lat is not None and lng is not None
    if geo:
        params["lat"] = lat
        params["lng"] = lng
        haversine = (
            "6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(lat)) * cos(radians(lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(lat)))))"
        )
        if radius_m is not None and not soft_radius:
            params["radius_m"] = radius_m
            filters.append(f"lat IS NOT NULL AND lng IS NOT NULL AND {haversine} <= :radius_m")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"""
        SELECT id, layer, title, snippet,
               1 - (embedding <=> CAST(:qvec AS vector)) AS score,
               lat, lng, year, category
        FROM layer_search_index {where}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    """
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(sql), params)
            rows = result.fetchall()
    except Exception as e:
        logger.error(f"[unified/layers] vector-only fallback failed: {e}", exc_info=True)
        return []
    _STATUS_TOKENS = {"extant", "demolished", "unbuilt", "transformed"}
    out = []
    for r in rows:
        category = r[8]
        lore_status = category if (category and category.lower() in _STATUS_TOKENS) else None
        layer_val = r[1]
        hit_type = layer_val if layer_val in ("lore", "plaque", "contribution") else "lore"
        out.append({
            "type": hit_type, "id": r[0], "bin": None, "bbl": None, "name": r[2], "snippet": r[3],
            "year": r[7], "style": None, "category": category, "landmark": None,
            "lat": r[5], "lng": r[6], "score": float(r[4]) if r[4] is not None else 0.0,
            "matched_field": "semantic", "dist_m": None, "photo_url": None, "lore_status": lore_status,
        })
    return out


# ---------------------------------------------------------------------------
# Query-interpretation cache — prose-intent only, OFF the response critical
# path. Synchronous cache CHECK (fast SELECT); on miss we return without it
# and write the interpretation in a background task so the NEXT identical
# query benefits. See services/openai_text.py::openai_text.
# ---------------------------------------------------------------------------

async def _get_cached_interpretation(q: str) -> Optional[dict]:
    try:
        async with get_search_db() as db:
            if db is None:
                return None
            result = await db.execute(
                text("SELECT interpretation FROM search_interpretation_cache WHERE query = :q"),
                {"q": q.strip().lower()},
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.info(f"[unified] interpretation cache check skipped: {e}")
        return None


async def _refine_and_cache_interpretation(q: str) -> None:
    """Background task: expand era/style hints for a prose query, then cache the
    interpretation for next time. Never raises into the caller (it's
    fire-and-forget via asyncio.create_task)."""
    try:
        # 600, not the old 150. `max_output_tokens` on a reasoning model is a
        # budget the reasoning pass draws from FIRST, so a tight cap truncates
        # the JSON — and truncated JSON fails the parse below and is discarded,
        # meaning the cache would simply never populate and every query would
        # pay the miss forever. The answer here is ~50 tokens; the headroom is
        # what guarantees it survives.
        raw = await openai_text(
            system=(
                "You expand a natural-language NYC-architecture search query into "
                "structured filter hints. Reply with compact JSON only: "
                '{"style_terms": [...], "year_from": int|null, "year_to": int|null}. '
                "No prose, no markdown fences."
            ),
            user=q,
            max_tokens=600,
            cache_key="jink-search-interp",
        )
        if not raw:
            return
        import json
        try:
            interpretation = json.loads(raw)
        except Exception:
            logger.info(f"[unified] interpretation not valid JSON, discarding: {raw[:120]}")
            return

        async with get_search_db() as db:
            if db is None:
                return
            await db.execute(
                text(
                    "INSERT INTO search_interpretation_cache (query, interpretation) "
                    "VALUES (:q, CAST(:interp AS jsonb)) "
                    "ON CONFLICT (query) DO UPDATE SET interpretation = EXCLUDED.interpretation, created_at = now()"
                ),
                {"q": q.strip().lower(), "interp": json.dumps(interpretation)},
            )
            await db.commit()
    except Exception as e:
        logger.info(f"[unified] background interpretation refine skipped: {e}")


async def _log_query(q: str, intent: str, latency_ms: float, result_ids: List[str]) -> None:
    """Best-effort query log. Never raises into the caller."""
    try:
        async with get_search_db() as db:
            if db is None:
                return
            import json
            await db.execute(
                text(
                    "INSERT INTO search_query_log (query, intent, latency_ms, result_ids) "
                    "VALUES (:q, :intent, :latency_ms, CAST(:result_ids AS jsonb))"
                ),
                {"q": q, "intent": intent, "latency_ms": latency_ms, "result_ids": json.dumps(result_ids)},
            )
            await db.commit()
    except Exception as e:
        logger.info(f"[unified] query log skipped: {e}")


# ---------------------------------------------------------------------------
# Unified result cache — in-process TTL cache so a repeat search in the same
# area (the common "search, pan a little, search again" flow) skips retrieval
# entirely. Location is quantized to ~500m cells so tiny GPS drift still hits.
# TTL is short (5 min): the index only changes at re-ingest, but scanned_bins /
# user_vector personalization can change mid-session.
# ---------------------------------------------------------------------------

_RESULT_CACHE_TTL_S = 300.0
_RESULT_CACHE_MAX = 256
_result_cache: "OrderedDict[tuple, tuple]" = OrderedDict()  # key -> (ts, response)


def _result_cache_key(q: str, lat, lng, radius_m, limit, filters: tuple) -> tuple:
    # ~0.005° ≈ 550m N-S; coarse enough that walking a block still hits.
    qlat = round(lat / 0.005) if lat is not None else None
    qlng = round(lng / 0.005) if lng is not None else None
    return (q.strip().lower(), qlat, qlng, radius_m, limit) + filters


def _result_cache_get(key: tuple):
    entry = _result_cache.get(key)
    if entry is None:
        return None
    ts, resp = entry
    if (time.monotonic() - ts) > _RESULT_CACHE_TTL_S:
        _result_cache.pop(key, None)
        return None
    _result_cache.move_to_end(key)
    return resp


def _result_cache_put(key: tuple, resp: Dict[str, Any]) -> None:
    _result_cache[key] = (time.monotonic(), resp)
    _result_cache.move_to_end(key)
    while len(_result_cache) > _RESULT_CACHE_MAX:
        _result_cache.popitem(last=False)


@router.get("/unified")
@limiter.limit(LIMIT_SEARCH)
async def search_unified(
    request: Request,
    q: str = Query(..., description="Natural-language search query"),
    lat: Optional[float] = Query(None),
    lng: Optional[float] = Query(None),
    radius_m: Optional[float] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    year_from: Optional[int] = Query(None),
    year_to: Optional[int] = Query(None),
    borough: Optional[str] = Query(None, description="Filters buildings leg by borough (real column post-migration; no-op on venues/layers)"),
    style_family: Optional[str] = Query(None, description="Substring-matched against style text (real column on buildings post-migration; best-effort on venues)"),
    material: Optional[str] = Query(None, description="Filters buildings leg by material (real column post-migration; no-op on venues/layers)"),
    lore_status: Optional[str] = Query(None, description="Filters layers leg by the lore_status column when populated, else by category token heuristic"),
    landmark: Optional[bool] = Query(None, description="Filters buildings leg by is_landmark"),
    user_vector: Optional[str] = Query(None, description="9 comma-separated floats: user's aesthetic archetype vector"),
    scanned_bins: Optional[str] = Query(None, description="CSV of bins the user has already scanned (novelty nudge)"),
) -> Dict[str, Any]:
    """Cross-corpus search: buildings + venues + layers (lore/plaques/
    contributions), fused via Reciprocal Rank Fusion with intent-aware corpus
    weights. See services/unified_search.py for the pure logic. Contract is
    pinned — see task spec; do not change response shape without updating the
    iOS client in lockstep.

    borough/material/style_family filter against building_search_index's
    enriched columns (20260710_index_enrich.sql) when that migration has run;
    until then (or for venues, which has no borough/material column) they
    degrade to a best-effort substring match against the parsed style text /
    become no-ops — see _leg_buildings' enriched/fallback SQL branches and the
    post-fetch filters below. style_family is always substring-matched (no
    normalized taxonomy exists), never exact-match.
    """
    start = time.monotonic()
    cache_key = _result_cache_key(
        q, lat, lng, radius_m, limit,
        (year_from, year_to, borough, style_family, material, lore_status,
         landmark, user_vector, scanned_bins),
    )
    cached_resp = _result_cache_get(cache_key)
    if cached_resp is not None:
        return cached_resp

    intent, poi_noun = classify_intent_detailed(q)
    weights = corpus_weights(intent)
    # HARD_RADIUS_INTENTS (poi/name/address/event): radius_m stays a hard
    # WHERE filter — "near me" is core to those queries. Everything else
    # (style/architect/lore/prose) treats radius as a soft proximity signal
    # only, so a better match further away can still surface.
    soft_radius = intent not in HARD_RADIUS_INTENTS

    try:
        # to_thread: the ONNX forward pass is sync CPU work — off the event
        # loop so concurrent searches don't serialize behind it. Cached per
        # query string inside embed_query, so repeats return instantly.
        qvec = await asyncio.to_thread(embed_query, q)
    except Exception as e:
        logger.error(f"[unified] query embedding failed: {e}", exc_info=True)
        return {"intent": intent, "header": "No results", "facets": [], "hits": []}
    qvec_lit = _vec_literal(qvec)
    q_lex = _lexical_query(q)

    leg_limit = max(limit, 20)
    layer_filter = None
    if lore_status and lore_status.lower() in {"extant", "demolished", "unbuilt", "transformed"}:
        layer_filter = None  # lore_status filters layers AFTER retrieval below, not via `layer` column

    # Personalization vector — parsed BEFORE the legs run so it can be pushed
    # into _leg_buildings' SQL (dot product computed in Postgres via pgvector's
    # <#> operator against building_search_index.profile). Buildings-only:
    # venues/layers carry no aesthetic profile column in this DB.
    user_vec_lit: Optional[str] = None
    if user_vector:
        try:
            parts = [float(x) for x in user_vector.split(",")]
            if len(parts) == 9:
                user_vec_lit = "[" + ",".join(f"{x:.6f}" for x in parts) + "]"
        except Exception:
            user_vec_lit = None

    buildings_task = _leg_buildings(
        qvec_lit, q_lex, leg_limit, lat, lng, radius_m, year_from, year_to,
        borough=borough, material=material, style_family=style_family,
        user_vec_lit=user_vec_lit, soft_radius=soft_radius,
        fame_weight=W_LEG_FAME if intent in FAME_BOOST_INTENTS else 0.0,
    )
    venues_task = _leg_venues(qvec_lit, q_lex, leg_limit, lat, lng, radius_m, year_from, year_to, soft_radius=soft_radius)
    layers_task = _leg_layers(qvec_lit, q_lex, leg_limit, lat, lng, radius_m, layer_filter, soft_radius=soft_radius)

    buildings_hits, venues_hits, layers_hits = await asyncio.gather(
        buildings_task, venues_task, layers_task, return_exceptions=False
    )

    if landmark is not None:
        buildings_hits = [h for h in buildings_hits if h.get("landmark") == landmark]
    if style_family:
        # Applied again here (belt-and-suspenders): the SQL leg already filters
        # buildings via style_family when the enriched columns exist; this
        # post-filter is what actually does the work for venues (no
        # style_family column on `venues` — best-effort substring match against
        # the parsed style text) and is a harmless no-op re-check for buildings.
        needle = style_family.replace("_", " ").lower()
        buildings_hits = [h for h in buildings_hits if h.get("style") and needle in h["style"].lower()]
        venues_hits = [h for h in venues_hits if h.get("style") and needle in h["style"].lower()]
    if borough:
        # No borough column on venues/layers in this DB — buildings-only filter
        # (SQL leg already applied it when enriched; this re-check is a no-op
        # there and harmless).
        buildings_hits = [h for h in buildings_hits if not h.get("borough") or h["borough"].lower() == borough.lower()]
    if material:
        buildings_hits = [h for h in buildings_hits if not h.get("material") or h["material"].lower() == material.lower()]
    if lore_status:
        layers_hits = [h for h in layers_hits if h.get("lore_status") == lore_status]

    if intent == "poi" and poi_noun:
        # Category-family rank adjustment (poi_category_adjustment, see
        # unified_search.py): a mild boost when the venue's category matches
        # the detected POI noun's family (bar/pub/lounge/... for "bar"), mild
        # demotion for a category that's clearly a DIFFERENT, unrelated
        # family (Antique Store / Art Gallery for a "bar" query). Never a
        # hard filter — applied to `score` BEFORE re-sorting, so it also
        # shifts each venue's rank within its own corpus (which RRF then
        # reads), not just the final fused score.
        style_toks = query_style_tokens(q, poi_noun)
        for h in venues_hits:
            adj = poi_category_adjustment(h.get("category"), poi_noun, h.get("category_labels"))
            # Host-building style/era affinity — "art deco bar" boosts bars
            # inside deco (or deco-era) buildings; the venue row already
            # carries building_style/building_year, previously unscored.
            adj += venue_style_affinity(style_toks, h.get("style"), h.get("year"))
            # Style words in a venue NAME ("High Style Deco", antique store)
            # are a lexical decoy, not noun relevance — penalized unless the
            # category really is in the noun's family.
            adj += style_name_decoy_penalty(q_lex, h.get("name"), h.get("category"), poi_noun)
            # Applied twice, on two different scales: here on the raw leg
            # score (reorders the leg, which RRF reads as rank) AND stashed
            # for the post-RRF nudge pass. Post-RRF_SCALE one rank step is
            # ~0.016, so a ±0.15 category adjustment moves a hit ~9 ranks —
            # decisive enough to bury the antique store, without the old
            # behaviour where it silently outweighed the entire relevance
            # range.
            h["poi_adj"] = adj
            h["score"] = (h.get("score") or 0.0) + adj
        venues_hits.sort(key=lambda h: h.get("score") or 0.0, reverse=True)

    # Build RankedHit lists (rank = position in each corpus's own score order;
    # legs already ORDER BY score DESC in SQL).
    legs = {
        "buildings": [RankedHit("buildings", h["id"], i + 1, h) for i, h in enumerate(buildings_hits) if h.get("id")],
        "venues": [RankedHit("venues", h["id"], i + 1, h) for i, h in enumerate(venues_hits) if h.get("id")],
        "layers": [RankedHit("layers", h["id"], i + 1, h) for i, h in enumerate(layers_hits) if h.get("id")],
    }
    fused = reciprocal_rank_fusion(legs, weights)

    scanned = set()
    if scanned_bins:
        scanned = {b.strip() for b in scanned_bins.split(",") if b.strip()}

    # Build the FULL nudged/scored list first (not truncated to `limit` yet):
    # dedupe_near_identical needs to see the whole candidate set to find
    # near-duplicate clusters, and truncating before dedup could keep two
    # duplicates while dropping a genuinely-different lower-ranked hit.
    all_hits: List[Dict[str, Any]] = []
    for gk, score, ranked_hit in fused:
        h = dict(ranked_hit.payload)
        # personalization_dot is set only on buildings hits, only when the
        # enriched `profile` column exists AND a user_vector param was passed
        # (see _leg_buildings' b_personalization SELECT) — None otherwise, in
        # which case apply_nudges skips the term cleanly.
        personalization_dot = h.get("personalization_dot")
        is_novel = h.get("bin") not in scanned if (scanned and h.get("bin")) else None
        # RRF_SCALE lifts the fused score (range 0–0.0164) onto the same 0–1
        # scale the nudges below use, so they tie-break instead of dominating.
        nudged = apply_nudges(
            score * RRF_SCALE,
            personalization_dot=personalization_dot,
            dist_m=h.get("dist_m"),
            is_novel=is_novel,
        )
        # Soft-radius proximity: for style/architect/lore/prose intents,
        # radius_m was never applied as a WHERE filter (see soft_radius
        # above), so dist_m may be large or None — add a decaying bonus
        # instead of a cutoff, only when the caller actually supplied a
        # location (dist_m is only ever populated when lat/lng were given).
        if soft_radius and h.get("dist_m") is not None:
            nudged += proximity_decay_bonus(h.get("dist_m"))
        # Landmark/fame boost: buildings only, only on intents where fame
        # should break ties (see FAME_BOOST_INTENTS) — lets an icon-tier
        # building (Chrysler etc.) beat an obscure same-style row house.
        nudged += fame_boost(intent, h.get("fame"))
        # Name intent: an exact/subset name match is the answer — this bonus
        # is deliberately dominant over every other nudge (see W_EXACT_NAME).
        if intent == "name":
            nudged += exact_name_bonus(q_lex, h.get("name"))
        # Architect intent: a real column match is the answer, same standing as
        # an exact name match. Previously this intent had no structured field
        # to score against at all.
        if intent == "architect":
            nudged += architect_match_bonus(q_lex, h.get("architect"))
        # House-number address queries ("469 broome") classify as name/address
        # but their number is the whole signal — a dominant bonus when a
        # building's address range contains it, so the exact address beats fame
        # (570 Broome was outranking 469-475). Buildings only.
        if intent in ("name", "address") and h.get("type") == "building":
            nudged += house_number_bonus(q_lex, h.get("name"), h.get("snippet"))
        # POI adjustments re-applied on the RRF scale (see the venues
        # re-score block above for why twice).
        nudged += h.get("poi_adj") or 0.0
        # Lore/event multi-token queries: reward full concept coverage
        # ("demolished" AND "theaters"), demote single-concept matches.
        nudged += coverage_adjustment(
            intent, q_lex, h.get("name"), h.get("snippet"),
            h.get("category"), h.get("style"),
        )
        # Hedged style attributions ("… colonial revival OR art deco") were
        # scoring as confident matches because trigram similarity reads the
        # best-matching substring. Discount a match that only lands on the
        # alternative, never on the primary.
        nudged += hedged_style_penalty(
            query_style_tokens(q, poi_noun), h.get("style")
        )
        why = build_why(
            matched_field=h.get("matched_field"),
            year=h.get("year"),
            style=h.get("style"),
            category=h.get("category"),
            fallback_snippet=h.get("snippet"),
        )
        all_hits.append({
            "type": h.get("type"),
            "id": h.get("id"),
            "score": round(float(nudged), 5),
            "name": h.get("name"),
            "why": why,
            "year": h.get("year"),
            "style": h.get("style"),
            "category": h.get("category"),
            "lat": h.get("lat"),
            "lng": h.get("lng"),
            "dist_m": h.get("dist_m"),
            "photo_url": h.get("photo_url"),
            "bin": h.get("bin"),
            "bbl": h.get("bbl"),
            "lore_status": h.get("lore_status"),
            "snippet": h.get("snippet"),
        })

    # Re-sort after the soft-radius/landmark nudges (both applied AFTER the
    # RRF-order `fused` list was built, so they can reorder within it), then
    # dedupe near-identical hits (e.g. repeated "Court Name: Roosevelt" rows
    # across adjacent BINs of one development — same name, <150m apart),
    # THEN truncate to `limit`.
    all_hits.sort(key=lambda h: h["score"], reverse=True)
    deduped = dedupe_near_identical(all_hits)
    # Floor BEFORE the limit: `limit` is a ceiling on how many good results to
    # show, not a quota to fill with noise.
    hits = apply_relevance_floor(deduped)[:limit]

    header = build_header(hits, intent)
    facets = build_facets({
        "style": sorted({h["style"] for h in hits if h.get("style")}),
        "lore_status": sorted({h["lore_status"] for h in hits if h.get("lore_status")}),
    })
    facets = [f for f in facets if f["kind"] == "style" or f["kind"] == "lore_status"]
    # Adjust param names to the documented filter params.
    for f in facets:
        if f["kind"] == "style":
            f["param"] = "style_family"
        elif f["kind"] == "lore_status":
            f["param"] = "lore_status"

    interpretation_used = False
    if intent == "prose":
        cached = await _get_cached_interpretation(q)
        if cached is None:
            asyncio.create_task(_refine_and_cache_interpretation(q))
        else:
            interpretation_used = True  # available for a future refinement pass; response shape is pinned

    latency_ms = (time.monotonic() - start) * 1000
    asyncio.create_task(_log_query(q, intent, latency_ms, [h["id"] for h in hits if h.get("id")]))

    resp = {
        "intent": intent,
        "header": header,
        "facets": facets,
        "hits": hits,
    }
    _result_cache_put(cache_key, resp)
    return resp


# ---------------------------------------------------------------------------
# Facets — DB-derived filter options, cached in-process for 1h.
# ---------------------------------------------------------------------------

_facets_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_FACETS_TTL_S = 3600.0


@router.get("/facets")
@limiter.limit(LIMIT_SEARCH)
async def search_facets(request: Request) -> Dict[str, Any]:
    """Filter options derived from DISTINCT queries over the search index —
    never a hardcoded list. Returns whatever columns actually exist (style is
    parsed from `snippet` text, not a normalized column — see _leg_buildings).
    Cached in-process for 1h since these values change slowly (only at
    re-ingest)."""
    now = time.monotonic()
    if _facets_cache["data"] is not None and (now - _facets_cache["ts"]) < _FACETS_TTL_S:
        return _facets_cache["data"]

    result: Dict[str, Any] = {
        "style_family": [],
        "lore_status": [],
        "year_min": None,
        "year_max": None,
        "landmark": [True, False],
    }
    try:
        async with get_search_db() as db:
            if db is None:
                return result

            # Styles: parse the part after the em-dash in `snippet`, DISTINCT,
            # non-empty. No normalized style_family column exists in this DB.
            styles_result = await db.execute(text(
                "SELECT DISTINCT trim(split_part(snippet, '—', 2)) AS style "
                "FROM building_search_index "
                "WHERE snippet LIKE '%—%' AND trim(split_part(snippet, '—', 2)) <> '' "
                "LIMIT 200"
            ))
            result["style_family"] = sorted({r[0] for r in styles_result.fetchall() if r[0]})

            years_result = await db.execute(text(
                "SELECT min(year_built), max(year_built) FROM building_search_index WHERE year_built IS NOT NULL"
            ))
            yr = years_result.fetchone()
            if yr:
                result["year_min"], result["year_max"] = yr[0], yr[1]

            # lore_status: only the four status tokens we know are meaningful
            # (see forgotten_city_layer memory), derived from actual category
            # values present — not hardcoded as an assumed list.
            status_result = await db.execute(text(
                "SELECT DISTINCT category FROM layer_search_index "
                "WHERE lower(category) IN ('extant','demolished','unbuilt','transformed')"
            ))
            result["lore_status"] = sorted({r[0] for r in status_result.fetchall() if r[0]})
    except Exception as e:
        logger.warning(f"[facets] query failed, returning partial/empty facets: {e}")

    _facets_cache["data"] = result
    _facets_cache["ts"] = now
    return result


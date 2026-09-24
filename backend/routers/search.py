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
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Query, Request
from sqlalchemy import bindparam, text

from models.search_session import get_search_db
from services.text_embeddings import embed_query
from utils.rate_limit import limiter, LIMIT_SEARCH
from services.openai_text import openai_text
from services.unified_search import (
    HARD_RADIUS_INTENTS,
    facet_adjustments,
    aesthetic_match_bonus,
    layer_title_bonus,
    apply_diversity_cap,
    fuzzy_name_bonus,
    LORE_SIM_FLOOR,
    LORE_LEX_FLOOR,
    leg_lore_weight,
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
    INTERP_VERSION,
    INTERP_WAIT_S,
    MAX_EXPANSION_QUERIES,
    W_EXPANSION_LEG,
    W_ORIGINAL_WHEN_EXPANDED,
    has_direct_match,
    about_weights,
    llm_style_bonus,
    llm_era_bonus,
    spelling_correction,
    MIN_LOCAL_MATCHES,
    order_by_tier,
    query_content_tokens,
    tier_of,
    resolve_entity_mode,
    carries_name,
    evidence_why,
    dedupe_same_place,
    token_coverage,
    llm_category_bonus,
    parse_interpretation,
    place_adjustment,
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
    # "spot"/"spots" belongs with "place"/"places": it is how people say
    # "somewhere", not a thing in the corpus. Left out, "spooky spots" matched
    # "Lily Spots Llc", "Five Spot Cafe" and "The Picnic Ba Ket" (Sandwich
    # Spot) on the literal token while the word that carried the intent,
    # "spooky", did the work alone.
    "spot", "spots",
    # Question words. "where did famous writers live" kept "where" and "did",
    # and long report chunks contain every one of those words, so apartment
    # houses counted as real matches for it.
    "where", "what", "which", "who", "whose", "when", "why", "how", "did",
    "does", "do", "can", "could", "should", "would", "was", "there", "about",
})


def _sanitize_query(q: str) -> str:
    """Strip bytes Postgres cannot accept in a text parameter.

    A NUL is rejected outright by psycopg when binding a text value, so it
    fails BEFORE the query runs -- every leg raises, and because each leg
    swallows its own errors (a broken leg must not break the others) the user
    just gets an empty result set with nothing in the logs to explain it.

    A client can send one: %00 in a URL decodes to NUL and renders as nothing
    in most UIs, so it is invisible in a bug report.

    This is defence in depth, not the fix for the outage of 2026-09-22 -- that
    NUL was server-side, a sentinel this module passed in params["aesth_toks"],
    and never touched `q`. Sanitizing input would not have caught it.
    """
    if not q:
        return q
    return q.replace("\x00", "").replace("\r", " ").strip()


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
                similarity(lower(:q_lex), lower(b.text)),
                word_similarity(lower(:q_lex), lower(coalesce(b.material_text, ''))),
                similarity(lower(:q_lex), lower(coalesce(b.name_norm, ''))),
                word_similarity(lower(:q_lex), lower(coalesce(b.neighborhood_text, '')))
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
        "cats": tuple(c.lower() for c in cats),
    }
    where = [
        "lat IS NOT NULL",
        "lng IS NOT NULL",
        "lower(category) IN :cats",
        f"{haversine} <= :radius_m",
        # Hide NJ rows and non-places; see scripts/enrich_venues.py.
        "searchable IS NOT FALSE",
    ]
    if require_building:
        where.append("bin IS NOT NULL")

    # `expanding` renders the tuple as a proper IN-list at execution time. A
    # bare `IN :cats` would bind the tuple as one scalar and match nothing, and
    # `= ANY(:cats)` depends on the driver expanding a list into an array —
    # true for asyncpg, not a property worth relying on silently.
    sql = text(f"""
        SELECT fsq_id, name, category, snippet, lat, lng,
               bin, bbl, building_year, building_style,
               {haversine} AS dist_m
        FROM venues
        WHERE {" AND ".join(where)}
        ORDER BY dist_m ASC
        LIMIT :limit
    """).bindparams(bindparam("cats", expanding=True))

    try:
        async with get_search_db() as db:
            if db is None:
                logger.warning("[venues/nearby] search DB not configured (SEARCH_DB_URL)")
                return []
            result = await db.execute(sql, params)
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
        WHERE category IS NOT NULL AND searchable IS NOT FALSE
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
    filters: List[str] = ["searchable IS NOT FALSE"]

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
    filters: List[str] = ["in_nyc IS NOT FALSE"]

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

# ---------------------------------------------------------------------------
# Closed vocabularies used to GATE the material / neighborhood pools.
#
# Both pools run a per-row subquery no index can serve, so running one that
# cannot match costs a full seq scan for zero rows. These sets are small and
# fixed (20 distinct materials, 197 NTA names), so a query token either is one
# of them or it isn't -- no heuristic involved.
#
# Loaded once per process on first use, directly off the index. Empty set on
# failure means the gate opens and behaviour is exactly as before.
# ---------------------------------------------------------------------------

_MATERIAL_VOCAB: Optional[set] = None
_HOOD_VOCAB: Optional[set] = None
_AESTH_VOCAB: Optional[set] = None


def _load_vocab(column: str) -> set:
    import psycopg  # sync, one-shot, at import-time cost only
    from models.config import get_settings
    url = get_settings().search_db_url
    if not url:
        return set()
    try:
        with psycopg.connect(url, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT lower(tok) FROM {column} src, "
                f"LATERAL unnest(regexp_split_to_array(src.v, '[^a-zA-Z]+')) tok "
                f"WHERE length(tok) >= 3"
            )
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning(f"[search] vocab load for {column} failed ({e}); gate opens")
        return set()


def aesthetic_vocab() -> set:
    global _AESTH_VOCAB
    if _AESTH_VOCAB is None:
        _AESTH_VOCAB = _load_vocab(
            "(SELECT DISTINCT aesthetic_text AS v FROM building_search_index "
            " WHERE aesthetic_text IS NOT NULL)")
    return _AESTH_VOCAB


def material_vocab() -> set:
    global _MATERIAL_VOCAB
    if _MATERIAL_VOCAB is None:
        _MATERIAL_VOCAB = _load_vocab(
            "(SELECT DISTINCT material_text AS v FROM building_search_index "
            " WHERE material_text IS NOT NULL)")
    return _MATERIAL_VOCAB


_HOOD_PHRASES: Optional[set] = None
_BOROUGH_NAMES = ("manhattan", "brooklyn", "queens", "bronx", "staten island")


def _neighborhood_phrases() -> set:
    """Whole neighborhood names a person would type, derived from the NTA
    names in the index: "Midtown South-Flatiron-Union Square" yields
    "midtown south", "flatiron", "union square". Parenthesised qualifiers
    ("Upper West Side (Central)") are dropped.

    Phrases, not tokens: the token vocabulary holds "club", "park", "east"
    and "city", and treating those as places would turn "night club near me"
    into a citywide search."""
    global _HOOD_PHRASES
    if _HOOD_PHRASES is None:
        import psycopg
        from models.config import get_settings
        phrases: set = set()
        url = get_settings().search_db_url
        try:
            if url:
                with psycopg.connect(url, connect_timeout=5) as c, c.cursor() as cur:
                    cur.execute("SELECT DISTINCT neighborhood FROM building_search_index WHERE neighborhood IS NOT NULL")
                    for (n,) in cur.fetchall():
                        n = re.sub(r"\([^)]*\)", "", n)
                        for part in n.split("-"):
                            part = re.sub(r"\s+", " ", part.strip().lower().replace("'", ""))
                            if len(part) >= 4:
                                phrases.add(part)
        except Exception as e:
            logger.warning(f"[search] neighborhood phrase load failed ({e})")
        _HOOD_PHRASES = phrases
    return _HOOD_PHRASES


_GENERIC_VOCAB: Optional[set] = None


def generic_vocab() -> set:
    """Words that describe a KIND of thing, not a particular one: every venue
    category, style, archetype, material and neighborhood word in the data.
    A query word outside this set ("seagram", "chrysler") names something, and
    a result carrying it in its name is the thing itself. Derived from the
    corpus, so it grows with it."""
    global _GENERIC_VOCAB
    if _GENERIC_VOCAB is None:
        import psycopg
        from models.config import get_settings
        from services.unified_search import _field_tokens
        words: set = set()
        url = get_settings().search_db_url
        try:
            if url:
                with psycopg.connect(url, connect_timeout=5) as c, c.cursor() as cur:
                    cur.execute("SELECT DISTINCT category FROM venues WHERE searchable AND category IS NOT NULL")
                    for (v,) in cur.fetchall():
                        words |= _field_tokens(v)
                    cur.execute("SELECT DISTINCT style_family FROM building_search_index WHERE style_family IS NOT NULL "
                                "UNION SELECT DISTINCT style_primary FROM building_search_index WHERE style_primary IS NOT NULL")
                    for (v,) in cur.fetchall():
                        words |= _field_tokens(v)
        except Exception as e:
            logger.warning(f"[search] generic vocab load failed ({e})")
        for w in aesthetic_vocab() | material_vocab() | neighborhood_vocab():
            words |= _field_tokens(w)
        words |= _field_tokens("manhattan brooklyn queens bronx staten island building buildings")
        _GENERIC_VOCAB = words
    return _GENERIC_VOCAB


_PROPER_CACHE: "OrderedDict[tuple, bool]" = OrderedDict()
PROPER_NOUN_RATIO = 0.8
PROPER_NOUN_MIN_SEEN = 5


async def _proper_nouns(tokens: set, unseen_is_proper: bool = True) -> set:
    """The query words the corpus writes as proper nouns.

    Not being category/style vocabulary does not make a word a name:
    "haunted" is neither, and a restaurant called Haunted Manhattan was
    pinned as THE answer to "haunted". The designation reports settle it --
    measured 2026-09-22, the share of mentions that are capitalized:
    Chrysler 119/119, Seagram 106/107, Woolworth 189/194, Genovese 11/11,
    against haunted 1/8, murder 9/88, gargoyle 5/99, and the ambiguous tin
    143/300 (Tin Pan Alley) and grand 234/300 (Grand Street, grand stair).
    A word the reports never use is presumed a name: a bar's name usually
    appears nowhere in LPC prose."""
    out, todo = set(), []
    for t in tokens:
        key = (t, unseen_is_proper)
        if key in _PROPER_CACHE:
            if _PROPER_CACHE[key]:
                out.add(t)
        else:
            todo.append(t)
    if todo:
        try:
            async with get_search_db() as db:
                if db is not None:
                    rows = (await db.execute(text("""
                        SELECT t, count(*) FILTER (WHERE s.text ~ ('\\m' || initcap(t))), count(s.text)
                          FROM unnest(CAST(:toks AS text[])) t
                          LEFT JOIN LATERAL (
                                SELECT text FROM building_lore_index
                                 WHERE lower(text) ~ ('\\m' || t) LIMIT 300) s ON true
                         GROUP BY t
                    """), {"toks": todo})).fetchall()
                    for t, cap, tot in rows:
                        proper = ((cap / tot) >= PROPER_NOUN_RATIO if tot >= PROPER_NOUN_MIN_SEEN
                                  else unseen_is_proper)
                        _PROPER_CACHE[(t, unseen_is_proper)] = proper
                        if len(_PROPER_CACHE) > 5000:
                            _PROPER_CACHE.popitem(last=False)
                        if proper:
                            out.add(t)
        except Exception as e:
            logger.info(f"[unified] proper-noun check skipped: {e}")
            out |= set(todo)
    return out


async def _named_tokens(q_lex: str, q_toks: set) -> set:
    """Query words that name something: non-generic words the reports write
    capitalized, plus both words of any adjacent pair that is capitalized as
    a pair and has at least one non-generic word. "grand" alone is 78%
    capitalized ("grand staircase") and fails, but "Grand Central" is 296/300;
    "art deco" (299/300) is excluded because both words are style vocabulary."""
    from services.unified_search import _field_tokens
    gen = generic_vocab()
    named = await _proper_nouns({t for t in q_toks if t not in gen})
    words = [w for w in re.split(r"[^a-z0-9]+", q_lex.lower()) if len(w) >= 3]
    pairs = {f"{a} {b}": (a, b) for a, b in zip(words, words[1:])
             if not ({x for w in (a, b) for x in _field_tokens(w)} <= gen)}
    if pairs:
        # A pair must be SEEN capitalized: "seagram bar" appears nowhere, and
        # presuming it a name made "bar" a name word too.
        proper_pairs = await _proper_nouns(set(pairs), unseen_is_proper=False)
        for p in proper_pairs:
            for w in pairs[p]:
                named |= _field_tokens(w)
    return named


def _query_places(q: str) -> tuple:
    """(neighborhood phrases, boroughs) the query names, whole-word."""
    ql = " " + re.sub(r"[^a-z0-9]+", " ", q.lower().replace("'", "")) + " "
    hoods = [p for p in _neighborhood_phrases() if f" {p} " in ql]
    # "upper west side" also contains "west side"; keep the longest.
    hoods = [p for p in hoods if not any(p != o and p in o for o in hoods)]
    boros = [b for b in _BOROUGH_NAMES if f" {b} " in ql]
    return hoods, boros


def neighborhood_vocab() -> set:
    global _HOOD_VOCAB
    if _HOOD_VOCAB is None:
        _HOOD_VOCAB = _load_vocab(
            "(SELECT DISTINCT neighborhood_text AS v FROM building_search_index "
            " WHERE neighborhood_text IS NOT NULL)")
    return _HOOD_VOCAB


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
    lore_weight: float = 0.0,
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
    # LPC prose leg. lore_w is 0 for address/poi/event; lore_scan over-fetches
    # chunks because a BIN averages 3.4 of them, so N chunks yield ~N/3.4 BINs.
    params["lore_w"] = lore_weight
    params["lore_floor"] = LORE_SIM_FLOOR
    # pool, not pool*3: the outer DISTINCT keeps at most `pool` bins anyway,
    # and a wider scan only costs time. Measured on "stained glass windows":
    # 600 -> 2.44s/200 bins, 250 -> 1.57s/157 bins.
    params["lore_scan"] = pool
    # Conjunctive whole-word stems for the lore lexical prefilter; see
    # lore_lex_pool. Plural "s" trimmed so "gargoyles" also finds "gargoyle".
    _lore_stems = [
        (t[:-1] if len(t) > 4 and t.endswith("s") else t)
        for t in re.findall(r"[a-z0-9]+", q_lex.lower()) if len(t) >= 3
    ][:4]
    for i, st in enumerate(_lore_stems):
        params[f"lore_rx{i}"] = r"\m" + re.escape(st)
    lore_rx_sql = (
        " AND ".join(f"lower(text) ~ :lore_rx{i}" for i in range(len(_lore_stems)))
        if _lore_stems else "lower(:q_lex) <% lower(text)"
    )
    # The same test gates the per-chunk word_similarity in the lore LATERAL:
    # lore.lex only scores above LORE_LEX_FLOOR (0.6), which a chunk cannot
    # reach without containing the words, and word_similarity over long
    # report text was 7.2ms per BIN x 223 BINs = 1.6s of a 1.8s leg.
    lore_chunk_gate = (
        " AND ".join(f"lower(l.text) ~ :lore_rx{i}" for i in range(len(_lore_stems)))
        if _lore_stems else "TRUE"
    )
    # Higher than lex_floor: a neighborhood name is a short, distinctive
    # string, so a loose match here drags in a whole different part of the city.
    params["hood_floor"] = 0.6
    # Material and neighborhood are SHORT controlled strings, so they must be
    # matched per query TOKEN, not against the whole query. Measured on the
    # live index for "buildings with terracotta" vs "limestone and terra cotta
    # terracotta terra-cotta":
    #   word_similarity(whole query, material_text)         = 0.423 (< 0.45)
    #   strict_word_similarity('terracotta', material_text) = 1.000
    params["q_toks"] = [t for t in re.split(r"[^\w'-]+", q_lex.lower()) if len(t) >= 3]
    params["tok_floor"] = 0.7
    # Gate: skip the material and neighborhood pools when no query token could
    # possibly match one. Both run a per-row subquery that no index can serve
    # (~85ms and ~98ms of a seq scan), and for a query like "cocktail bar" they
    # scan the whole table to return zero rows. The vocabularies are small and
    # closed -- 20 material values, 197 NTA names -- so this is a set lookup,
    # not a heuristic.
    _toks = set(params["q_toks"])
    want_mat = bool(_toks & material_vocab())
    want_hood = bool(_toks & neighborhood_vocab())
    # The nine archetypes are a CLOSED set, so naming one is an exact
    # instruction ("show me the visionary ones"), not a similarity guess.
    # build_text already folds "{archetype} character" into the embedding, so
    # they were reachable semantically -- but with no structured column there
    # was no way to return precisely those and nothing else.
    want_aesth = bool(_toks & aesthetic_vocab())
    # Sentinel must be a VALID text value. '\x00' is not: Postgres rejects NUL
    # bytes in text outright, so passing one made the whole buildings query
    # raise, both the enriched and fallback branches fail, and the leg return
    # zero hits for EVERY query. A string that cannot occur as a query token
    # does the same job safely.
    params["aesth_toks"] = sorted(_toks & aesthetic_vocab()) or ["__none__"]
    params["lore_lex_floor"] = LORE_LEX_FLOOR
    # Lexical carries the term (literal chunk match = 1.000 vs 0.159 average),
    # the vector adds a smaller paraphrase margin. Both normalized by their
    # headroom so each contributes 0..1 of its own weight.
    lore_term = (
        "(:lore_w * ("
        "  0.75 * GREATEST(0, coalesce(lore.lex, 0) - :lore_lex_floor) / (1 - :lore_lex_floor)"
        # RELATIVE, not an absolute floor. bge similarity moves with how
        # abstract the query is, so one fixed cutoff cannot serve both:
        #   cemetery 0.744  murder 0.594  haunted 0.574  spooky 0.526
        # A 0.60 floor gave "cemetery" full credit and the other three
        # exactly zero -- which is why conceptual queries felt dead while
        # literal ones worked. ref.hi is the best lore match ANY candidate
        # achieved for THIS query, so the term measures "how good is this
        # row's lore match compared with the best available", and a query
        # with no real lore signal still scores everyone near zero because
        # ref.hi itself is low relative to ref.floor.
        "+ 0.25 * GREATEST(0, coalesce(lore.sim, 0) - ref.lo)"
        "       / NULLIF(GREATEST(ref.hi - ref.lo, 0.05), 0)"
        "))"
    )
    fused = ("(0.7 * (1 - (b.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex + "
             + lore_term + ")")

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
            "b.architect AS b_architect, b.neighborhood AS b_neighborhood, "
            "b.aesthetic AS b_aesthetic"
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
            -- Typo tolerance runs against name_norm (names + aliases only),
            -- NOT the embedded document. similarity() is a whole-string
            -- trigram comparison, so against a ~200-char text it is
            -- structurally near zero however good the match:
            -- similarity('chrystler building', text) = 0.119, under the 0.2
            -- floor, while against name_norm it is 0.640. That is why
            -- "woolwoth" found the Woolworth Building but "chrystler" never
            -- surfaced the Chrysler. coalesce keeps this a no-op until
            -- backfill_index_name_norm has run.
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}
                  similarity(lower(:q_lex), lower(coalesce(name_norm, text))) > :fuzzy_floor
            ORDER BY similarity(lower(:q_lex), lower(coalesce(name_norm, text))) DESC
            LIMIT :pool
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
        hood_pool AS (
            -- Neighborhood recall. Search had no neighborhood concept at all,
            -- so "art deco in tribeca" returned Midtown and "cast iron soho"
            -- returned 2016-2018 glass towers. neighborhood_text holds the
            -- NTA name split into its parts ("SoHo-Little Italy-Hudson
            -- Square" -> "soho little italy hudson square") because the
            -- official compound name is otherwise one opaque token.
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}
                  {'TRUE' if want_hood else 'FALSE'}
              AND (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(neighborhood_text,'')))), 0) FROM unnest(CAST(:q_toks AS text[])) t) > :tok_floor
            ORDER BY (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(neighborhood_text,'')))), 0) FROM unnest(CAST(:q_toks AS text[])) t) DESC
            LIMIT :pool
        ),
        aesth_pool AS (
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}
                  {'TRUE' if want_aesth else 'FALSE'}
              AND aesthetic_text IS NOT NULL
              AND EXISTS (SELECT 1 FROM unnest(CAST(:aesth_toks AS text[])) t
                           WHERE lower(aesthetic_text) ~ ('\\y' || t || '\\y'))
            ORDER BY coalesce(fame, 0) DESC
            LIMIT :pool
        ),
        mat_pool AS (
            -- Material recall. The source spells it "Terra Cotta" (two words,
            -- 1,502 rows) while people type "terracotta"; material_text holds
            -- both spellings so the trigram can bridge them. Guarded by a
            -- coalesce so it is a no-op until backfill_index_material runs.
            SELECT bin FROM building_search_index
            {where + (' AND ' if where else 'WHERE ')}
                  {'TRUE' if want_mat else 'FALSE'}
              AND (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(material_text,'')))), 0) FROM unnest(CAST(:q_toks AS text[])) t) > :tok_floor
            ORDER BY (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(material_text,'')))), 0) FROM unnest(CAST(:q_toks AS text[])) t) DESC
            LIMIT :pool
        ),
        lore_lex_pool AS (
            -- Literal recall over the report prose. The vector pool alone did
            -- not contain the chunk that says "gargoyle" for the query
            -- "gargoyles", so without this leg the text is in the index and
            -- still unreachable.
            -- Prefiltered by EVERY query word as a whole-word stem, which
            -- the trigram index serves selectively. It used `<%` (the
            -- indexable word_similarity), which was fine for one rare word
            -- and a disaster for a phrase: "haunted ghost story" scanned
            -- 7.2s to return nothing, because every chunk shares trigrams
            -- with "story". LLM rewrites are exactly such phrases, and three
            -- of them per search took the buildings leg to 7.8s. The
            -- conjunctive stems run 40-160ms with comparable recall
            -- ("mansard roof" 208 vs 199 chunks), and a phrase that clears
            -- a 0.6 word_similarity contains its words anyway.
            --
            -- No ORDER BY: this pool is a RECALL set, not a ranking -- the
            -- fused score orders everything downstream.
            SELECT DISTINCT bin FROM (
                SELECT bin FROM building_lore_index
                 WHERE {lore_rx_sql}
                   AND word_similarity(lower(:q_lex), lower(text)) > :lore_lex_floor
                 LIMIT :lore_scan
            ) llp LIMIT :pool
        ),
        lore_pool AS (
            -- 4th leg: per-building LPC designation-report prose. This is the
            -- only corpus that contains ornament/material/feature language, so
            -- it is what lets "gargoyles" or "stained glass" retrieve the
            -- right BIN at all. Chunks roll up to one row per BIN.
            SELECT DISTINCT bin FROM (
                SELECT bin FROM building_lore_index
                 ORDER BY embedding <=> CAST(:qvec AS vector)
                 LIMIT :lore_scan
            ) lp LIMIT :pool
        ),
        lore_ref AS (
            -- Per-query scale for the lore vector term. Cheap: it reuses the
            -- same HNSW probe lore_pool already performs.
            SELECT max(sim) AS hi, min(sim) AS lo FROM (
                SELECT 1 - (embedding <=> CAST(:qvec AS vector)) AS sim
                  FROM building_lore_index
                 ORDER BY embedding <=> CAST(:qvec AS vector)
                 LIMIT :lore_scan
            ) lr
        ),
        pool AS (
            SELECT bin FROM vec_pool UNION SELECT bin FROM lex_pool
            UNION SELECT bin FROM fuzzy_pool UNION SELECT bin FROM fame_pool
            UNION SELECT bin FROM lore_pool UNION SELECT bin FROM mat_pool
            UNION SELECT bin FROM hood_pool UNION SELECT bin FROM lore_lex_pool
            UNION SELECT bin FROM aesth_pool
        )
        SELECT b.bin, b.bbl, b.snippet, b.year_built, b.is_landmark, b.fame, b.lat, b.lng,
               {fused} AS score,
               wl.lex AS lex_score
               {select_extra}
               {(', ' + haversine_b + ' AS dist_m') if geo else ''}
               -- ALWAYS LAST, and read as r[-2]/r[-1]: everything above is
               -- indexed positionally off extra_offset, so a mid-list insert
               -- silently shifts style_family/borough/material/architect.
               , lore.sim AS lore_score, lore.text AS lore_text, lore.lex AS lore_lex
               -- MAX over the individual aliases, not the concatenated blob.
               -- name_norm is "Chrysler Building | The Chrysler"; comparing a
               -- one-word q_lex against the whole string dilutes it to 0.280,
               -- against 0.261 for the unrelated "10 Chrystie Street" -- no
               -- separation. Per alias it is 0.438 vs 0.261. (word_similarity
               -- is worse still here: 0.583 vs 0.600, i.e. inverted.)
               , (SELECT max(similarity(lower(:q_lex), lower(a)))
                    FROM unnest(string_to_array(coalesce(b.name_norm, ''), ' | ')) a)
                 AS name_sim
        FROM building_search_index b
        JOIN pool USING (bin)
        CROSS JOIN lore_ref ref
        CROSS JOIN LATERAL (
            SELECT greatest(
                word_similarity(lower(:q_lex), lower(b.text)),
                similarity(lower(:q_lex), lower(b.text)),
                -- Retrieval columns the embedded `text` does not carry:
                -- material spelling variants, a name-only string short enough
                -- for similarity() to score, and the NTA neighborhood.
                similarity(lower(:q_lex), lower(coalesce(b.name_norm, ''))),
                (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(b.material_text, '')))), 0)
                   FROM unnest(CAST(:q_toks AS text[])) t),
                (SELECT coalesce(max(strict_word_similarity(t, lower(coalesce(b.neighborhood_text, '')))), 0)
                   FROM unnest(CAST(:q_toks AS text[])) t)
            ) AS lex
        ) wl
        LEFT JOIN LATERAL (
            -- Best-matching report chunk for this BIN: its similarity feeds
            -- the fused score, its text becomes the `why` citation.
            SELECT c.sim, c.lex, c.text FROM (
                SELECT 1 - (l.embedding <=> CAST(:qvec AS vector)) AS sim,
                       CASE WHEN {lore_chunk_gate}
                            THEN word_similarity(lower(:q_lex), lower(l.text))
                            ELSE 0 END AS lex,
                       l.text AS text
                  FROM building_lore_index l
                 WHERE l.bin = b.bin
            ) c
             -- Pick the chunk that best explains the hit on EITHER signal, so
             -- the citation is the sentence the user would recognise.
             ORDER BY GREATEST(c.lex, c.sim) DESC
             LIMIT 1
        ) lore ON true
        {('WHERE ' + ' AND '.join(enriched_filters)) if (enriched and enriched_filters) else ''}
        -- fame participates in the LEG's ordering (not just the post-RRF
        -- boost): the leg's LIMIT would otherwise cut a high-fame row (the
        -- Chrysler Building for "art deco") on raw fused score before the
        -- boost ever sees it. :fame_w is 0 for non-fame intents.
        -- A row whose own name IS the query leads the leg. "flatiron
        -- building" near Madison Square matched the Flatiron neighborhood on
        -- every nearby row with the same lexical score, and the closer ones
        -- cut the Flatiron Building itself before ranking ever saw it.
        ORDER BY ({fused} + :fame_w * coalesce(b.fame, 0)
                  + CASE WHEN (SELECT max(similarity(lower(:q_lex), lower(a)))
                                 FROM unnest(string_to_array(coalesce(b.name_norm, ''), ' | ')) a)
                              >= 0.6 THEN 1.0 ELSE 0 END) DESC
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
        # neighborhood rides in select_extra AFTER architect, so every index
        # below it shifts by one. These offsets are positional by design.
        neighborhood_val = r[extra_offset + 5] if enriched else None
        aesthetic_val = r[extra_offset + 6] if enriched else None
        personalization_dot = float(r[extra_offset + 7]) if has_personalization and r[extra_offset + 7] is not None else None
        dist_idx = extra_offset + (8 if has_personalization else 7) if enriched else extra_offset
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
            "neighborhood": neighborhood_val,
            "aesthetic": aesthetic_val,
            "category": None,
            "landmark": bool(r[4]) if r[4] is not None else None,
            "fame": float(r[5]) if r[5] is not None else None,
            "lat": r[6],
            "lng": r[7],
            "score": float(r[8]) if r[8] is not None else 0.0,
            "matched_field": (
                infer_matched_field(q_lex, name=name, style=style_family or parsed_style,
                                    architect=architect_val)
                if (r[9] or 0) > 0.5
                # The designation report carried this hit, not the metadata
                # template — say so, so "gargoyles" can cite the sentence that
                # actually says gargoyles instead of claiming "semantic".
                # Credit the REPORT when the designation text is what matched.
                # Keyed on the lexical score, not the vector one: the vector
                # barely separates (avg 0.516 vs max 0.543 for "gargoyles"),
                # so a vector-keyed test called every lore hit "semantic".
                else ("report" if (r[-2] or 0) > LORE_LEX_FLOOR else "semantic")
            ),
            "lore_score": float(r[-4]) if r[-4] is not None else None,
            "lore_text": r[-3],
            "lore_lex": float(r[-2]) if r[-2] is not None else None,
            "name_sim": float(r[-1]) if r[-1] is not None else None,
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
    """Venues leg: vector pool + trigram pool over `lex_text`, fused.

    Only `searchable` rows (see scripts/enrich_venues.py): not in New Jersey,
    not an FSQ "Structure" filed under an address, not a registry LLC. Before
    that filter, "seagram bar" returned Korean bars in Fort Lee and "art deco
    bar" led with Elove.com.

    The trigram pool reads `lex_text` (name, category, host building,
    neighborhood, borough), not name||snippet. The snippet is "{name} —
    {category}", so the host building was invisible to it: The Bar sits in the
    Seagram Building and "seagram bar" could not reach it lexically.

    soft_radius: see _leg_buildings' docstring — radius_m skips the WHERE
    filter and dist_m becomes a scoring-only signal instead."""
    params: dict = {"qvec": qvec_lit, "limit": limit, "q_lex": q_lex}
    filters: List[str] = ["searchable IS NOT FALSE"]
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

    where = "WHERE " + " AND ".join(filters)
    pool = min(max(limit * 4, 40), 200)
    params["pool"] = pool
    params["lex_floor"] = LEX_FLOOR
    fused = "(0.7 * (1 - (v.embedding <=> CAST(:qvec AS vector))) + 0.3 * wl.lex)"
    dist_sql = ""
    if geo:
        dist_sql = (
            ", 6371000 * acos(GREATEST(-1, LEAST(1, "
            "cos(radians(:lat)) * cos(radians(v.lat)) * cos(radians(v.lng) - radians(:lng)) "
            "+ sin(radians(:lat)) * sin(radians(v.lat))))) AS dist_m"
        )

    # Word-boundary guard — see _word_boundary_pattern's docstring. Venues has
    # no separate fuzzy/typo pool, so this is the ONLY trigram recall path;
    # skipped entirely when q_lex has no token long enough to build one. The
    # regex is also what lets the GIN trigram index on lex_text drive the pool.
    word_boundary = _word_boundary_pattern(q_lex)
    lex_word_boundary_clause = ""
    if word_boundary:
        params["lex_wb"] = word_boundary
        lex_word_boundary_clause = "AND lex_text ~ :lex_wb"

    sql = f"""
        WITH vec_pool AS (
            SELECT fsq_id FROM venues {where}
            ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT :pool
        ),
        lex_pool AS (
            SELECT fsq_id FROM venues
            {where}
            AND word_similarity(lower(:q_lex), lex_text) > :lex_floor
            {lex_word_boundary_clause}
            ORDER BY word_similarity(lower(:q_lex), lex_text) DESC
            LIMIT :pool
        ),
        pool AS (
            SELECT fsq_id FROM vec_pool UNION SELECT fsq_id FROM lex_pool
        )
        SELECT v.fsq_id, v.name, v.category, v.snippet, v.lat, v.lng,
               v.bin, v.bbl, v.building_year, v.building_style, v.photo_url,
               v.category_labels, v.neighborhood, v.borough, v.lex_text, v.text,
               {fused} AS score, wl.lex AS lex_score
               {dist_sql}
        FROM venues v
        JOIN pool USING (fsq_id)
        CROSS JOIN LATERAL (
            SELECT word_similarity(lower(:q_lex), coalesce(v.lex_text, lower(v.name))) AS lex
        ) wl
        ORDER BY score DESC
        LIMIT :limit
    """
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            result = await db.execute(text(sql), params)
            rows = [r._mapping for r in result.fetchall()]
    except Exception as e:
        logger.warning(f"[unified/venues] hybrid query failed ({e}); falling back to pure vector")
        return await _leg_venues_vector_only(qvec_lit, limit, lat, lng, radius_m, year_from, year_to, soft_radius=soft_radius)

    hits = []
    for r in rows:
        hits.append({
            "type": "venue",
            "id": r["fsq_id"],
            "bin": str(r["bin"]).replace(".0", "") if r["bin"] else None,
            "bbl": str(r["bbl"]).replace(".0", "") if r["bbl"] else None,
            "name": r["name"],
            "snippet": r["snippet"],
            "year": r["building_year"],
            "style": r["building_style"],
            "category": r["category"],
            "neighborhood": r["neighborhood"],
            "borough": r["borough"],
            "lex_text": r["lex_text"],
            "text": r["text"],
            "landmark": None,
            "lat": r["lat"],
            "lng": r["lng"],
            "score": float(r["score"]) if r["score"] is not None else 0.0,
            "lex_score": float(r["lex_score"]) if r["lex_score"] is not None else 0.0,
            "matched_field": (
                infer_matched_field(q_lex, name=r["name"], style=r["building_style"], category=r["category"])
                if (r["lex_score"] or 0) > 0.5 else "semantic"
            ),
            "dist_m": round(float(r["dist_m"]), 1) if geo and r.get("dist_m") is not None else None,
            "category_labels": list(r["category_labels"]) if r["category_labels"] else None,
            "photo_url": r["photo_url"],
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
    filters: List[str] = ["searchable IS NOT FALSE"]
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
    # See scripts/flag_layers_in_nyc.py: AMC Wayne 14 and a house in Sea
    # Cliff are not New York lore.
    filters: List[str] = ["in_nyc IS NOT FALSE"]
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
               , CASE WHEN l.layer = 'wiki' THEN l.text END AS full_text
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
        hit_type = layer_val if layer_val in ("lore", "plaque", "contribution", "wiki") else "lore"
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
            # The article extract, minus the "Title. " prefix it was embedded with.
            "summary": ((r._mapping.get("full_text") or "")[len(r[2] or "") + 2:] or None)
                       if hit_type == "wiki" else None,
        })
    return hits


async def _leg_layers_vector_only(
    qvec_lit: str, limit: int,
    lat: Optional[float], lng: Optional[float], radius_m: Optional[float],
    layer: Optional[str],
    soft_radius: bool = False,
) -> List[dict]:
    params: dict = {"qvec": qvec_lit, "limit": limit}
    # See scripts/flag_layers_in_nyc.py: AMC Wayne 14 and a house in Sea
    # Cliff are not New York lore.
    filters: List[str] = ["in_nyc IS NOT FALSE"]
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
        hit_type = layer_val if layer_val in ("lore", "plaque", "contribution", "wiki") else "lore"
        out.append({
            "type": hit_type, "id": r[0], "bin": None, "bbl": None, "name": r[2], "snippet": r[3],
            "year": r[7], "style": None, "category": category, "landmark": None,
            "lat": r[5], "lng": r[6], "score": float(r[4]) if r[4] is not None else 0.0,
            "matched_field": "semantic", "dist_m": None, "photo_url": None, "lore_status": lore_status,
        })
    return out


# ---------------------------------------------------------------------------
# Query interpretation (LLM expansion). See the block comment above
# INTERP_VERSION in services/unified_search.py for why it exists.
#
# The cache is keyed on the lowercased query and versioned: rows written by an
# older prompt are ignored rather than trusted, because the v1 prompt returned
# mood adjectives ("foggy", "ominous") that retrieve nothing.
# ---------------------------------------------------------------------------

_INTERP_SYSTEM = """You translate a search typed into a New York City architecture and history app into phrases its database can match.

The database holds three things:
1. NYC landmark designation reports: architectural styles, materials, ornament, architects, building types (church, bank, theater, tenement, loft, cemetery, mausoleum, rowhouse).
2. Short histories of buildings and events: fires, murders, hauntings, ghosts, demolitions, scandals, riots, film locations, famous residents.
3. Business listings: a venue name, a category such as "Cocktail Bar", "Wine Bar", "Speakeasy", "Coffee Shop", "Art Gallery", and a neighborhood.

Reply with JSON only, no prose, no code fences:
{"queries": [...], "categories": [...], "neighborhoods": [...], "boroughs": [...], "styles": [...], "years": [from, to] or null, "about": "buildings" | "places" | "stories" | "mixed", "picks": [{"name": ..., "hood": ..., "note": ...}], "events": true | false, "genres": [...], "kinds": [...], "when": "tonight" | "today" | "weekend" | "week" | null}

queries: 1 to 3 phrases of 1 to 5 words, written the way the DATABASE describes things, never the way people search. Turn moods into concrete things a report, a history or a listing would literally say. When the query is vague, give each phrase a DIFFERENT angle (architecture, history, a place to go) rather than three wordings of one idea. When the query asks for a kind of place (a bar, a cafe, a church), EVERY phrase names that kind of place. Use distinctive words only: never "house", "building", "place", "spot", "site", "location", "NYC", "New York", "near me", "best", "ideas", "things to do". If the query names a specific building, business, person or event, return that exact name as the only phrase.
categories: listing categories, only when the query asks for a kind of place to go. Otherwise [].
neighborhoods: NYC neighborhoods the query names or clearly implies. Otherwise [].
boroughs: any of Manhattan, Brooklyn, Queens, Bronx, Staten Island the query names. Otherwise [].
styles: architectural style names, as a designation report writes them, that the query names or implies ("modernist" -> "international style", "mid-century modern", "brutalist", "modern"). Otherwise [].
years: [from, to] when the query names or implies a period ("modernist" -> [1930, 1975], "gilded age" -> [1870, 1910], "prewar" -> [1880, 1940]). Otherwise null.
If the query is misspelt, the FIRST phrase is the query with its spelling fixed and nothing else changed.
picks: the specific real New York places or buildings a well-informed local would name as the best answers, up to 8, when the query is a vibe, a mood, a scene, slang, a superlative, a cuisine or style of place, or an architect's or firm's work ("chic bars", "dim lit bars", "romantic dinner", "old school italian", "cool hangout", "buildings by frank lloyd wright"). Read slang generously: "cunt", "slay", "serving", "giving" mean fashionable, fierce, glamorous, see-and-be-seen. name is the exact name the place goes by; hood is its neighborhood; note is 3 to 8 plain words on why it fits ("Piano bar inside the Carlyle"). Only places that exist; prefer ones still open; never invent. Name a place only if you are confident it fits this query; three right answers beat eight guesses. [] when the query already names one specific thing, or asks about history or architectural features rather than where to go or whose work.
events: true when the query asks what is on or happening now or soon (tonight, this weekend, a party, a gig, a DJ, live music, clubbing), or asks for exhibitions, gallery shows or openings, or film screenings. Otherwise false.
genres: music genres the query names or implies for events ("techno", "house", "jazz"). Otherwise [].
kinds: which listings the query asks about, any of "music", "art_opening" (gallery shows and openings), "exhibition" (museum shows), "film" (screenings). [] when it asks about all of them or none.
when: the time the query asks about: "tonight", "today", "weekend", "week", or null.
about: what the answer should mostly be. "buildings" for architecture (styles, features, materials, architects); "places" for somewhere to go (bars, cafes, shops, parks); "stories" for history, people and events (who lived where, crimes, disasters, hauntings, demolished things); "mixed" when it is genuinely several.

Examples:
"creepy places" -> {"queries":["cemetery mausoleum","haunted ghost story","murder"],"categories":[],"neighborhoods":[],"boroughs":[],"styles":["gothic revival"],"years":null,"about":"mixed"}
"brutalist cafes in soho" -> {"queries":["cafe brutalist concrete","coffee shop modern building"],"categories":["Coffee Shop","Cafe","Café"],"neighborhoods":["SoHo"],"boroughs":[],"styles":["brutalist","modern"],"years":[1950,1980],"about":"places"}
"woolworth bar" -> {"queries":["Woolworth Building"],"categories":["Cocktail Bar","Bar","Lounge"],"neighborhoods":[],"boroughs":[],"styles":[],"years":null,"about":"places"}
"date night queens" -> {"queries":["candlelit restaurant","wine bar garden"],"categories":["Restaurant","Wine Bar","Italian Restaurant","French Restaurant"],"neighborhoods":[],"boroughs":["Queens"],"styles":[],"years":null,"about":"places","picks":[{"name":"Bohemian Hall & Beer Garden","hood":"Astoria","note":"Century-old Czech beer garden"}],"events":false,"genres":[]}
"techno tonight" -> {"queries":["techno club","dance club warehouse"],"categories":["Dance Club","Music Venue","Nightclub"],"neighborhoods":[],"boroughs":[],"styles":[],"years":null,"about":"places","picks":[{"name":"Nowadays","hood":"Ridgewood","note":"Indoor-outdoor dance club, long sets"},{"name":"Basement","hood":"Maspeth","note":"Concrete techno bunker under a warehouse"}],"events":true,"genres":["techno"],"kinds":["music"],"when":"tonight"}
"flatiron building" -> {"queries":["Flatiron Building"],"categories":[],"neighborhoods":[],"boroughs":[],"styles":[],"years":null,"about":"buildings","picks":[],"events":false,"genres":[]}"""


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
            interp = row[0] if row else None
            if isinstance(interp, dict) and interp.get("v") == INTERP_VERSION:
                return interp
            return None
    except Exception as e:
        logger.info(f"[unified] interpretation cache check skipped: {e}")
        return None


async def _store_interpretation(q: str, interp: dict) -> None:
    import json
    try:
        async with get_search_db() as db:
            if db is not None:
                await db.execute(
                    text(
                        "INSERT INTO search_interpretation_cache (query, interpretation) "
                        "VALUES (:q, CAST(:interp AS jsonb)) "
                        "ON CONFLICT (query) DO UPDATE SET interpretation = EXCLUDED.interpretation, created_at = now()"
                    ),
                    {"q": q.strip().lower(), "interp": json.dumps(interp)},
                )
                await db.commit()
    except Exception as e:
        logger.info(f"[unified] interpretation store skipped for {q!r}: {e}")


async def _mark_direct_when_ready(q: str, task: "asyncio.Task", direct: bool) -> None:
    """For a request that did not wait for its rewrite: once the model
    answers, record whether this query needed it."""
    try:
        interp = await task
        if interp is not None:
            await _store_interpretation(q, {**interp, "direct": direct})
    except Exception:
        pass


async def _interpret_and_cache(q: str) -> Optional[dict]:
    """Ask the model, validate, cache, return. Never raises.

    Runs as a task started at the top of the request, concurrently with
    retrieval, so on the queries that need it most of its ~2.5s is already
    spent by the time the first pass finishes. If the request stops waiting,
    the task still completes and caches, so the next person gets it free."""
    try:
        raw = await openai_text(
            system=_INTERP_SYSTEM,
            user=q,
            # Reasoning is off in openai_text, so this is all answer. The
            # answer is ~60 tokens; 300 is headroom, not a target.
            max_tokens=900,
            timeout_s=10.0,
            cache_key=f"jink-search-interp-v{INTERP_VERSION}",
        )
        interp = parse_interpretation(raw, q)
        if interp is None:
            logger.info(f"[unified] interpretation unusable for {q!r}: {(raw or '')[:120]!r}")
            return None
        await _store_interpretation(q, interp)
        return interp
    except Exception as e:
        logger.info(f"[unified] interpretation skipped for {q!r}: {e}")
        return None


# ---------------------------------------------------------------------------
# Picks: the model's named answers, resolved against our own rows.
#
# Venues carry a name, a category and a host building, nothing else: no
# reviews, no ratings, no description. So "chic bars", "dim lit", "romantic"
# and slang had nothing to match and fell back to "the nearest bar". The
# model does know which bars are chic. It names them; a name only becomes a
# result when it matches a venue or building we hold, so an invented or
# closed place cannot show.
# ---------------------------------------------------------------------------

PICK_VENUE_MIN_SIM = 0.6
PICK_BUILDING_MIN_SIM = 0.55


def _meters(lat1, lng1, lat2, lng2) -> Optional[float]:
    import math
    if None in (lat1, lng1, lat2, lng2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


async def _resolve_picks(picks: List[dict], about: Optional[str],
                         lat: Optional[float], lng: Optional[float]) -> List[dict]:
    """One hit per pick that matches a row, in the model's order."""
    if not picks:
        return []
    names = [p["name"] for p in picks]
    hoods = [p.get("hood") or "" for p in picks]
    venues_sql = text("""
        SELECT p.ord, v.fsq_id, v.name, v.category, v.lat, v.lng, v.bin, v.bbl,
               v.building_year, v.building_style, v.neighborhood, v.borough,
               v.photo_url, v.snippet, v.sim
          FROM unnest(CAST(:names AS text[]), CAST(:hoods AS text[])) WITH ORDINALITY AS p(n, hood, ord)
          CROSS JOIN LATERAL (
            SELECT fsq_id, name, category, lat, lng, bin, bbl, building_year, building_style,
                   neighborhood, borough, photo_url, snippet,
                   similarity(lower(p.n), lower(name)) AS sim
              FROM venues
             WHERE searchable IS NOT FALSE
               AND lower(name || ' ' || coalesce(snippet, '')) %> lower(p.n)
             -- Several rows share a name (branches, stale duplicates with the
             -- wrong coordinates); the one in the neighborhood the model
             -- named wins.
             ORDER BY similarity(lower(p.n), lower(name))
                      + CASE WHEN p.hood <> '' AND lower(coalesce(neighborhood, ''))
                                  LIKE '%' || lower(split_part(p.hood, ' ', 1)) || '%'
                             THEN 0.3 ELSE 0 END DESC
             LIMIT 1
          ) v
    """)
    buildings_sql = text("""
        SELECT p.ord, b.bin, b.bbl, b.snippet, b.year_built, b.style_family, b.lat, b.lng,
               b.photo_url, b.architect, b.neighborhood, b.borough, b.fame, b.sim
          FROM unnest(CAST(:names AS text[])) WITH ORDINALITY AS p(n, ord)
          CROSS JOIN LATERAL (
            SELECT bin, bbl, snippet, year_built, style_family, lat, lng, photo_url,
                   architect, neighborhood, borough, fame,
                   (SELECT max(similarity(lower(p.n), lower(a)))
                      FROM unnest(string_to_array(name_norm, ' | ')) a) AS sim
              FROM building_search_index
             WHERE lower(name_norm) %> lower(p.n)
             ORDER BY sim DESC NULLS LAST, fame DESC NULLS LAST
             LIMIT 1
          ) b
    """)
    try:
        async with get_search_db() as db:
            if db is None:
                return []
            vrows = (await db.execute(venues_sql, {"names": names, "hoods": hoods})).mappings().all()
            brows = (await db.execute(buildings_sql, {"names": names})).mappings().all()
    except Exception as e:
        logger.info(f"[unified] pick resolution skipped: {e}")
        return []

    best: Dict[int, dict] = {}
    import re as _re

    def _words(x: str) -> set:
        return {w for w in _re.split(r"[^\w']+", (x or "").lower()) if w}

    for r in vrows:
        # Close on the whole string, or every word of the pick is in the name
        # ("230 Fifth" is "230 Fifth Rooftop Bar"). A near-spelling is not:
        # "Tino's Cucina" is not "Tina's Cuban".
        pick_words = _words(picks[r["ord"] - 1]["name"]) - {"the"}
        if (r["sim"] or 0) < PICK_VENUE_MIN_SIM and not (pick_words and pick_words <= _words(r["name"])):
            continue
        best[r["ord"]] = {
            "type": "venue", "id": r["fsq_id"],
            "bin": str(r["bin"]).replace(".0", "") if r["bin"] else None,
            "bbl": str(r["bbl"]).replace(".0", "") if r["bbl"] else None,
            "name": (r["name"] or "").strip(), "snippet": r["snippet"],
            "year": r["building_year"], "style": r["building_style"],
            "category": r["category"], "lat": r["lat"], "lng": r["lng"],
            "photo_url": r["photo_url"], "lore_status": None, "_sim": float(r["sim"]),
        }
    for r in brows:
        sim = float(r["sim"] or 0)
        if sim < PICK_BUILDING_MIN_SIM:
            continue
        cur = best.get(r["ord"])
        # A building beats a venue only when it is the better name match, or
        # an equal one on a query about architecture.
        if cur and (cur["_sim"] > sim or (cur["_sim"] == sim and about != "buildings")):
            continue
        snippet = r["snippet"] or ""
        name = snippet.split("—", 1)[0].strip() if "—" in snippet else snippet
        bin_ = str(r["bin"]).replace(".0", "") if r["bin"] else None
        best[r["ord"]] = {
            "type": "building", "id": bin_, "bin": bin_,
            "bbl": str(r["bbl"]).replace(".0", "") if r["bbl"] else None,
            "name": name or picks[r["ord"] - 1]["name"], "snippet": snippet or None,
            "year": r["year_built"], "style": r["style_family"], "category": None,
            "lat": r["lat"], "lng": r["lng"], "photo_url": r["photo_url"],
            "lore_status": None, "_sim": sim,
        }
    out = []
    for ord_, h in sorted(best.items()):
        h.pop("_sim", None)
        h["why"] = picks[ord_ - 1].get("note") or ""
        h["dist_m"] = (round(_meters(lat, lng, h["lat"], h["lng"]), 1)
                       if lat is not None and lng is not None and h["lat"] is not None else None)
        h["pick"] = True
        out.append(h)
    return out


async def _log_query(q: str, intent: str, latency_ms: float, result_ids: List[str]) -> None:
    """Best-effort query log. Never raises into the caller.

    Sanitizes its OWN input rather than trusting the caller. This runs in a
    fire-and-forget task, so it can be reached with whatever `q` the endpoint
    happened to capture -- and it did: a query containing %00 searched fine
    (the endpoint sanitizes before building params) and then died here on the
    INSERT, because analytics wrote the value the request arrived with.

    An error from a best-effort logger is pure noise: it cannot help the user,
    it fires after the response is already built, and it buries real errors in
    Sentry.
    """
    q = _sanitize_query(q)
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
    """Cache a response, but NEVER an empty one.

    Every leg degrades to [] on exception (the deliberate "a broken leg never
    breaks the others" contract), so a transient DB stall produces a perfectly
    valid-looking response with zero hits -- and caching it pinned that exact
    query to "no results" for the full 300s TTL, long after the database had
    recovered.

    Observed exactly that: a long-running UPDATE blocked reads of `venues`,
    and afterwards "art deco" returned 0 hits while "art deco building" and
    "brutalist" were fine, purely because the one query had been asked during
    the stall. Nudging the coordinates by a few hundred metres -- a different
    cache key -- returned 3 hits immediately.

    An empty result is nearly always a failure or a miss, and neither is worth
    remembering. Re-running a genuinely empty query costs one search; serving
    a wrong empty one costs the user the feature.
    """
    if not resp.get("hits"):
        return
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
    debug: bool = Query(False, description="Include a per-hit score breakdown (_debug). For tuning; never cached."),
    area: bool = Query(False, description="'Search this area': radius_m is a HARD bound for every intent and every leg"),
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
         landmark, user_vector, scanned_bins, area),
    )
    cached_resp = None if debug else _result_cache_get(cache_key)
    if cached_resp is not None:
        return cached_resp

    q = _sanitize_query(q)
    intent, poi_noun = classify_intent_detailed(q)
    weights = dict(corpus_weights(intent))
    # HARD_RADIUS_INTENTS (poi/name/address/event): radius_m stays a hard
    # WHERE filter — "near me" is core to those queries. Everything else
    # (style/architect/lore/prose) treats radius as a soft proximity signal
    # only, so a better match further away can still surface.
    soft_radius = intent not in HARD_RADIUS_INTENTS

    # A query that names a place is not a "near me" query, whatever its
    # intent: "bars in midtown" asked from Brooklyn must not be clipped to
    # the user's radius, where it can only return Brooklyn bars. And the
    # place is a constraint in its own right, detected here without the LLM.
    q_hoods, q_boros = _query_places(q)
    if q_hoods or q_boros:
        soft_radius = True
    # "Search this area" is the user drawing the boundary by moving the map.
    # It overrides every softening above: results outside the viewport are
    # exactly what the button exists to exclude.
    area_bound = bool(area and lat is not None and lng is not None and radius_m)
    if area_bound:
        soft_radius = False

    # LLM expansion, started NOW so it runs alongside retrieval rather than
    # after it. See INTERP_VERSION in services/unified_search.py.
    #
    # Before this, expansion was inert twice over: it only ever fired on
    # `prose` intent (which almost no real query reaches -- "spooky spots" is
    # `name`), and even on a cache hit the result was never applied. The cache
    # table was empty on 2026-09-22 after thousands of searches.
    interp = await _get_cached_interpretation(q)
    interp_task: Optional[asyncio.Task] = None
    if interp is None and intent != "address" and len(q) >= 3:
        interp_task = asyncio.create_task(_interpret_and_cache(q))

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

    # Personalization vector — parsed BEFORE the legs run so it can be pushed
    # into _leg_buildings' SQL (dot product computed in Postgres via pgvector's
    # <#> operator against building_search_index.profile). Buildings-only:
    # venues/layers carry no aesthetic profile column in this DB.
    user_vec_lit: Optional[str] = None
    if user_vector:
        try:
            parts = [float(x) for x in user_vector.split(",")]
            if len(parts) == 9:
                # L2-NORMALIZE. The nudge is `W_PERSONALIZATION * dot(profile,
                # uvec)`, sized in rank-steps on the assumption that dot is a
                # cosine in [-1, 1]. It was not: stored profile vectors have
                # magnitudes up to 88 (mean 65), so an un-normalized dot made
                # the 0.02 weight worth up to ~110 rank steps and personal
                # taste silently became the primary sort key -- "oculus"
                # ranked Odyssey House first and The Oculus fifth. The iOS
                # client has had user_vector hard-disabled over this.
                # Normalizing both sides is what makes the weight mean what it
                # says; see the matching UPDATE in
                # migrations/20260920_normalize_profile_vectors.sql.
                norm = sum(x * x for x in parts) ** 0.5
                if norm > 0:
                    parts = [x / norm for x in parts]
                    user_vec_lit = "[" + ",".join(f"{x:.6f}" for x in parts) + "]"
        except Exception:
            user_vec_lit = None

    async def _retrieve(vec_lit: str, lex: str, *, with_personal: bool = True,
                        soft: Optional[bool] = None, years: Optional[List[int]] = None):
        """One full leg set (buildings, venues, layers) for one phrasing of
        the query, with the request's filters and POI adjustments applied."""
        soft_r = soft_radius if soft is None else soft
        yf, yt = (years if years else (year_from, year_to))
        b, v, l = await asyncio.gather(
            _leg_buildings(
                vec_lit, lex, leg_limit, lat, lng, radius_m, yf, yt,
                borough=borough, material=material, style_family=style_family,
                user_vec_lit=user_vec_lit if with_personal else None, soft_radius=soft_r,
                fame_weight=W_LEG_FAME if intent in FAME_BOOST_INTENTS else 0.0,
                lore_weight=leg_lore_weight(intent),
            ),
            _leg_venues(vec_lit, lex, leg_limit, lat, lng, radius_m, yf, yt, soft_radius=soft_r),
            _leg_layers(vec_lit, lex, leg_limit, lat, lng, radius_m, layer_filter, soft_radius=soft_r),
        )
        return _post_filter(b, v, l)

    def _post_filter(buildings_hits, venues_hits, layers_hits):
        if area_bound:
            # Belt and braces over the SQL radius: some pools (typo, lore,
            # neighborhood) are unions that do not all carry the WHERE, and a
            # rewrite leg must not reintroduce what the viewport excludes.
            def _inside(hs):
                return [h for h in hs if h.get("dist_m") is not None and h["dist_m"] <= radius_m * 1.05]
            buildings_hits, venues_hits, layers_hits = _inside(buildings_hits), _inside(venues_hits), _inside(layers_hits)
        if landmark is not None:
            buildings_hits = [h for h in buildings_hits if h.get("landmark") == landmark]
        if style_family:
            # The SQL leg already filters buildings via style_family; this is
            # what does the work for venues (no style_family column) and a
            # harmless re-check for buildings.
            needle = style_family.replace("_", " ").lower()
            buildings_hits = [h for h in buildings_hits if h.get("style") and needle in h["style"].lower()]
            venues_hits = [h for h in venues_hits if h.get("style") and needle in h["style"].lower()]
        if borough:
            buildings_hits = [h for h in buildings_hits if not h.get("borough") or h["borough"].lower() == borough.lower()]
            venues_hits = [h for h in venues_hits if not h.get("borough") or h["borough"].lower() == borough.lower()]
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
        return buildings_hits, venues_hits, layers_hits

    async def _expand(phrase: str):
        vec = await asyncio.to_thread(embed_query, phrase)
        # Personalization rides on the user's own query only: applying it
        # to every rewrite would count taste once per phrasing.
        # A rewrite is not held to the "near me" radius: it is how a query
        # that names something ("seagram bar" -> "Seagram Building") reaches
        # it from across town. Only "search this area" bounds a rewrite.
        # The implied era FILTERS the rewrite legs (never the user's own):
        # re-ranking cannot surface a 1958 bar that was never retrieved.
        # An explicit year filter from the request wins.
        era = None if (year_from or year_to) else (interp or {}).get("years")
        return await _retrieve(_vec_literal(vec), _lexical_query(phrase), with_personal=False,
                               soft=not area_bound, years=era)

    # A cached rewrite that last time turned out to be NEEDED ("direct":
    # false) runs alongside the user's own legs instead of after them, so a
    # repeat of "spooky spots" costs one round of queries, not two.
    expansion_queries: List[str] = []
    expanded: Optional[list] = None
    pre_expand = bool(interp and interp.get("direct") is False and interp.get("queries"))
    if pre_expand:
        expansion_queries = list(interp["queries"])[:MAX_EXPANSION_QUERIES]
        results = await asyncio.gather(
            _retrieve(qvec_lit, q_lex), *[_expand(p) for p in expansion_queries],
            return_exceptions=True,
        )
        if isinstance(results[0], Exception):
            raise results[0]
        buildings_hits, venues_hits, layers_hits = results[0]
        expanded = list(results[1:])
    else:
        buildings_hits, venues_hits, layers_hits = await _retrieve(qvec_lit, q_lex)
    raw_legs = {"buildings": buildings_hits, "venues": venues_hits, "layers": layers_hits}
    t_first = time.monotonic()

    # Not enough REAL matches inside the near-me radius: ask again citywide.
    # "tin ceilings" or "art deco bars near me" with none close by should show
    # the real ones further out (tiered nearest-first below), not fill the
    # list with nearby things that merely scored. "Search this area" is the
    # user's own boundary and never widens.
    widened = None
    if (not soft_radius and not area_bound and radius_m and lat is not None and lng is not None):
        _q_toks = query_content_tokens(q_lex)
        _named = await _named_tokens(q_lex, _q_toks)
        _place = {"neighborhoods": q_hoods, "boroughs": q_boros} if (q_hoods or q_boros) else None
        # A query that names something ("bars near grand central") is not
        # satisfied by any bar nearby: the rewrite's category-only notion of
        # a match is off, exactly as in the final tiering.
        _interp_for_count = None if _named else interp
        _local = [h for hs in raw_legs.values() for h in hs
                  if tier_of(h, _q_toks, _named, _interp_for_count, _place, neighborhood_vocab(), generic_vocab()) < 2]
        local_matches = len(_local)
        widened = local_matches
        if local_matches < MIN_LOCAL_MATCHES:
            gb, gv, gl = await _retrieve(qvec_lit, q_lex, soft=True)
            fresh = {"buildings": gb, "venues": gv, "layers": gl}
            for k in raw_legs:
                seen = {x.get("id") for x in raw_legs[k]}
                raw_legs[k] = raw_legs[k] + [h for h in fresh[k] if h.get("id") not in seen]

    # Wait for the model only when the user's own words did not already find
    # the answer. "chrysler building" must not pay 2.5s for a rewrite it does
    # not need; "spooky spots" should.
    direct = has_direct_match(q_lex, intent, raw_legs)
    # A kind of place ("chic bars") also waits, once per query: a venue
    # named "Chic Republic" made the words look answered, and the picks,
    # the actual chic bars, never ran.
    if interp is None and interp_task is not None and (not direct or intent == "poi"):
        remaining = INTERP_WAIT_S - (time.monotonic() - start)
        if remaining > 0:
            try:
                # shield: on timeout the task keeps running and still caches.
                interp = await asyncio.wait_for(asyncio.shield(interp_task), timeout=remaining)
            except asyncio.TimeoutError:
                logger.info(f"[unified] interpretation not ready in {INTERP_WAIT_S}s for {q!r}; cached for next time")
    # Remember whether this query needed its rewrite, so the next run knows
    # whether to start it in parallel. Fire-and-forget.
    if interp is not None and interp.get("direct") is not (direct is True):
        interp = {**interp, "direct": bool(direct)}
        asyncio.create_task(_store_interpretation(q, interp))
    elif interp is None and interp_task is not None:
        asyncio.create_task(_mark_direct_when_ready(q, interp_task, bool(direct)))

    t_llm = time.monotonic()
    legs = {
        name: [RankedHit(name, h["id"], i + 1, h) for i, h in enumerate(hits) if h.get("id")]
        for name, hits in raw_legs.items()
    }

    if direct:
        expansion_queries, expanded = [], None
    elif interp and expanded is None:
        expansion_queries = list(interp.get("queries") or [])[:MAX_EXPANSION_QUERIES]
        if expansion_queries:
            expanded = await asyncio.gather(*[_expand(p) for p in expansion_queries], return_exceptions=True)
    # The model naming venue categories means the query asks for a PLACE TO
    # GO, whatever the intent router guessed: "romantic dinner brooklyn"
    # routes as `name` (the fallback) and weighted lore and buildings equal
    # to restaurants, so a Botanic Garden light show outranked them.
    place_seeking = bool(interp and interp.get("categories")) and intent not in ("poi", "address")
    if place_seeking:
        weights.update(corpus_weights("poi"))
    # What the query is about, when the model said so and the query needed
    # its rewrite (a direct match keeps the router's weights).
    about_w = about_weights(interp) if (interp and not direct and intent != "address") else None
    if about_w:
        weights.update(about_w)
    correction = spelling_correction(q, interp) if expansion_queries and expanded else None
    if expansion_queries and expanded:
        for corpus in ("buildings", "venues", "layers"):
            # A misspelt query's own legs are noise: the correction replaces them.
            weights[corpus] = 0.0 if correction else weights.get(corpus, 1.0) * W_ORIGINAL_WHEN_EXPANDED
        if correction:
            # And every word-level signal below (name bonuses, coverage,
            # facets) reads the corrected words, not the typo.
            q_lex = _lexical_query(correction)
        for n, res in enumerate(expanded):
            if isinstance(res, Exception):
                logger.warning(f"[unified] expansion leg failed for {expansion_queries[n]!r}: {res}")
                continue
            for corpus, hits in zip(("buildings", "venues", "layers"), res):
                leg = f"{corpus}~{n}"
                if not (correction and n == 0):
                    # lore_lex / name_sim on these hits measure the REWRITE
                    # phrase, not the user's words; tier_of must not read
                    # them as proof of a match (see _rewrite in tier_of).
                    for h in hits:
                        h["_rewrite"] = True
                # A rewrite of a POI query is still a POI query, so it keeps the
                # user's corpus weights: with neutral ones, "modernist bars in
                # midtown" returned lore ABOUT modernism above any bar. But
                # `name` is the classifier's fallback, not a real reading, and
                # it weights lore at 0.6 -- exactly where "haunted ghost story"
                # for "spooky spots" belongs -- so those use neutral weights.
                basis = ("poi" if place_seeking else
                         intent if intent in ("poi", "style", "architect", "lore", "event") else "prose")
                weights[leg] = (about_w or corpus_weights(basis)).get(corpus, 1.0) * W_EXPANSION_LEG
                legs[leg] = [RankedHit(corpus, h["id"], i + 1, h) for i, h in enumerate(hits) if h.get("id")]
        logger.info(f"[unified] expanded {q!r} -> {expansion_queries} "
                    f"cats={interp.get('categories')} hoods={interp.get('neighborhoods')} boros={interp.get('boroughs')}")

    t_expand = time.monotonic()
    fused = reciprocal_rank_fusion(legs, weights)

    # Where the query asked to be: phrases found in the query itself, plus
    # whatever the model inferred. Either alone is enough.
    place_req = None
    _hoods = list(q_hoods) + [x for x in ((interp or {}).get("neighborhoods") or []) if x.lower() not in q_hoods]
    _boros = list(q_boros) + [x for x in ((interp or {}).get("boroughs") or []) if x.lower() not in q_boros]
    if _hoods or _boros:
        place_req = {"neighborhoods": _hoods, "boroughs": _boros}

    scanned = set()
    if scanned_bins:
        scanned = {b.strip() for b in scanned_bins.split(",") if b.strip()}

    # Build the FULL nudged/scored list first (not truncated to `limit` yet):
    # dedupe_near_identical needs to see the whole candidate set to find
    # near-duplicate clusters, and truncating before dedup could keep two
    # duplicates while dropping a genuinely-different lower-ranked hit.
    all_hits: List[Dict[str, Any]] = []
    # Facet-ness is emergent from the RESULT SET (a token is a material or
    # neighborhood term only because some hit really carries it in that
    # column), so this is computed once over the whole list rather than
    # per-hit inside the loop.
    _facet_adj = facet_adjustments(q_lex, [rh.payload for _, _, rh in fused])
    for idx, (gk, score, ranked_hit) in enumerate(fused):
        h = dict(ranked_hit.payload)
        dbg: Dict[str, float] = {}

        def _t(label: str, val: float) -> float:
            if debug and val:
                dbg[label] = round(dbg.get(label, 0.0) + float(val), 4)
            return val
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
        _t("apply_nudges", nudged - score * RRF_SCALE)
        # Soft-radius proximity: for style/architect/lore/prose intents,
        # radius_m was never applied as a WHERE filter (see soft_radius
        # above), so dist_m may be large or None — add a decaying bonus
        # instead of a cutoff, only when the caller actually supplied a
        # location (dist_m is only ever populated when lat/lng were given).
        if soft_radius and h.get("dist_m") is not None:
            nudged += _t("proximity_decay_bonus", proximity_decay_bonus(h.get("dist_m")))
        # Landmark/fame boost: buildings only, only on intents where fame
        # should break ties (see FAME_BOOST_INTENTS) — lets an icon-tier
        # building (Chrysler etc.) beat an obscure same-style row house.
        nudged += _t("fame_boost", fame_boost(intent, h.get("fame")))
        # Name intent: an exact/subset name match is the answer — this bonus
        # is deliberately dominant over every other nudge (see W_EXACT_NAME).
        if intent == "name":
            _exact = exact_name_bonus(q_lex, h.get("name"))
            nudged += _t("_exact", _exact)
            # A typo of a famous name is still a name match. exact_name_bonus
            # fires only on exact/subset TOKEN equality, so a misspelling got
            # nothing and "chrystler building" put the Chrysler Building
            # second, behind 215 Chrystie Street. Skipped when the exact bonus
            # already fired -- same signal, would double-count.
            nudged += _t("fuzzy_name_bonus", fuzzy_name_bonus(intent, h.get("name_sim"), _exact))
        # Architect intent: a real column match is the answer, same standing as
        # an exact name match. Previously this intent had no structured field
        # to score against at all.
        if intent == "architect":
            nudged += _t("architect_match_bonus", architect_match_bonus(q_lex, h.get("architect")))
        # Naming an archetype is an instruction: "austerist" must return the
        # 169 austerist buildings, not the stylistically adjacent modernists
        # that happen to be more famous.
        # ...but only when the row answers the rest of the query too.
        # "romantic" is an archetype AND an adjective: "romantic dinner
        # brooklyn" handed +0.30 (~19 rank steps) to every romantic-archetype
        # building in Brooklyn, and not one of them serves dinner.
        if token_coverage(q_lex, h.get("name"), h.get("snippet"), h.get("category"),
                          h.get("style"), h.get("neighborhood"),
                          (h.get("aesthetic") or "").replace("_", " ")) >= 1.0:
            nudged += _t("aesthetic_match_bonus", aesthetic_match_bonus(q_lex, h.get("aesthetic")))
        # A lore entry whose title carries the query is the answer regardless
        # of intent: "kitty genovese" retrieves the Kitty Genovese Murder at
        # 0.82 and was still buried under buildings by the corpus weights.
        nudged += _t("layer_title_bonus", layer_title_bonus(q_lex, h.get("type"), h.get("name")))
        # House-number address queries ("469 broome") classify as name/address
        # but their number is the whole signal — a dominant bonus when a
        # building's address range contains it, so the exact address beats fame
        # (570 Broome was outranking 469-475). Buildings only.
        if intent in ("name", "address") and h.get("type") == "building":
            nudged += _t("house_number_bonus", house_number_bonus(q_lex, h.get("name"), h.get("snippet")))
        # POI adjustments re-applied on the RRF scale (see the venues
        # re-score block above for why twice).
        nudged += _t("poi_adj", h.get("poi_adj") or 0.0)
        # Lore/event multi-token queries: reward full concept coverage
        # ("demolished" AND "theaters"), demote single-concept matches.
        nudged += _t("coverage", coverage_adjustment(
            intent, q_lex, h.get("name"), h.get("snippet"),
            h.get("category"), h.get("style"),
        ))
        # Hedged style attributions ("… colonial revival OR art deco") were
        # scoring as confident matches because trigram similarity reads the
        # best-matching substring. Discount a match that only lands on the
        # alternative, never on the primary.
        nudged += _t("hedged", hedged_style_penalty(
            query_style_tokens(q, poi_noun), h.get("style")
        ))
        # Facet decoys: "cast iron soho" ranked 565 Broome SoHo (glass, 2018)
        # first on a NAME match for "soho" while the actual cast-iron district
        # sat below it. Computed across the whole list (facet-ness is emergent
        # from it), so it is applied from a precomputed array, not per-hit.
        nudged += _t("_facet_adj", _facet_adj[idx])
        # What the model said the query MEANT: the kind of place, and where.
        # Applied whenever an interpretation exists, including on a direct
        # match, because "bars in midtown" names a place either way.
        nudged += _t("llm_category_bonus", llm_category_bonus(interp, h))
        nudged += _t("llm_style_bonus", llm_style_bonus(interp, h))
        nudged += _t("llm_era_bonus", llm_era_bonus(interp, h))
        nudged += _t("place_adjustment", place_adjustment(place_req, h, neighborhood_vocab()))
        # Evidence, not internals: see evidence_why. Buildings and venues show
        # nothing rather than repeat the year/style the row already shows.
        why = evidence_why(h, q_lex)
        if why is None:
            # Nothing to add beyond the row's own meta line (year, style,
            # category, distance), so say nothing; the client hides it.
            why = ""
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
            # Wikipedia hits carry their article link so the client can open
            # the same sheet its Wikipedia map layer uses.
            **({"url": "https://en.wikipedia.org/wiki/" + (h.get("name") or "").replace(" ", "_"),
                "summary": h.get("summary")}
               if h.get("type") == "wiki" else {}),
            "_src": h,
            **({"_debug": {"rrf": round(score * RRF_SCALE, 4), **dbg,
                           "legs": sorted(k for k, lst in legs.items()
                                          if any(r.key == ranked_hit.key and r.corpus == ranked_hit.corpus for r in lst))}}
               if debug else {}),
        })

    # Re-sort after the soft-radius/landmark nudges (both applied AFTER the
    # RRF-order `fused` list was built, so they can reorder within it), then
    # dedupe near-identical hits (e.g. repeated "Court Name: Roosevelt" rows
    # across adjacent BINs of one development — same name, <150m apart),
    # THEN truncate to `limit`.
    all_hits.sort(key=lambda h: h["score"], reverse=True)
    deduped = dedupe_same_place(dedupe_near_identical(all_hits))
    # Diversity BEFORE the limit, or the cap has nothing to promote into the
    # space it frees: three adjacent row houses sharing one designation report
    # otherwise fill the whole visible list ("haunted buildings" returned 55,
    # 53 and 47 West 28th Street). Order-preserving -- nothing is dropped,
    # the surplus is pushed below the alternatives.
    diversified = apply_diversity_cap(deduped)
    # Tiers: the actual thing, then real matches NEAREST FIRST, then the rest
    # by relevance. See order_by_tier in services/unified_search.py.
    q_toks = query_content_tokens(q_lex)
    named_toks = await _named_tokens(q_lex, q_toks)
    # When the named thing itself is in the list ("seagram bar" -> The Bar),
    # the rewrite's looser "any Cocktail Bar" definition of a match is off:
    # other bars near you are not what was asked for.
    tiers = [tier_of(h["_src"], q_toks, named_toks, None, place_req, neighborhood_vocab(), generic_vocab())
             for h in diversified]
    if 0 not in tiers and interp:
        tiers = [tier_of(h["_src"], q_toks, named_toks, interp, place_req, neighborhood_vocab(), generic_vocab())
                 for h in diversified]
    # Hits that carry the name stay real matches only when the query also asks
    # for a KIND of thing ("bars near grand central"). A bare name ("chrysler
    # building") keeps its context ranked by relevance, not its tenants.
    _asks_kind = bool(q_toks - named_toks)
    # A building whose whole name IS the query is the thing, even when every
    # word of it is generic: "flatiron" is a neighborhood, so the Flatiron
    # Building had no distinctive word, sorted by distance among forty
    # things "in Flatiron", and fell off the list. The name goes through the
    # same stopword strip as the query ("building" is one). Buildings only:
    # a venue literally named "Wine Bar" is not the answer to "wine bar".
    for i, h in enumerate(diversified):
        if (h.get("type") == "building" and not h["_src"].get("_rewrite") and q_toks
                and query_content_tokens(_lexical_query(h.get("name") or "")) == q_toks):
            tiers[i] = 0
    tiers = resolve_entity_mode(tiers, [_asks_kind and carries_name(h["_src"], named_toks) for h in diversified])
    if debug:
        for h, t in zip(diversified, tiers):
            h["_debug"]["tier"] = t
    # An address names one place: its matches rank by relevance (the house
    # number bonus), not by which copy of it is a few metres closer.
    ordered = order_by_tier(diversified, tiers,
                            lat is not None and lng is not None and intent != "address")
    tier_by_id = {id(h): t for h, t in zip(diversified, tiers)}
    matched = [h for h in ordered if tier_by_id[id(h)] < 2]
    rest = [h for h in ordered if tier_by_id[id(h)] == 2]
    # Floor only the unmatched tail: a real match is never dropped for scoring
    # below a vaguer hit, and `limit` is a ceiling, not a quota to fill.
    # Street cap again AFTER tiering: sorting matches by distance regrouped
    # the adjacent West 28th Street row houses the first pass had spread out.
    hits = apply_diversity_cap(matched + apply_relevance_floor(rest))[:limit]
    for h in hits:
        h.pop("_src", None)

    # The model's named answers lead, nearest first. The user's words did
    # not find them (a direct match means the words did), so they are what
    # the query meant. Anything already in the list moves up, not in twice.
    picks: List[dict] = []
    if interp and interp.get("picks"):
        picks = await _resolve_picks(interp["picks"], interp.get("about"), lat, lng)
        # "Near me" and "search this area" bound picks like everything else:
        # "coffee near me" is not answered by a famous roaster 14km away.
        if (area_bound or not soft_radius) and radius_m and lat is not None:
            picks = [p for p in picks if p.get("dist_m") is not None and p["dist_m"] <= radius_m * 1.05]
        if lat is not None and lng is not None:
            picks.sort(key=lambda p: p["dist_m"] if p.get("dist_m") is not None else 1e12)
        if picks:
            ids = {p["id"] for p in picks}
            bins = {p["bin"] for p in picks if p["type"] == "building" and p.get("bin")}
            names = {(p["name"] or "").lower() for p in picks}
            def _dup(h):
                return (h.get("id") in ids
                        or (h.get("type") == "building" and h.get("bin") in bins)
                        or (h.get("type") in ("venue", "apple") and (h.get("name") or "").lower() in names))
            if debug:
                for p in picks:
                    p["_debug"] = {"pick": True}
            rest_hits = [h for h in hits if not _dup(h)]
            # When the query named a thing and it was found, it and whatever
            # carries its name (the bars inside Grand Central) stay first.
            lead = ([h for h in rest_hits if tier_by_id.get(id(h), 2) <= 1]
                    if 0 in tiers else [])
            lead_ids = {id(h) for h in lead}
            hits = (lead + picks + [h for h in rest_hits if id(h) not in lead_ids])[:max(limit, len(picks))]

    # Some listings carry stray whitespace and CRLFs in their names.
    for h in hits:
        if isinstance(h.get("name"), str):
            h["name"] = " ".join(h["name"].split())

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

    latency_ms = (time.monotonic() - start) * 1000
    asyncio.create_task(_log_query(q, intent, latency_ms, [h["id"] for h in hits if h.get("id")]))

    resp = {
        "intent": intent,
        "header": header,
        "facets": facets,
        "hits": hits,
    }
    # The query asks what is on. Events live client-side (Resident Advisor),
    # so the backend only says so, and which genres.
    if interp and interp.get("events"):
        resp["events"] = {"genres": interp.get("genres") or [],
                          "kinds": interp.get("kinds") or [],
                          "when": interp.get("when")}
    if debug:
        resp["_timing"] = {
            "first_pass_ms": round((t_first - start) * 1000),
            "llm_wait_ms": round((t_llm - t_first) * 1000),
            "expansion_ms": round((t_expand - t_llm) * 1000),
            "total_ms": round(latency_ms),
            "direct": direct, "pre_expand": pre_expand,
            "local_matches": widened,
            "local_names": [h.get("name") for h in _local][:10] if widened is not None else None,
            "expansions": expansion_queries,
        }
    # An answer given before the rewrite arrived is incomplete; caching it
    # would serve the pick-less version for five minutes after the picks
    # exist.
    if not debug and not (interp is None and interp_task is not None):
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


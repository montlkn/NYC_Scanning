"""
RAG Router - Retrieves historical context from NYC Landmarks PDF chunks
"""

import os

from fastapi import APIRouter, HTTPException, Query, Request
from typing import List, Optional
import psycopg2
from psycopg2.extras import RealDictCursor

from utils.rate_limit import limiter, LIMIT_SEARCH

router = APIRouter(prefix="/rag", tags=["rag"])


def get_connection():
    url = os.environ.get("FOOTPRINTS_DB_URL")
    if not url:
        raise HTTPException(
            status_code=503,
            detail="RAG database not configured (FOOTPRINTS_DB_URL unset)",
        )
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


@router.get("/search")
@limiter.limit(LIMIT_SEARCH)
async def search_landmark_chunks(
    request: Request,
    building_name: Optional[str] = Query(None, description="Building name to search for"),
    bin: Optional[str] = Query(None, description="BIN — exact, and strongly preferred"),
    limit: int = Query(3, description="Max chunks to return"),
) -> List[dict]:
    """
    Landmark chunks for a building, from NYC Landmarks Commission reports.

    Prefer `bin`. Name matching is a substring ILIKE and measured at only 43%
    hit rate on a random sample of the catalogue — the misses were name-match
    failures, not missing corpus, since chunks are BIN-keyed at ingest. Name is
    kept as a fallback for callers that genuinely have no BIN.

    Building-specific chunks always outrank district-level ones. Most buildings
    in a historic district share a single generic district blurb, so without
    that ordering an entire neighbourhood reads identically.
    """
    if not building_name and not bin:
        raise HTTPException(status_code=400, detail="bin or building_name required")
    try:
        conn = get_connection()
        cur = conn.cursor()

        if bin:
            cur.execute(
                """
                SELECT id, building_name, bin, bbl, address, chunk_text,
                       source_file, page_number, specificity
                FROM landmark_chunks
                WHERE replace(bin, '.0', '') = replace(%s, '.0', '')
                ORDER BY (specificity = 'building') DESC NULLS LAST, chunk_index
                LIMIT %s
                """,
                (bin, limit),
            )
            rows = cur.fetchall()
            if rows:
                cur.close()
                conn.close()
                return [dict(r) for r in rows]

        if not building_name:
            cur.close()
            conn.close()
            return []

        cur.execute(
            """
            SELECT id, building_name, bin, bbl, address, chunk_text,
                   source_file, page_number, specificity
            FROM landmark_chunks
            WHERE building_name ILIKE %s
            ORDER BY (specificity = 'building') DESC NULLS LAST, chunk_index
            LIMIT %s
            """,
            (f"%{building_name}%", limit),
        )

        rows = cur.fetchall()
        cur.close()
        conn.close()

        return [dict(row) for row in rows]

    except HTTPException:
        raise
    except Exception as e:
        print(f"[RAG] Search error: {e}")
        return []


@router.get("/batch")
@limiter.limit(LIMIT_SEARCH)
async def search_batch(
    request: Request,
    building_names: str = Query(..., description="Comma-separated building names"),
    limit: int = Query(3, description="Max chunks per building"),
) -> dict:
    """
    Search for landmark chunks for multiple buildings.
    Returns a map of building name -> chunks.
    """
    names = [n.strip() for n in building_names.split(",") if n.strip()]
    results = {}

    try:
        conn = get_connection()
        cur = conn.cursor()

        for name in names:
            cur.execute(
                """
                SELECT chunk_text
                FROM landmark_chunks
                WHERE building_name ILIKE %s
                ORDER BY chunk_index
                LIMIT %s
                """,
                (f"%{name}%", limit),
            )
            rows = cur.fetchall()
            results[name] = [row["chunk_text"] for row in rows]

        cur.close()
        conn.close()

    except Exception as e:
        print(f"[RAG] Batch search error: {e}")

    return results

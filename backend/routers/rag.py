"""
RAG Router - Retrieves historical context from NYC Landmarks PDF chunks
"""

from fastapi import APIRouter, Query
from typing import List, Optional
import psycopg2
from psycopg2.extras import RealDictCursor

router = APIRouter(prefix="/rag", tags=["rag"])

RAILWAY_URL = "postgres://postgres:FgefB6c14fGCGbG4EdEb2a3D2F4b4cEB@metro.proxy.rlwy.net:56050/railway"


def get_connection():
    return psycopg2.connect(RAILWAY_URL, cursor_factory=RealDictCursor)


@router.get("/search")
async def search_landmark_chunks(
    building_name: str = Query(..., description="Building name to search for"),
    limit: int = Query(3, description="Max chunks to return"),
) -> List[dict]:
    """
    Search for landmark chunks by building name.
    Returns historical context from NYC Landmarks Commission reports.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()

        # Use ILIKE for case-insensitive partial matching
        cur.execute(
            """
            SELECT id, building_name, bin, bbl, address, chunk_text,
                   source_file, page_number
            FROM landmark_chunks
            WHERE building_name ILIKE %s
            ORDER BY chunk_index
            LIMIT %s
            """,
            (f"%{building_name}%", limit),
        )

        rows = cur.fetchall()
        cur.close()
        conn.close()

        return [dict(row) for row in rows]

    except Exception as e:
        print(f"[RAG] Search error: {e}")
        return []


@router.get("/batch")
async def search_batch(
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

"""
Web-search router — capped Brave search, for clients that must not hold a key.

Why this exists
───────────────
The iOS app used to attach a model-side `web_search` tool to five different
calls (building narratives, place narratives, lore contribution research and
verification, colloquial expansion). That is AGENTIC search: the model decides
how many queries to run, and nobody downstream can see or cap the number. It is
the same pattern that reached 13-24 queries and $0.55 on a single building in
the lore pipeline, which is why that pipeline replaced it with a capped Brave
fan-out. This endpoint gives the client the same deal.

Two properties the tool call could not offer:

  * The query count is OURS. `brave_search.MAX_QUERIES` bounds it, server-side,
    where the client cannot raise it. A compromised or modified client cannot
    turn one tap into fifty billed searches.
  * Citations become deterministic. The client synthesises from exactly the
    snippets returned here, so a returned URL is the page the prose was written
    from — not a link a model chose to attach afterwards. The whole class of
    dead, wrong-building, and landing-page citations cannot occur.

The Brave key stays server-side. Shipping it in the bundle would recreate the
problem the LLM proxy was built to solve.
"""

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from services import brave_search

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger(__name__)


class SourcesRequest(BaseModel):
    """Structured subject fields, NOT a free-text query.

    Deliberate: `build_queries` composes literal queries from known fields, so
    the shape of a query is fixed here rather than chosen by a caller. A free
    `q` would make this an open search proxy — anyone with a user token could
    bill our Brave subscription for arbitrary searches.
    """
    building_name: Optional[str] = None
    address: Optional[str] = None
    architect: Optional[str] = None
    year_built: Optional[str] = None
    categories: Optional[list[str]] = Field(default=None, max_length=32)


class SourcesResponse(BaseModel):
    source_text: Optional[str] = None
    sources: list[str] = []
    queries_run: int = 0
    configured: bool = True


@router.post("/sources", response_model=SourcesResponse)
async def get_sources(req: SourcesRequest) -> SourcesResponse:
    """Run the capped fan-out and return snippets plus their URLs.

    Always 200. A search miss, an unconfigured key and a subject too thin to
    query are all "no sources" to the caller, which falls back to writing from
    the fields it already holds — the same contract the lore chain uses. Raising
    here would turn a degraded narrative into a visible error for something the
    user cannot act on.

    `configured` is reported separately so a missing key is distinguishable from
    a genuine no-results, which is exactly the distinction that cost a debugging
    session when it was invisible.
    """
    if not brave_search.is_configured():
        logger.warning("[websearch] BRAVE_API_KEY not set — returning no sources")
        return SourcesResponse(configured=False)

    if not (req.building_name or req.address):
        # build_queries needs a subject to quote; without one it would either
        # return nothing or fall back to a year-only query that matches the
        # whole city. Not worth a billed request.
        return SourcesResponse(queries_run=0)

    queries = brave_search.build_queries(
        req.building_name, req.address, req.architect,
        req.year_built, categories=req.categories,
    )
    results = brave_search.filter_relevant(
        await brave_search.search(queries), req.building_name, req.address,
    )
    return SourcesResponse(
        source_text=brave_search.as_source_text(results),
        sources=brave_search.source_urls(results),
        queries_run=len(queries),
    )

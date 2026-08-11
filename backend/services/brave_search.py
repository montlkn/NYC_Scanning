"""
Brave Search client — the paid tier, for buildings no free source covers.

Why a search provider at all
────────────────────────────
Tiers 1 and 2 (LPC designation reports, Wikipedia) cover the landmarked
catalogue: 20,497 of 34,999 BINs now carry building-specific designation text.
But there is no designation report for a PLUTO building or a POI, and there
never will be. For those, search is the only source that exists — this is a
COVERAGE argument, not a cost one.

Why Brave over the alternatives (checked 2026-08):
  Brave      $5 / 1k        <- chosen
  Exa        $3-7 / 1k depending on plan
  Bing       RETIRED August 2025
  Google CSE closed to new customers
  SerpAPI    materially more expensive

Why NOT an agentic search model
───────────────────────────────
The path this replaces asked a model to search on its own, which spawned 13-24
internal searches per building and once cost $0.55 for a single building. The
count was set by the model, so there was no ceiling. Here the queries are
literal, fixed in number, and fired by us: N queries in, N billed, always.

The results are handed to the normal synthesis step with `webSearch` off. The
model never chooses what to search for and never sees a search tool.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

# Hard ceiling on queries per building. The whole point of moving off agentic
# search is that this number is OURS. Raising it raises the bill linearly and
# predictably, which is the property that was missing before.
MAX_QUERIES = 3
RESULTS_PER_QUERY = 5


def is_configured() -> bool:
    return bool(BRAVE_API_KEY)


def build_queries(building_name: Optional[str], address: Optional[str],
                  architect: Optional[str], year_built: Optional[str]) -> list[str]:
    """Literal queries from what we already know, most specific first.

    Deliberately not model-generated: a model writing its own queries is how the
    old path ended up running twenty of them.
    """
    name = (building_name or "").strip()
    addr = (address or "").strip()
    arch = (architect or "").strip()
    if arch.lower() in ("not determined", "unknown"):
        arch = ""

    queries: list[str] = []
    if name and name != "0":
        queries.append(f'"{name}" New York building history')
    if addr:
        queries.append(f'"{addr}" New York City building history')
    if arch and (addr or name):
        queries.append(f'{arch} architect "{addr or name}"')
    if not queries and year_built:
        queries.append(f"New York City building built {year_built} history")

    # De-dupe while preserving order, then cap.
    seen = set()
    out = []
    for q in queries:
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q)
    return out[:MAX_QUERIES]


async def search(queries: list[str], timeout_s: float = 12.0) -> list[dict]:
    """Run each query once. Returns [{title, url, description}], de-duplicated
    by URL. Never raises — a search failure degrades to no lore, not an error."""
    if not BRAVE_API_KEY or not queries:
        return []

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    seen_urls: set[str] = set()
    out: list[dict] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for q in queries[:MAX_QUERIES]:
            try:
                resp = await client.get(
                    BRAVE_URL, headers=headers,
                    params={"q": q, "count": RESULTS_PER_QUERY},
                )
            except Exception as e:
                logger.warning(f"brave query failed ({q[:40]}): {e}")
                continue
            if resp.status_code != 200:
                logger.warning(f"brave {resp.status_code} for {q[:40]}: "
                               f"{resp.text[:160]}")
                continue
            try:
                results = (resp.json().get("web") or {}).get("results") or []
            except Exception:
                continue
            for r in results:
                url = r.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                out.append({
                    "title": (r.get("title") or "").strip(),
                    "url": url,
                    "description": (r.get("description") or "").strip(),
                })

    logger.info(f"[BRAVE] {len(queries)} queries -> {len(out)} unique results")
    return out


def as_source_text(results: list[dict], max_chars: int = 3000) -> Optional[str]:
    """Format results as source material for synthesis.

    Titles and snippets only — we do not fetch the pages. Snippets are thinner
    than full text, but they are what the price buys, and the synthesis step is
    told to use nothing beyond them.
    """
    if not results:
        return None
    lines = []
    total = 0
    for r in results:
        line = f"- {r['title']}: {r['description']} ({r['url']})"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) if lines else None


def source_urls(results: list[dict], limit: int = 3) -> list[str]:
    """URLs actually handed to the model, for a deterministic SOURCES block.

    These are ours, not the model's: it cannot cite a page it was never given,
    which removes the whole class of invented and wrong-building citations.
    """
    return [r["url"] for r in results[:limit]]

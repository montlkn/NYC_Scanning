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
# predictably, which is the property that was missing before — an agentic model
# choosing 24 queries could not be reasoned about at all, whereas this is
# multiplication.
#
# 4, not 3. At 3 the list below was cut exactly where the architect query sat,
# so the highest-value citation we can produce was never searched for. The
# cheapest fix for "the results are weak" is asking a DIFFERENT question, not
# asking the same ones again — each query here attacks the subject from its own
# angle (identity, architect, address, story), so a fourth is a genuinely new
# chance rather than a retry.
#
# Keep this in step with the cost per building: queries x (plan rate). Raising
# it is a deliberate spend decision, which is exactly the property agentic
# search denied us.
MAX_QUERIES = 4
RESULTS_PER_QUERY = 5


def is_configured() -> bool:
    return bool(BRAVE_API_KEY)


def build_queries(building_name: Optional[str], address: Optional[str],
                  architect: Optional[str], year_built: Optional[str],
                  categories: Optional[list[str]] = None) -> list[str]:
    """Literal queries from what we already know, most specific first.

    Deliberately not model-generated: a model writing its own queries is how the
    old path ended up running twenty of them.

    `categories` turns the last slot into a STORY SWEEP. The first two queries
    establish identity — who built it, when — which a designation report already
    gives us for most buildings. The interesting registers (crime, disaster,
    supernatural, pop culture) need to be asked for, or search returns the same
    architectural summary three times.

    One sweep rather than one query per category: Brave bills per REQUEST, so
    eight category queries would be eight times the cost for the same building.
    OR-ing them into a single request keeps the ceiling at 3.

    The terms come from the live `lore` category list in the search index, not a
    list written here — add a category to the data and the sweep picks it up.
    """
    name = (building_name or "").strip()
    addr = (address or "").strip()
    arch = (architect or "").strip()
    if arch.lower() in ("not determined", "unknown"):
        arch = ""

    subject = f'"{name}"' if (name and name != "0") else (f'"{addr}"' if addr else "")

    # ORDER IS THE CAP. Everything past MAX_QUERIES is silently discarded
    # below, so this list is a priority ranking, not a wish list.
    #
    # The architect query used to sit LAST and was therefore dropped for every
    # named building that also had an address and categories — which is most of
    # the interesting ones. That silently defeated the client's whole citation
    # ranking: `sourcesBlock` scores firm domains FIRST and takes an `architect`
    # argument purely to bubble them up, but the firm page could never be in the
    # result set because nothing ever searched for it. It is second now: a
    # building's architect page is the single highest-value citation we can
    # return, and it is the one an encyclopedia link cannot substitute for.
    queries: list[str] = []
    if name and name != "0":
        queries.append(f'"{name}" New York building history')
    if arch and (name or addr):
        queries.append(f'{arch} architect "{name or addr}"')
    if addr and addr != name:
        queries.append(f'"{addr}" New York City building history')
    if categories and subject:
        # Story sweep — see docstring. Category slugs are used verbatim so the
        # query tracks the data; jargon slugs still narrow the result set
        # usefully when OR-ed against a quoted address.
        terms = " OR ".join(sorted({c.strip() for c in categories if c and c.strip()}))
        if terms:
            queries.append(f"{subject} New York {terms}")
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


def _dedupe_key(url: str) -> str:
    """Collapse URLs that are the same page wearing different clothes.

    Exact-URL dedupe left near-duplicates in place, and because the caller only
    keeps the top few, a duplicate does not merely repeat — it EVICTS a
    different source. Observed on VIA 57 West, where two casings of the same
    Wikipedia article and two cityrealty URLs differing by one path segment
    took all four citation slots between them.

    Two normalisations, both conservative:
      * lowercase — `/wiki/VIA_57_West` and `/wiki/Via_57_West` are one article
      * host + last path segment — `/review/57292` and `/57292` are one page,
        the extra segment being a site's own view of its record

    The second is the aggressive one, so it is applied only when that segment
    is distinctive (>= 4 chars): collapsing on `/en` or `/1` would merge pages
    that genuinely differ.
    """
    u = url.strip().lower().rstrip("/")
    u = u.split("#", 1)[0].split("?", 1)[0]
    try:
        rest = u.split("://", 1)[1]
    except IndexError:
        return u
    host, _, path = rest.partition("/")
    host = host.removeprefix("www.")
    segs = [s for s in path.split("/") if s]
    if segs and len(segs[-1]) >= 4:
        return f"{host}/{segs[-1]}"
    return f"{host}/{path}"


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
    # Per-query buckets, interleaved at the end. Appending query-by-query into
    # one flat list looked harmless but was not: callers truncate (source_urls
    # takes the first 3, as_source_text fills a char budget), so a flat list
    # meant the truncation fell entirely inside QUERY 1 and every later query
    # was structurally unable to contribute a citation. Reordering the queries
    # could not fix that -- whichever query ran first took every slot.
    buckets: list[list[dict]] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for q in queries[:MAX_QUERIES]:
            bucket: list[dict] = []
            buckets.append(bucket)
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
                if not url:
                    continue
                key = _dedupe_key(url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                bucket.append({
                    "title": (r.get("title") or "").strip(),
                    "url": url,
                    "description": (r.get("description") or "").strip(),
                })

    # Round-robin: every query's best result before any query's second. The
    # architect query is the reason this matters — its top hit is usually the
    # firm's own page, the highest-value citation available and the one the
    # client's ranking exists to promote, and under the old flat order it never
    # survived truncation.
    out: list[dict] = []
    for rank in range(RESULTS_PER_QUERY):
        for bucket in buckets:
            if rank < len(bucket):
                out.append(bucket[rank])

    logger.info(
        f"[BRAVE] {len(queries)} queries -> {len(out)} unique results "
        f"(per-query: {[len(b) for b in buckets]})"
    )
    return out


# Domains that answer any address query with a generated page and no history:
# listing sites, rental aggregators, data scrapers. They rank well and say
# nothing, and a snippet from one reads to the model as real material about the
# building — the failure is silent because the prose that results is fluent.
_JUNK_HOSTS = (
    "zillow.com", "trulia.com", "streeteasy.com", "realtor.com", "redfin.com",
    "apartments.com", "rentcafe.com", "loopnet.com", "propertyshark.com",
    "yelp.com", "tripadvisor.com", "mapquest.com", "zolo.com",
)


# Words that carry no identifying power here: they appear in a large share of
# NYC building names AND in unrelated pages, so matching on one is the same as
# not filtering. Left as a literal set rather than derived — this is a property
# of English and NYC addressing, not of our data, so there is nothing to query.
_GENERIC_TOKENS = frozenset({
    "building", "buildings", "house", "tower", "towers", "center", "centre",
    "hall", "apartments", "apartment", "street", "avenue", "place", "road",
    "york", "city", "york's", "west", "east", "north", "south", "the", "and",
})


def filter_relevant(results: list[dict], building_name: Optional[str],
                    address: Optional[str]) -> list[dict]:
    """Drop results that are obviously not about this subject.

    Answers the "what if the results are junk?" case, which is NOT the same as
    the empty case: empty falls through to the next tier honestly, whereas junk
    gets synthesized into confident prose about the wrong building. Address
    queries in particular pull listing sites that carry the address and no
    history at all.

    Deliberately conservative, in two ways. It only removes a result on POSITIVE
    evidence of irrelevance — a known junk host, or a total miss on every
    subject token — rather than scoring relevance and keeping the top N. And it
    NEVER returns empty when it was given input: if every result fails, the
    filter is more likely wrong than the whole result set, so the originals are
    returned and the synthesis prompt's own grounding rules take over. A filter
    that can starve the model is worse than no filter.
    """
    if not results:
        return results

    tokens = set()
    for src in (building_name, address):
        for t in (src or "").lower().replace(",", " ").split():
            # Short tokens ("the", "st") match everything, and the generic
            # architecture vocabulary below appears in most NYC building names
            # AND most unrelated pages — matching on "building" makes the check
            # meaningless, which is what the first version of this did.
            if len(t) >= 4 and not t.isdigit() and t not in _GENERIC_TOKENS:
                tokens.add(t)
    # A house number IS distinctive when present, unlike a bare short word.
    for t in (address or "").split():
        if t.isdigit() and len(t) >= 2:
            tokens.add(t)

    kept = []
    for r in results:
        host = (r.get("url") or "").lower()
        if any(j in host for j in _JUNK_HOSTS):
            continue
        if tokens:
            hay = f"{r.get('title','')} {r.get('description','')}".lower()
            if not any(t in hay for t in tokens):
                continue
        kept.append(r)

    if not kept:
        logger.info(
            f"[BRAVE] relevance filter rejected all {len(results)} results; "
            f"keeping originals rather than starving synthesis"
        )
        return results
    if len(kept) < len(results):
        logger.info(f"[BRAVE] relevance filter {len(results)} -> {len(kept)}")
    return kept


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


def source_urls(results: list[dict], limit: int = 4) -> list[str]:
    """URLs actually handed to the model, for a deterministic SOURCES block.

    These are ours, not the model's: it cannot cite a page it was never given,
    which removes the whole class of invented and wrong-building citations.

    `limit` tracks MAX_QUERIES because `search` returns results round-robin
    across queries: at 4 the top hit from each of the four angles (identity,
    architect, address, story) gets a slot. At the old 3 the story sweep was
    always cut, and before the interleave the first three slots were all query
    one — so the architect and address angles could never appear at all.
    """
    return [r["url"] for r in results[:limit]]

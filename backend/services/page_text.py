"""
Fetch and flatten the pages Brave found, instead of writing from its blurbs.

Why
───
Brave returns a ~200-character preview per result, not the page. That is the
ceiling the lore quality kept hitting, and it is invisible until you diff the
snippet against the article. For the Graham Home the preview carried "rehab to
condos" and the word "Fayette"; the actual Brownstoner page carries the
building's whole second life as the Bull Shippers Plaza Motor Inn, a hot-pillow
motel raided for drugs and prostitution, boarded up and painted black by 1985.
No number of extra queries surfaces that — only reading the page does.

The agentic search this pipeline replaced fetched pages, which is why its
narratives had stories and ours had façade description. Reading is the half we
dropped; the capped querying is fine.

Cost: NOTHING against the search quota. A fetch is bandwidth and latency, not a
billed query. That asymmetry is the whole argument — reading two pages we have
already paid to find is free, while finding two more pages is not.

No new dependency. The extraction is deliberately crude regex rather than a
parser: this runs on Railway, a wheel that fails to build takes the service
down, and "strip the tags and collapse the whitespace" does not need an HTML
tree. It is fed to a model that tolerates mess, not to a renderer.
"""

import asyncio
import html
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Per-page ceiling handed to the model. A designation report already supplies
# the fabric; these pages are here for the story, and the story is near the top
# of an article. Taking everything would bury it and inflate the prompt.
MAX_CHARS_PER_PAGE = 4000
FETCH_TIMEOUT_S = 6.0
MAX_PAGES = 2
MAX_BYTES = 2_000_000

# Some hosts are never worth the round trip: PDFs we cannot flatten here, media,
# and the listing sites already filtered out of results for having no history.
_SKIP_HOSTS = (
    "flickr.com", "instagram.com", "youtube.com", "pinterest.",
    "zillow.com", "streeteasy.com", "trulia.com", "realtor.com",
)

_SCRIPTY = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|form|aside)[^>]*>.*?</\1>",
    re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n\s*\n+")


def _flatten(raw_html: str) -> str:
    """HTML → readable-ish text. Crude on purpose; see module docstring."""
    t = _SCRIPTY.sub(" ", raw_html)
    # Turn block boundaries into newlines BEFORE dropping tags, or every
    # paragraph runs into the next and the model reads one giant sentence.
    t = re.sub(r"</(p|div|li|h[1-6]|tr|section|article)>", "\n", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    t = html.unescape(t)
    t = _WS.sub(" ", t)
    t = _NL.sub("\n\n", t)
    return t.strip()


async def _fetch_one(client: httpx.AsyncClient, url: str) -> Optional[str]:
    low = url.lower()
    if any(h in low for h in _SKIP_HOSTS) or low.endswith(".pdf"):
        return None
    try:
        resp = await client.get(url, follow_redirects=True, headers={
            # Some publishers serve a stub to an unknown agent. This is a plain
            # identifying UA, not an attempt to look like a person.
            "User-Agent": "JinkLoreBot/1.0 (+https://jinkapp.co; contact@jinkapp.co)",
            "Accept": "text/html,application/xhtml+xml",
        })
    except Exception as e:
        logger.info(f"[pagetext] fetch failed {url[:60]}: {e}")
        return None

    if resp.status_code != 200:
        logger.info(f"[pagetext] {resp.status_code} {url[:60]}")
        return None
    if "html" not in (resp.headers.get("content-type") or "").lower():
        return None
    if len(resp.content) > MAX_BYTES:
        return None

    text = _flatten(resp.text)
    # A page that flattens to almost nothing is a JS shell or a bot wall; its
    # snippet was more use than its body.
    if len(text) < 400:
        return None
    return text[:MAX_CHARS_PER_PAGE]


async def fetch_pages(urls: list[str], limit: int = MAX_PAGES) -> list[tuple[str, str]]:
    """Fetch up to `limit` URLs concurrently. Returns [(url, text)], never raises.

    Concurrent because these are independent hosts and the whole point is to
    overlap the latency; sequential fetching would add its cost to the critical
    path one page at a time.
    """
    picked = [u for u in urls if u][:limit]
    if not picked:
        return []
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, u) for u in picked), return_exceptions=True
        )
    out: list[tuple[str, str]] = []
    for url, res in zip(picked, results):
        if isinstance(res, str) and res:
            out.append((url, res))
    logger.info(f"[pagetext] fetched {len(out)}/{len(picked)} pages")
    return out


def as_source_text(pages: list[tuple[str, str]], max_chars: int = 7000) -> Optional[str]:
    """Format fetched pages as source material, each labelled with its URL.

    Labelled so the synthesis can attribute correctly and so a citation always
    corresponds to text the model actually read — the property the snippet-only
    path could not offer.
    """
    if not pages:
        return None
    parts, total = [], 0
    for url, text in pages:
        block = f"--- FULL PAGE: {url} ---\n{text}\n"
        if total + len(block) > max_chars:
            block = block[: max(0, max_chars - total)]
            if len(block) < 200:
                break
        parts.append(block)
        total += len(block)
    return "\n".join(parts) if parts else None

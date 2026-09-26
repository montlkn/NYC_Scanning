"""Build a short, sourced description ("card") for each venue in an area.

Why this exists
───────────────
A venue row is a name, an FSQ category and a host building. Nothing in it says
Clockwork is a punk bar or Clandestino is a scene, so "punk bar" and "sceney
LES bars" had nothing to match and fell back to the nearest bars. The fix is not
a vibe vocabulary (no hand list could keep up with "cutty"); it is evidence.
The card is plain prose taken from what the web says about the place, and at
query time a model reads the cards and judges them against the query
(see services/vibe_judge.py). Slang is understood by the judge, never mapped.

How
───
1. Venues come from the public /api/search/venues/nearby endpoint on a grid,
   so this needs no database access (the search DB is on Railway's private
   network).
2. One Brave query per venue, ten results. Cost is one query per venue:
   $5 / 1k, so a few hundred venues is a few dollars.
3. The model writes the card from the snippets only, and says whether the
   snippets are about this venue at all and whether it has closed.

Output is JSONL, one row per venue, appended as it goes and resumable: a rerun
skips fsq_ids already written. The backend loads the file at startup.

    cd backend && railway run python -m scripts.build_venue_cards \
        --area "Lower East Side / East Village" \
        --bbox 40.7120,-73.9960,40.7340,-73.9750
"""

import argparse
import asyncio
import json
import math
import os
import sys
from typing import Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.openai_text import openai_text  # noqa: E402

API = os.environ.get("JINK_API", "https://nycscanning-production.up.railway.app")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "cards", "venue_cards.jsonl")

# Pilot scope: places people go out to drink, dance or hear music. Read off
# /api/search/venues/categories (2026-09-26), not invented; pass --categories
# to change it. Juice bars, salad bars and wine STORES are excluded on purpose.
DEFAULT_CATEGORIES = [
    "Bar", "Lounge", "Music Venue", "Cocktail Bar", "Night Club", "Wine Bar",
    "Pub", "Sports Bar", "Dive Bar", "Hookah Bar", "Karaoke Bar", "Beer Garden",
    "Hotel Bar", "Speakeasy", "Gastropub", "Brewery", "Gay Bar", "Comedy Club",
    "Rock Club", "Jazz and Blues Venue", "Beer Bar", "Dance Club", "Irish Pub",
    "Tapas Bar", "Whisky Bar", "Whiskey Bar", "Rooftop Bar", "Sake Bar",
    "Piano Bar", "Tiki Bar", "Salsa Club", "Champagne Bar", "Cigar Bar",
]

CARD_SYSTEM = """You describe one New York City venue for a search engine that matches places to the mood and scene people ask for.

You get the venue's name, its listed category (often wrong: bars are filed as restaurants and the reverse), the area it is in, and web search results. Use ONLY what the search results say. Never add anything from memory.

Reply with JSON only:
{"match": true|false, "closed": true|false, "kind": "...", "card": "..."}

match: true only if the results are clearly about THIS venue in this area of New York. A different business with a similar name, a different city, or results that are just directory listings with no description all mean false.
closed: true if a result says it has permanently closed.
kind: what the place actually is, 1 to 4 plain words, from the results, not the listed category ("dive bar", "cocktail bar", "Chinese restaurant", "music venue", "wine bar and restaurant"). Empty string when match is false.
card: 2 to 3 sentences, at most 70 words. Say what the place is like: crowd, scene, music, decor, drinks, price, reputation, history. Keep the vivid words reviewers and writers used ("grungy", "sceney", "punk", "fashion crowd", "divey", "candlelit", "downtown it-crowd"), because those are what people search for. Keep the names of scenes, micro-neighborhoods and movements the results tie it to, and say if it is newly fashionable. Prefer recent results over old ones when they disagree. Plain, specific, no marketing tone. Never mention the search results themselves or what they lack. Empty string when match is false, and also when the results only list an address, hours or category without saying what the place is like."""


def grid(bbox: list[float], step_m: float) -> list[tuple[float, float]]:
    s, w, n, e = bbox
    dlat = step_m / 111_320
    dlng = step_m / (111_320 * math.cos(math.radians((s + n) / 2)))
    pts, lat = [], s
    while lat <= n + 1e-9:
        lng = w
        while lng <= e + 1e-9:
            pts.append((round(lat, 6), round(lng, 6)))
            lng += dlng
        lat += dlat
    return pts


async def fetch_venues(bbox: list[float], cats: list[str]) -> list[dict]:
    """Every venue of these categories in the bbox, via the public API.

    The API allows 60 requests a minute and 2,000 a day per IP, so requests
    are paced, a 429 waits out the window, and the list is cached on disk so
    a rerun does not fetch it again."""
    import hashlib
    tag = hashlib.md5(",".join(sorted(cats)).encode()).hexdigest()[:8]
    cache = os.path.join(os.path.dirname(OUT), f"venues_{'_'.join(map(str, bbox))}_{tag}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    step = 200.0
    radius = step * 0.75  # circles overlap to cover the square cells
    seen: dict[str, dict] = {}
    s, w, n, e = bbox
    async with httpx.AsyncClient(timeout=30) as c:
        async def cell(lat: float, lng: float, rad: float) -> None:
            for _ in range(3):
                r = await c.get(f"{API}/api/search/venues/nearby", params={
                    "lat": lat, "lng": lng, "radius_m": rad, "limit": 100,
                    "categories": ",".join(cats)})
                if r.status_code != 429:
                    break
                await asyncio.sleep(61)
            await asyncio.sleep(1.1)
            if r.status_code != 200:
                print(f"nearby {r.status_code} at {lat},{lng}", file=sys.stderr)
                return
            rows = r.json()
            for v in rows:
                if s <= v["lat"] <= n and w <= v["lng"] <= e:
                    seen.setdefault(v["fsq_id"], v)
            # A full page means the cell holds more than one request returns:
            # split it into four smaller overlapping circles.
            if len(rows) >= 100:
                if rad < 40:
                    print(f"warning: cell {lat},{lng} still full at {rad:.0f}m", file=sys.stderr)
                    return
                off = rad / 2
                dlat = off / 111_320
                dlng = off / (111_320 * math.cos(math.radians(lat)))
                for a, b in ((dlat, dlng), (dlat, -dlng), (-dlat, dlng), (-dlat, -dlng)):
                    await cell(lat + a, lng + b, rad * 0.75)
        for lat, lng in grid(bbox, step):
            await cell(lat, lng, radius)
    out = list(seen.values())
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w") as f:
        json.dump(out, f)
    return out


async def brave(client: httpx.AsyncClient, q: str, tries: int = 0) -> list[dict]:
    r = await client.get(BRAVE_URL, params={"q": q, "count": 10}, headers={
        "Accept": "application/json",
        "X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
    if r.status_code == 429:
        await asyncio.sleep(2)
        return await brave(client, q, tries)
    # Right after a budget change Brave answered 402 to a burst while single
    # requests went through, so a 402 is retried before it is believed.
    if r.status_code == 402 and tries < 3:
        await asyncio.sleep(10)
        return await brave(client, q, tries + 1)
    if r.status_code == 402:
        # Out of plan quota. Every later query fails the same way, and
        # writing them as "no results" would bury real bars as unknown.
        raise SystemExit(f"brave quota exhausted: {r.text[:400]}")
    if r.status_code != 200:
        print(f"brave {r.status_code}: {r.text[:120]}", file=sys.stderr)
        return None
    out = []
    for x in (r.json().get("web") or {}).get("results") or []:
        desc = " ".join(filter(None, [x.get("description")] + (x.get("extra_snippets") or [])))
        out.append({"title": x.get("title", ""), "url": x.get("url", ""), "text": desc})
    return out


def parse_card(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    t = raw.strip().strip("`")
    t = t[t.find("{"):t.rfind("}") + 1]
    try:
        d = json.loads(t)
    except Exception:
        return None
    card = (d.get("card") or "").strip() if isinstance(d.get("card"), str) else ""
    kind = (d.get("kind") or "").strip() if isinstance(d.get("kind"), str) else ""
    return {"match": d.get("match") is True, "closed": d.get("closed") is True,
            "kind": kind[:40], "card": card[:600]}


async def card_for(client: httpx.AsyncClient, v: dict, area: str) -> Optional[dict]:
    """Two queries, two angles: where it is (the listed category is left out
    because it is often wrong: Upstairs Bar is filed as a Chinese
    restaurant), and what reviewers and press say."""
    first = await brave(client, f'"{v["name"]}" {area} NYC')
    second = await brave(client, f'"{v["name"]}" New York review')
    if first is None or second is None:
        return None  # a failed request is retried on the next run, not recorded
    results, seen = [], set()
    for r in first + second:
        if r["url"] not in seen:
            seen.add(r["url"])
            results.append(r)
    if not results:
        # Nothing on the web is not evidence the place is fake: found=False
        # keeps it visible, judged by name only.
        return {"match": False, "found": False, "closed": False, "kind": "", "card": "", "sources": []}
    src = "\n".join(f"- {r['title']}: {r['text'][:350]} ({r['url']})" for r in results[:14])
    user = (f"Venue: {v['name']}\nListed category: {v['category']}\n"
            f"Area: {area}, Manhattan, New York\n\nSearch results:\n{src}")
    card = parse_card(await openai_text(system=CARD_SYSTEM, user=user, max_tokens=500,
                                        timeout_s=30, cache_key="jink-venue-card-v3"))
    if card is None:
        return None
    card["found"] = True
    card["sources"] = [r["url"] for r in results[:6]]
    return card


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", required=True, help='e.g. "Lower East Side / East Village"')
    ap.add_argument("--bbox", required=True, help="south,west,north,east")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    ap.add_argument("--categories-regex", default="",
                    help="instead of --categories: every live category matching this regex")
    ap.add_argument("--exclude-regex", default="", help="drop categories matching this")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new venues")
    ap.add_argument("--dry-run", action="store_true", help="count venues, spend nothing")
    a = ap.parse_args()
    bbox = [float(x) for x in a.bbox.split(",")]
    cats = [c.strip() for c in a.categories.split(",") if c.strip()]
    if a.categories_regex:
        import re
        async with httpx.AsyncClient(timeout=30) as c:
            live = (await c.get(f"{API}/api/search/venues/categories")).json()
        rx = re.compile(a.categories_regex, re.I)
        ex = re.compile(a.exclude_regex, re.I) if a.exclude_regex else None
        cats = sorted({x["category"] for x in live if x.get("category") and rx.search(x["category"])
                       and not (ex and ex.search(x["category"]))})
        print(f"{len(cats)} categories match")

    venues = await fetch_venues(bbox, cats)
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            done = {json.loads(l)["fsq_id"] for l in f if l.strip()}
    todo = [v for v in venues if v["fsq_id"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(venues)} venues in area, {len(done)} already carded, {len(todo)} to do "
          f"(~${len(todo) * 2 * 5 / 1000:.2f} Brave, 2 queries each)")
    if a.dry_run or not todo:
        return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sem = asyncio.Semaphore(4)
    counts = {"match": 0, "nomatch": 0, "closed": 0, "failed": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=20) as client:
        async def one(v: dict) -> None:
            async with sem:
                card = await card_for(client, v, a.area)
            if card is None:
                counts["failed"] += 1
                return
            counts["match" if card["match"] else "nomatch"] += 1
            counts["closed"] += card["closed"]
            row = {"fsq_id": v["fsq_id"], "name": v["name"], "category": v["category"], **card}
            async with lock:
                with open(OUT, "a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        await asyncio.gather(*(one(v) for v in todo))
    print(counts)


if __name__ == "__main__":
    asyncio.run(main())

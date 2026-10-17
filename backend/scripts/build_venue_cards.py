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
2. Two Brave queries per venue, ten results each: $5 / 1k queries, so a few
   hundred venues is a few dollars.
3. Kit writes the card from the snippets only, says whether they are about
   this venue at all and whether it has closed, and the card is fact-checked
   against the snippets (services/venue_cards, services/kit_judge).

With SEARCH_DB_WRITE_URL set, cards go to the search DB's venue_cards table,
which the API reloads every 30 minutes. Without it they are appended to the
JSONL, which the API reads at startup. Resumable either way: a rerun skips
venues already carded. --import-jsonl copies the JSONL into the table once.

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

from services import venue_cards  # noqa: E402

API = os.environ.get("JINK_API", "https://nycscanning-production.up.railway.app")
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


async def card_for(client: httpx.AsyncClient, v: dict, area: str) -> Optional[dict]:
    """See services/venue_cards.write_card: the same writer, fact check
    included, that the nightly requested-venue job uses."""
    return await venue_cards.write_card(client, v, area)


def _db_url() -> Optional[str]:
    """Cards go to the search DB's venue_cards table when the owner login is
    available; the API reloads it, so no redeploy is needed. Without it they
    are appended to the JSONL as before."""
    return os.environ.get("SEARCH_DB_WRITE_URL")


def import_jsonl() -> None:
    """One-time: copy cards/venue_cards.jsonl into venue_cards."""
    import psycopg
    url = _db_url()
    if not url:
        raise SystemExit("set SEARCH_DB_WRITE_URL")
    rows = []
    with open(OUT) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                rows.append(venue_cards.upsert_params(r["fsq_id"], r.get("name"), r.get("category"), r))
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.executemany(venue_cards.UPSERT_SQL, rows)
        conn.commit()
    print(f"imported {len(rows)} cards")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", help='e.g. "Lower East Side / East Village"')
    ap.add_argument("--bbox", help="south,west,north,east")
    ap.add_argument("--import-jsonl", action="store_true", help="copy the JSONL into venue_cards and stop")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    ap.add_argument("--categories-regex", default="",
                    help="instead of --categories: every live category matching this regex")
    ap.add_argument("--exclude-regex", default="", help="drop categories matching this")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new venues")
    ap.add_argument("--dry-run", action="store_true", help="count venues, spend nothing")
    a = ap.parse_args()
    if a.import_jsonl:
        import_jsonl()
        return
    if not a.area or not a.bbox:
        ap.error("--area and --bbox are required")
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
    if _db_url():
        import psycopg
        with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
            cur.execute("SELECT fsq_id FROM venue_cards")
            done |= {r[0] for r in cur.fetchall()}
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
                if _db_url():
                    import psycopg
                    with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
                        cur.execute(venue_cards.UPSERT_SQL,
                                    venue_cards.upsert_params(v["fsq_id"], v["name"], v["category"], card))
                        conn.commit()
                else:
                    with open(OUT, "a") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        await asyncio.gather(*(one(v) for v in todo))
    print(counts)


if __name__ == "__main__":
    asyncio.run(main())

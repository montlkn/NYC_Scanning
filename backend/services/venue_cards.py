"""
Venue cards: what a venue is actually like, written by Kit from the web.

One writer for every job that makes cards (scripts/build_venue_cards.py for
an area, scripts/ingest_requested_venues.py for places people found through
Apple Maps). Same research rules as building lore (services/lore_generator):

  1. A fixed number of web queries we choose (two Brave searches), never a
     number a model chooses, and no tools attached to the writing call.
  2. Kit (gpt-5.6-luna) writes ONLY from those results, and says whether they
     are even about this venue and whether it has closed.
  3. The card then goes through services/kit_judge, the hostile fact checker
     the narratives use. A card that claims what the results do not say is
     stored with `judge` set and an empty `card`, so it never shows, and the
     venue falls back to being read through its building.

Cards live in the search DB's `venue_cards` table; the API reloads it every 30
minutes (services/vibe_judge), so a card written tonight shows tomorrow.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

import httpx

from services.kit_judge import judge
from services.openai_text import openai_text

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

CARD_SYSTEM = """You describe one New York City venue for a search engine that matches places to the mood and scene people ask for.

You get the venue's name, its listed category (often wrong: bars are filed as restaurants and the reverse), the area it is in, and web search results. Use ONLY what the search results say. Never add anything from memory.

Reply with JSON only:
{"match": true|false, "closed": true|false, "kind": "...", "card": "..."}

match: true only if the results are clearly about THIS venue in this area of New York. A different business with a similar name, a different city, or results that are just directory listings with no description all mean false.
closed: true if a result says it has permanently closed.
kind: what the place actually is, 1 to 4 plain words, from the results, not the listed category ("dive bar", "cocktail bar", "Chinese restaurant", "music venue", "wine bar and restaurant", "chess shop"). Empty string when match is false.
card: 2 to 3 sentences, at most 70 words. Say what the place is like: crowd, scene, music, decor, drinks, price, reputation, history. Keep the vivid words reviewers and writers used ("grungy", "sceney", "punk", "fashion crowd", "divey", "candlelit", "downtown it-crowd"), because those are what people search for. Keep the names of scenes, micro-neighborhoods and movements the results tie it to, and say if it is newly fashionable. Prefer recent results over old ones when they disagree. Plain, specific, no marketing tone. Never mention the search results themselves or what they lack. Empty string when match is false, and also when the results only list an address, hours or category without saying what the place is like."""


def is_configured() -> bool:
    return bool(os.environ.get("BRAVE_API_KEY"))


async def brave(client: httpx.AsyncClient, q: str, tries: int = 0) -> Optional[list]:
    """Ten results for one query, or None when the request failed (so the
    venue is retried on the next run instead of recorded as unknown)."""
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


async def write_card(client: httpx.AsyncClient, v: dict, area: str, *,
                     check: bool = True) -> Optional[dict]:
    """Card for one venue (`v` needs name and category), or None on a failed
    request. Two queries, two angles: where it is (the listed category is left
    out because it is often wrong: Upstairs Bar is filed as a Chinese
    restaurant), and what reviewers and press say."""
    first = await brave(client, f'"{v["name"]}" {area} NYC')
    second = await brave(client, f'"{v["name"]}" New York review')
    if first is None or second is None:
        return None
    results, seen = [], set()
    for r in first + second:
        if r["url"] not in seen:
            seen.add(r["url"])
            results.append(r)
    if not results:
        # Nothing on the web is not evidence the place is fake: found=False
        # keeps it visible, judged by name only.
        return {"match": False, "found": False, "closed": False, "kind": "", "card": "",
                "sources": [], "judge": None}
    src = "\n".join(f"- {r['title']}: {r['text'][:350]} ({r['url']})" for r in results[:14])
    user = (f"Venue: {v['name']}\nListed category: {v.get('category') or ''}\n"
            f"Area: {area}, New York\n\nSearch results:\n{src}")
    card = parse_card(await openai_text(system=CARD_SYSTEM, user=user, max_tokens=500,
                                        timeout_s=30, cache_key="jink-venue-card-v3"))
    if card is None:
        return None
    card["found"] = True
    card["sources"] = [r["url"] for r in results[:6]]
    card["judge"] = None
    # The fact check the narratives get. A rejected card keeps its match and
    # closed verdicts (those decide whether the venue shows at all) but loses
    # its text, so nothing unsupported is ever displayed or searched.
    if check and card["match"] and card["card"]:
        why = await judge(src, card["card"], label="venue card")
        if why:
            card["judge"] = why[:300]
            card["card"] = ""
    return card


UPSERT_SQL = """
INSERT INTO venue_cards (fsq_id, name, category, match, found, closed, kind, card, sources, judge, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (fsq_id) DO UPDATE SET
    name=EXCLUDED.name, category=EXCLUDED.category, match=EXCLUDED.match,
    found=EXCLUDED.found, closed=EXCLUDED.closed, kind=EXCLUDED.kind,
    card=EXCLUDED.card, sources=EXCLUDED.sources, judge=EXCLUDED.judge,
    updated_at=now()
"""


def upsert_params(fsq_id: str, name: str, category: Optional[str], card: dict) -> tuple:
    return (fsq_id, name, category, card.get("match", False), card.get("found", False),
            card.get("closed", False), card.get("kind") or None, card.get("card") or None,
            card.get("sources") or [], card.get("judge"))

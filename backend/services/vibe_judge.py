"""Vibe search: a model reads real descriptions of nearby venues and picks the
ones that fit the query.

Why
───
"punk bar", "sceney LES bars", "cutty bars": the words describe a scene, and
nothing in a venue row (name, category, host building) describes a scene. The
rewrite's `picks` asked the model to RECALL bars, and its recall was thin and
partly wrong: it never named Clockwork or Clandestino, both of which we hold.

This inverts it. Retrieval is plain and exact (the kind of place asked for, in
the place asked for); the model only JUDGES, reading each candidate's card
(scripts/build_venue_cards.py: a short description written from web sources).
Judging a supplied list is far more reliable than recalling one, and no vibe
vocabulary exists anywhere: "cutty" is understood by the model, not mapped.

Venues without a card are still offered by name, so the step degrades to name
recognition outside carded areas instead of switching off.
"""

import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text

from models.search_session import get_search_db
from services.openai_text import openai_text

logger = logging.getLogger(__name__)

CARDS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "cards", "venue_cards.jsonl")

# Candidates shown to the model. Carded ones go first; each line is ~60 tokens
# with a card and ~10 without, so 120 is roughly 5-7k input tokens.
MAX_CANDIDATES = 120
POOL = 800
RADIUS_M = 1500
JUDGE_TIMEOUT_S = 6.0
MAX_PICKS = 8
JUDGE_VERSION = 2
# Below this many carded candidates the judge would be choosing by names
# alone, which is worse than the rewrite's recalled picks: outside the
# carded area "chic bars" lost Bemelmans and Le Bain to whatever bar was
# nearest. 15 is a judgment call, not a measured threshold; revisit once
# more areas are carded.
MIN_CARDED = 15


def _load_cards() -> Dict[str, Dict[str, Any]]:
    cards: Dict[str, Dict[str, Any]] = {}
    try:
        with open(CARDS_PATH) as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    cards[row["fsq_id"]] = row
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[vibe] could not load venue cards: {e}")
    logger.info(f"[vibe] {len(cards)} venue cards loaded")
    return cards


_CARDS = _load_cards()


def card_text(fsq_id: Optional[str]) -> Optional[str]:
    """The description for a venue, when there is a usable one."""
    c = _CARDS.get(fsq_id or "")
    if c and c.get("match") and not c.get("closed") and c.get("card"):
        return c["card"]
    return None


def is_rejected(fsq_id: Optional[str]) -> bool:
    """Carded and found to be closed, or the web had results and none of them
    were this place (a hair salon filed as a Speakeasy). No results at all is
    not evidence, so it never hides a venue."""
    c = _CARDS.get(fsq_id or "")
    if not c:
        return False
    return bool(c.get("closed")) or (not c.get("match") and c.get("found") is True)


def _words(x: str) -> set:
    return {w for w in re.split(r"[^a-z]+", (x or "").lower()) if len(w) > 2}


def kind_matches(categories: List[str]) -> List[str]:
    """Carded venues whose real kind is the kind asked for, whatever their
    listed category: Upstairs Bar is filed as a Chinese restaurant, Le Dive
    as a French one. The card's `kind` ("bar and lounge") shares a word with
    a wanted category ("Bar")."""
    want = set().union(*(_words(c) for c in categories)) if categories else set()
    return [fid for fid, c in _CARDS.items()
            if want & _words(c.get("kind") or "") and card_text(fid)]


def label(r: dict) -> str:
    """What the place is, for the judge: the card's kind when there is one,
    since the listed category is often wrong."""
    c = _CARDS.get(r.get("fsq_id") or "")
    return (c or {}).get("kind") or r.get("category") or ""


def wants_judge(interp: Optional[Dict[str, Any]]) -> bool:
    return bool(interp and interp.get("vibe") and interp.get("categories")
                and interp.get("about") in ("places", "mixed"))


_JUDGE_SYSTEM = """You pick New York places that fit a search.

You get the search and a numbered list of real places near where the person is looking. Most have a short description taken from reviews and articles.

Reply with JSON only: {"picks": [{"n": <number>, "why": "..."}]}

Pick up to 8 places that genuinely fit what the search asks for, best fit first. Read slang and scene words the way a New Yorker means them, including what a neighborhood's scene implies for the places in it. Judge mainly from the description, and add what you reliably know about the place or its scene; a place with no description may be picked only if you know it well and are sure it fits. Fewer right answers beat a full list: return fewer, or [], rather than padding with places that merely match the kind of place. why: at most 10 plain words, taken from the description, saying why it fits (e.g. "Punk dive, graffiti walls, loud non-Top-40 music")."""


# Keyed on the query and where it was asked, so the same words in another
# neighborhood are judged again. In-process only: the answer depends on the
# cards, which change on deploy anyway.
_cache: "OrderedDict[str, tuple]" = OrderedDict()
_CACHE_MAX = 500
_CACHE_TTL_S = 6 * 3600


def _cache_key(q: str, hoods: List[str], lat: Optional[float], lng: Optional[float]) -> str:
    where = ",".join(sorted(h.lower() for h in hoods)) if hoods else (
        f"{lat:.2f},{lng:.2f}" if lat is not None and lng is not None else "city")
    return f"v{JUDGE_VERSION}|{q.strip().lower()}|{where}"


async def _candidates(categories: List[str], hoods: List[str],
                      lat: Optional[float], lng: Optional[float]) -> List[dict]:
    """The kind of place asked for, in the place asked for, nearest first.

    A named neighborhood wins over the map: "sceney LES bars" asked with the
    map on Hell's Kitchen means the Lower East Side."""
    cats = tuple(c.lower() for c in categories)
    extra = tuple(kind_matches(categories)) or ("-",)
    geo = lat is not None and lng is not None
    dist = ("6371000 * acos(GREATEST(-1, LEAST(1, cos(radians(:lat)) * cos(radians(lat)) "
            "* cos(radians(lng) - radians(:lng)) + sin(radians(:lat)) * sin(radians(lat)))))")
    base = ("SELECT fsq_id, name, category, lat, lng, bin, bbl, building_year, building_style, "
            "photo_url, snippet, neighborhood{d} FROM venues WHERE searchable IS NOT FALSE "
            "AND lat IS NOT NULL AND (lower(category) IN :cats OR fsq_id IN :extra)")
    rows: List[Any] = []
    async with get_search_db() as db:
        if db is None:
            return []
        if hoods:
            params: Dict[str, Any] = {"cats": cats, "extra": extra, "pool": POOL,
                                      "hoods": [f"%{h.lower()}%" for h in hoods]}
            sql = (base.format(d=f", {dist} AS dist_m" if geo else ", NULL AS dist_m")
                   + " AND lower(coalesce(neighborhood, '')) LIKE ANY(:hoods)"
                   + (" ORDER BY dist_m" if geo else "") + " LIMIT :pool")
            if geo:
                params.update(lat=lat, lng=lng)
            rows = (await db.execute(text(sql).bindparams(bindparam("cats", expanding=True),
                                                          bindparam("extra", expanding=True)),
                                     params)).mappings().all()
        if not rows and geo:
            sql = base.format(d=f", {dist} AS dist_m") + \
                f" AND {dist} <= :radius ORDER BY dist_m LIMIT :pool"
            rows = (await db.execute(text(sql).bindparams(bindparam("cats", expanding=True),
                                                          bindparam("extra", expanding=True)),
                                     {"cats": cats, "extra": extra, "pool": POOL, "lat": lat, "lng": lng,
                                      "radius": RADIUS_M})).mappings().all()
    return [dict(r) for r in rows]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


async def judge(q: str, interp: Dict[str, Any], lat: Optional[float],
                lng: Optional[float]) -> List[dict]:
    """Picks as hits (same shape as routers.search._resolve_picks). Never raises."""
    hoods = list(interp.get("neighborhoods") or [])
    key = _cache_key(q, hoods, lat, lng)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        _cache.move_to_end(key)
        return [dict(h) for h in hit[1]]
    try:
        rows = await _candidates(interp["categories"], hoods, lat, lng)
    except Exception as e:
        logger.info(f"[vibe] candidate query failed for {q!r}: {e}")
        return []
    # One row per name: FSQ holds duplicates (four "Hotel Chantelle" rows).
    # The carded, nearest one wins.
    rows = [r for r in rows if not is_rejected(r["fsq_id"])]
    rows.sort(key=lambda r: (card_text(r["fsq_id"]) is None,
                             r["dist_m"] if r.get("dist_m") is not None else 1e12))
    seen, cands = set(), []
    for r in rows:
        k = _norm(r["name"])
        if k and k not in seen:
            seen.add(k)
            cands.append(r)
        if len(cands) >= MAX_CANDIDATES:
            break
    if not cands:
        return []
    carded = sum(card_text(r["fsq_id"]) is not None for r in cands)
    if carded < MIN_CARDED:
        logger.info(f"[vibe] {q!r}: only {carded} carded candidates, using recalled picks")
        return []

    lines = []
    for i, r in enumerate(cands, 1):
        card = card_text(r["fsq_id"])
        lines.append(f"{i}. {r['name']} ({label(r)})" + (f": {card}" if card else ""))
    user = f"Search: {q}\n\nPlaces:\n" + "\n".join(lines)
    t0 = time.monotonic()
    raw = await openai_text(system=_JUDGE_SYSTEM, user=user, max_tokens=500,
                            timeout_s=JUDGE_TIMEOUT_S, cache_key=f"jink-vibe-judge-v{JUDGE_VERSION}")
    picks = _parse(raw, len(cands))
    logger.info(f"[vibe] {q!r}: {len(cands)} candidates "
                f"({sum(card_text(r['fsq_id']) is not None for r in cands)} carded), "
                f"{len(picks)} picks in {(time.monotonic() - t0) * 1000:.0f}ms")
    if raw is None:
        return []  # a failed call is not an answer; do not cache it

    out = []
    for n, why in picks:
        r = cands[n - 1]
        out.append({
            "type": "venue", "id": r["fsq_id"],
            "bin": str(r["bin"]).replace(".0", "") if r["bin"] else None,
            "bbl": str(r["bbl"]).replace(".0", "") if r["bbl"] else None,
            "name": " ".join((r["name"] or "").split()), "snippet": r["snippet"],
            "year": r["building_year"], "style": r["building_style"],
            "category": r["category"], "lat": r["lat"], "lng": r["lng"],
            "photo_url": r["photo_url"], "lore_status": None, "why": why,
            "dist_m": round(float(r["dist_m"]), 1) if r.get("dist_m") is not None else None,
            "pick": True,
        })
    _cache[key] = (time.monotonic(), [dict(h) for h in out])
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return out


def _parse(raw: Optional[str], n_cands: int) -> List[tuple]:
    if not raw:
        return []
    t = raw.strip().strip("`")
    t = t[t.find("{"):t.rfind("}") + 1]
    try:
        d = json.loads(t)
    except Exception:
        return []
    out, seen = [], set()
    for p in d.get("picks") or []:
        if not isinstance(p, dict):
            continue
        n = p.get("n")
        if isinstance(n, int) and 1 <= n <= n_cands and n not in seen:
            seen.add(n)
            why = p.get("why") if isinstance(p.get("why"), str) else ""
            out.append((n, " ".join(why.split())[:90]))
    return out[:MAX_PICKS]

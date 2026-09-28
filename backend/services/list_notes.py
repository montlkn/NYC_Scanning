"""What people's public lists say about a venue, for the vibe judge.

Design: docs/VIBE_SEARCH.md, "Curator lists". People put venues, buildings and
places (parks, cemeteries, landmarks) on lists in the app; a public list gives
each one 1-5 short descriptors and an optional comment. MAIN aggregates them
in `venue_list_notes()`, keyed by the venue's fsq id, "bin:<bin>" for a
building, or "place:<id>" for a place (see `key_for`),
and this module pulls that every few hours and keeps it in memory, next to the
hand-written editor notes. Changes reach search within REFRESH_S, not
instantly; that was the agreed trade for needing no new job infrastructure.

Weighting is in the text, not in code: descriptors are shown with the number
of lists that used them, most agreed first, so the judge sees "cutty (4
lists)" before "cozy (1 list)". Comments follow as quotes.

`save_count` covers private lists too, which is why MAIN returns it only once
5 people have saved the place. It is shown as popularity, never as vibe.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from models.config import get_settings

logger = logging.getLogger(__name__)

REFRESH_S = 3 * 3600
# Per public list that used a descriptor the query names, capped. A rank step
# is ~0.016 after RRF_SCALE, so one list moves a hit ~2 steps and the cap ~7:
# a tiebreak among real matches, never enough to lift an unrelated row.
W_LIST_DESCRIPTOR = 0.03
W_LIST_DESCRIPTOR_MAX = 0.12
MAX_DESCRIPTORS = 8
MAX_COMMENTS = 3
MAX_COMMENT_CHARS = 160

_NOTES: Dict[str, Dict[str, Any]] = {}
_generation = 0


def generation() -> int:
    """Bumped on every refresh that changed something, so judge caches keyed
    on it stop serving answers made without the new lists."""
    return _generation


def get(fsq_id: Optional[str]) -> Optional[Dict[str, Any]]:
    return _NOTES.get(fsq_id or "")


def key_for(hit: Dict[str, Any]) -> Optional[str]:
    """The `venue_list_notes` key for a unified search hit, or None when that
    kind of hit can't be on a list."""
    t = hit.get("type")
    if t == "venue":
        return hit.get("id")
    if t == "building" and hit.get("bin"):
        return "bin:" + str(hit["bin"]).replace(".0", "")
    return None


def descriptor_bonus(q_lex: str, key: Optional[str]) -> float:
    """Public lists called this place what the query asks for. A descriptor
    counts when every one of its words is in the query ("candlelit" in
    "candlelit wine bar"); its weight is how many lists used it."""
    descs = (get(key) or {}).get("descriptors") or {}
    if not descs or not q_lex:
        return 0.0
    q_words = set(q_lex.lower().split())
    lists = sum(int(n or 0) for d, n in descs.items()
                if d and set(d.lower().split()) <= q_words)
    return min(W_LIST_DESCRIPTOR_MAX, W_LIST_DESCRIPTOR * lists)


def list_count(fsq_id: Optional[str]) -> int:
    return int((get(fsq_id) or {}).get("list_count") or 0)


def text_for(fsq_id: Optional[str]) -> Optional[str]:
    """One line for the judge, or None when no public list describes it."""
    n = get(fsq_id)
    if not n or not n.get("list_count"):
        return None
    return format_note(n)


def save_count(fsq_id: Optional[str]) -> Optional[int]:
    return (get(fsq_id) or {}).get("save_count")


def format_note(n: Dict[str, Any]) -> str:
    k = int(n.get("list_count") or 0)
    parts: List[str] = []
    titles = []
    for l in n.get("lists") or []:
        t = (l.get("name") or "").strip()
        if l.get("mood"):
            t = f"{t} ({l['mood'].strip()})" if t else l["mood"].strip()
        if t:
            titles.append(f'"{t}"')
    parts.append(f"on {k} public list{'s' if k != 1 else ''}"
                 + (f": {', '.join(titles[:4])}" if titles else ""))
    descs = sorted((n.get("descriptors") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    if descs:
        parts.append("described as " + ", ".join(
            f"{d} ({c} list{'s' if c != 1 else ''})" for d, c in descs[:MAX_DESCRIPTORS]))
    comments = [" ".join(c.split())[:MAX_COMMENT_CHARS] for c in (n.get("comments") or []) if c]
    if comments:
        parts.append("comments: " + " / ".join(f'"{c}"' for c in comments[:MAX_COMMENTS]))
    return "; ".join(parts)


async def refresh() -> bool:
    """Replace the in-memory notes from MAIN. Keeps the old ones on failure,
    so a MAIN hiccup never empties search of list knowledge."""
    global _NOTES, _generation
    s = get_settings()
    url = s.supabase_url.rstrip("/") + "/rest/v1/rpc/venue_list_notes"
    headers = {"apikey": s.supabase_key, "Authorization": f"Bearer {s.supabase_key}",
               "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=headers, json={})
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        logger.warning(f"[lists] refresh failed, keeping {len(_NOTES)} notes: {e}")
        return False
    fresh = {row["venue_id"]: row for row in rows if row.get("venue_id")}
    if fresh != _NOTES:
        _NOTES = fresh
        _generation += 1
    logger.info(f"[lists] {sum(1 for v in fresh.values() if v.get('list_count'))} venues on "
                f"public lists, {sum(1 for v in fresh.values() if v.get('save_count'))} with save counts")
    return True


async def run_forever() -> None:
    while True:
        t0 = time.monotonic()
        await refresh()
        await asyncio.sleep(max(60.0, REFRESH_S - (time.monotonic() - t0)))

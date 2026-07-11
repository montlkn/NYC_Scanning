"""
Pure logic for GET /api/search/unified: intent classification, Reciprocal Rank
Fusion across corpora, and deterministic `why`/`header` string builders.

Kept DB-free and side-effect-free so it's unit-testable without SEARCH_DB_URL
(see backend/tests/test_unified_search.py). routers/search.py imports these
functions and wires them to the actual retrieval legs.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Intent router — cheap, deterministic, rules-first. No LLM call on this path;
# Grok is reserved for `prose` intent refinement (off the critical path, see
# routers/search.py::_maybe_refine_with_grok).
# ---------------------------------------------------------------------------

INTENTS = ("name", "address", "poi", "architect", "style", "lore", "event", "prose")

# NYC street-address shape: leading house number (digits, optional letter/
# hyphen for Queens-style "35-01"), then a street-ish token.
_ADDRESS_RE = re.compile(
    r"^\s*\d+[\-\d]*\s+[A-Za-z0-9][A-Za-z0-9.\s]*"
    r"(street|st|avenue|ave|boulevard|blvd|road|rd|place|pl|drive|dr|lane|ln|way|court|ct|square|sq|broadway|parkway|pkwy)\b",
    re.IGNORECASE,
)

_POI_NOUNS = frozenset({
    "bar", "bars", "cafe", "cafes", "coffee", "restaurant", "restaurants",
    "club", "clubs", "theater", "theatre", "theaters", "theatres", "diner",
    "diners", "speakeasy", "speakeasies", "bakery", "bakeries", "pizzeria",
    "pizzerias", "brewery", "breweries", "bookstore", "bookstores", "gallery",
    "galleries", "museum", "museums", "shop", "shops", "market", "markets",
    "pub", "pubs", "lounge", "lounges", "hotel", "hotels", "deli", "delis",
    "venue", "venues",
})

_ARCHITECT_MARKERS = frozenset({
    "architect", "architects", "designed", "designer", "firm", "atelier",
})

_STYLE_VOCAB = frozenset({
    "deco", "beaux-arts", "beaux", "arts", "gothic", "romanesque", "modernist",
    "modernism", "brutalist", "brutalism", "victorian", "federal", "georgian",
    "italianate", "neoclassical", "postmodern", "postmodernism", "moderne",
    "streamline", "cast-iron", "greek", "revival", "mid-century", "midcentury",
    "international", "queen", "renaissance", "colonial", "tudor", "art",
})

_LORE_VOCAB = frozenset({
    "demolished", "demolition", "lost", "former", "formerly", "ghost",
    "torn", "razed", "vanished", "gone", "unbuilt", "never", "abandoned",
    "ruins", "ruin", "history", "historic", "used", "once",
})

_EVENT_VOCAB = frozenset({
    "fire", "blackout", "riot", "riots", "strike", "collapse", "explosion",
    "disaster", "protest", "protests", "attack", "bombing", "crash",
})


def _tokens(q: str) -> List[str]:
    return [t for t in re.split(r"[^\w'-]+", q.lower()) if t]


def classify_intent(q: str) -> str:
    """Deterministic rules cascade. Order matters — more specific first."""
    q = (q or "").strip()
    if not q:
        return "prose"

    if _ADDRESS_RE.match(q):
        return "address"

    toks = _tokens(q)
    tokset = set(toks)

    if tokset & _EVENT_VOCAB:
        return "event"
    if tokset & _LORE_VOCAB:
        return "lore"
    if tokset & _ARCHITECT_MARKERS:
        return "architect"
    if tokset & _POI_NOUNS:
        return "poi"
    if tokset & _STYLE_VOCAB:
        return "style"

    # Short, capitalized, name-like query (e.g. "Chrysler Building", "The Dakota")
    # and NOT a full sentence: <= 5 words, no verb-ish trailing punctuation.
    words = q.split()
    if len(words) <= 5:
        cap_words = [w for w in words if w[:1].isupper()]
        if len(cap_words) >= max(1, len(words) - 1):  # almost every word capitalized
            return "name"

    if len(words) <= 6:
        return "name"

    return "prose"


# Per-intent corpus weights for RRF fusion. Keys: buildings / venues / layers.
# Values are multipliers applied AFTER RRF score is computed per corpus, so an
# intent can favor the corpus it's most relevant to without changing the RRF
# formula itself. `layers` doubles as the lore/event/plaque corpus.
_INTENT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "name":      {"buildings": 1.0, "venues": 0.6, "layers": 0.6},
    "address":   {"buildings": 1.0, "venues": 0.5, "layers": 0.4},
    "poi":       {"buildings": 0.4, "venues": 1.0, "layers": 0.3},
    "architect": {"buildings": 1.0, "venues": 0.3, "layers": 0.4},
    "style":     {"buildings": 1.0, "venues": 0.5, "layers": 0.4},
    "lore":      {"buildings": 0.5, "venues": 0.3, "layers": 1.0},
    "event":     {"buildings": 0.3, "venues": 0.2, "layers": 1.0},
    "prose":     {"buildings": 0.8, "venues": 0.8, "layers": 0.8},
}


def corpus_weights(intent: str) -> Dict[str, float]:
    return _INTENT_WEIGHTS.get(intent, _INTENT_WEIGHTS["prose"])


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

RRF_K = 60

# Small, named personalization/proximity/novelty nudge constants. These only
# break ties — never large enough to overturn a real relevance gap.
W_PERSONALIZATION = 0.05
W_PROXIMITY = 0.05
W_NOVELTY = 0.02
PROXIMITY_DECAY_M = 1500.0  # nudge decays to ~0 by this distance


@dataclass
class RankedHit:
    """A hit as produced by one retrieval leg, pre-fusion."""

    corpus: str  # "buildings" | "venues" | "layers"
    key: str     # unique id within its corpus (bin / fsq_id / layer id)
    rank: int    # 1-based rank within that corpus's result list (by its own score)
    payload: Dict[str, Any] = field(default_factory=dict)


def reciprocal_rank_fusion(
    legs: Dict[str, Sequence[RankedHit]],
    weights: Dict[str, float],
    k: int = RRF_K,
) -> List[tuple]:
    """RRF across corpora. Returns list of (global_key, fused_score, best_hit)
    sorted descending by fused_score. global_key = "corpus:key" so cross-corpus
    collisions (e.g. a building bin and a layer id sharing a string) can't merge.

    NO absolute cosine thresholds anywhere in this function — only ranks.
    """
    scores: Dict[str, float] = {}
    best_hit: Dict[str, RankedHit] = {}

    for corpus, hits in legs.items():
        w = weights.get(corpus, 1.0)
        for h in hits:
            gk = f"{corpus}:{h.key}"
            contrib = w * (1.0 / (k + h.rank))
            scores[gk] = scores.get(gk, 0.0) + contrib
            if gk not in best_hit:
                best_hit[gk] = h

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(gk, score, best_hit[gk]) for gk, score in ranked]


def profile_similarity(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> Optional[float]:
    """Dot product between a user's 9-archetype aesthetic vector and a
    building's `profile` vector, for the personalization nudge.

    Defensive: returns None (no nudge) rather than raising when either vector
    is missing, empty, or the lengths mismatch — a personalization score is
    optional context, never a hard requirement. Does NOT normalize/clamp the
    output; both inputs are expected to already be normalized score vectors
    (see AestheticProfile.normalized_scores in the iOS app), so the raw dot
    product is already a small bounded value suitable for apply_nudges' tie-
    breaking role.
    """
    if not a or not b:
        return None
    if len(a) != len(b):
        return None
    return sum(x * y for x, y in zip(a, b))


def apply_nudges(
    base_score: float,
    *,
    personalization_dot: Optional[float] = None,
    dist_m: Optional[float] = None,
    is_novel: Optional[bool] = None,
) -> float:
    """Add small named nudge terms to a fused score. Ties only."""
    score = base_score
    if personalization_dot is not None:
        score += W_PERSONALIZATION * personalization_dot
    if dist_m is not None and dist_m >= 0:
        proximity = max(0.0, 1.0 - (dist_m / PROXIMITY_DECAY_M))
        score += W_PROXIMITY * proximity
    if is_novel:
        score += W_NOVELTY
    return score


# ---------------------------------------------------------------------------
# `why` / `header` — deterministic, pure, from data only. No LLM.
# ---------------------------------------------------------------------------

def build_why(
    *,
    matched_field: Optional[str] = None,
    year: Optional[int] = None,
    style: Optional[str] = None,
    category: Optional[str] = None,
    fallback_snippet: Optional[str] = None,
) -> str:
    """Deterministic per-hit explanation. Always non-empty."""
    parts: List[str] = []
    if style:
        parts.append(str(style))
    if year:
        parts.append(str(year))
    if category and category not in parts:
        parts.append(str(category))

    head = ", ".join(parts) if parts else (fallback_snippet or "").strip()[:60]
    tail = f"matched: {matched_field}" if matched_field else None

    if head and tail:
        return f"{head} · {tail}"
    if head:
        return head
    if tail:
        return tail
    return "match"


def build_header(hits: List[Dict[str, Any]], intent: str) -> str:
    """Single deterministic summary line computed from the result set. No LLM.

    Examples: "14 Art Deco buildings, mostly Midtown, 1927–1937"
              "6 results for \"chrysler\""
    """
    n = len(hits)
    if n == 0:
        return "No results"

    styles = Counter(h.get("style") for h in hits if h.get("style"))
    years = [h.get("year") for h in hits if isinstance(h.get("year"), int)]
    types = Counter(h.get("type") for h in hits if h.get("type"))

    dominant_style = styles.most_common(1)[0][0] if styles else None
    dominant_type = types.most_common(1)[0][0] if types else None

    noun = "results"
    if dominant_type:
        noun = f"{dominant_type}s" if not dominant_type.endswith("s") else dominant_type

    lead = f"{n} {dominant_style + ' ' if dominant_style else ''}{noun}"

    era = ""
    if years:
        lo, hi = min(years), max(years)
        era = f", {lo}" if lo == hi else f", {lo}–{hi}"

    return f"{lead}{era}"


# ---------------------------------------------------------------------------
# Facets from a hit's raw field for filter-chip construction (used by router).
# ---------------------------------------------------------------------------

def build_facets(available: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """Given {kind: [values...]} derived from DB DISTINCT queries, produce
    facet chip descriptors. Purely mechanical — no hardcoded option lists."""
    facets: List[Dict[str, Any]] = []
    for kind, values in available.items():
        param = kind  # kind IS the query param name by convention
        for v in values:
            if v is None or v == "":
                continue
            facets.append({
                "kind": kind,
                "label": str(v).replace("_", " ").title(),
                "param": param,
                "value": v,
            })
    return facets

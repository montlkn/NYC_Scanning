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
    intent, _noun = classify_intent_detailed(q)
    return intent


def classify_intent_detailed(q: str) -> tuple:
    """Same cascade as classify_intent, but also returns the detected POI noun
    (singular form, e.g. "bar" for "bars") when intent == "poi", else None.
    Split out so routers/search.py can use the noun for category-family
    ranking without re-running the classifier or duplicating _POI_NOUNS."""
    q = (q or "").strip()
    if not q:
        return "prose", None

    if _ADDRESS_RE.match(q):
        return "address", None

    toks = _tokens(q)
    tokset = set(toks)

    if tokset & _EVENT_VOCAB:
        return "event", None
    if tokset & _LORE_VOCAB:
        return "lore", None
    if tokset & _ARCHITECT_MARKERS:
        return "architect", None
    poi_hit = tokset & _POI_NOUNS
    if poi_hit:
        # Prefer the noun in the order it appears in the query (deterministic,
        # not set-iteration order) so "art deco bar" -> "bar", not whichever
        # hash order the set produces.
        noun = next((t for t in toks if t in poi_hit), None)
        return "poi", _singularize_poi_noun(noun)
    if tokset & _STYLE_VOCAB:
        return "style", None

    # Short, capitalized, name-like query (e.g. "Chrysler Building", "The Dakota")
    # and NOT a full sentence: <= 5 words, no verb-ish trailing punctuation.
    words = q.split()
    if len(words) <= 5:
        cap_words = [w for w in words if w[:1].isupper()]
        if len(cap_words) >= max(1, len(words) - 1):  # almost every word capitalized
            return "name", None

    if len(words) <= 6:
        return "name", None

    return "prose", None


# Plural -> singular map for the handful of _POI_NOUNS with an 's' plural, so
# the exposed noun can key straight into _POI_CATEGORY_FAMILIES below without
# needing every plural spelled out there too.
_POI_NOUN_SINGULAR: Dict[str, str] = {
    "bars": "bar", "cafes": "cafe", "restaurants": "restaurant",
    "clubs": "club", "theaters": "theater", "theatres": "theater",
    "theatre": "theater", "diners": "diner", "speakeasies": "speakeasy",
    "bakeries": "bakery", "pizzerias": "pizzeria", "breweries": "brewery",
    "bookstores": "bookstore", "galleries": "gallery", "museums": "museum",
    "shops": "shop", "markets": "market", "pubs": "pub", "lounges": "lounge",
    "hotels": "hotel", "delis": "deli", "venues": "venue",
}


def _singularize_poi_noun(noun: Optional[str]) -> Optional[str]:
    if noun is None:
        return None
    return _POI_NOUN_SINGULAR.get(noun, noun)


# ---------------------------------------------------------------------------
# POI category-family alignment (poi intent only). Maps a detected POI noun
# to the family of FSQ `category` substrings that count as a match, for a
# mild rank boost/demotion in routers/search.py's venues leg. Kept minimal —
# only families with real ambiguity risk in the venues corpus need entries;
# a noun with no entry here just gets no boost/demotion (neutral).
# ---------------------------------------------------------------------------

POI_CATEGORY_FAMILIES: Dict[str, frozenset] = {
    "bar": frozenset({"bar", "pub", "lounge", "speakeasy", "cocktail", "gastropub", "tavern"}),
    "pub": frozenset({"bar", "pub", "lounge", "speakeasy", "cocktail", "gastropub", "tavern"}),
    "lounge": frozenset({"bar", "pub", "lounge", "speakeasy", "cocktail", "gastropub"}),
    "speakeasy": frozenset({"bar", "pub", "lounge", "speakeasy", "cocktail"}),
    "cafe": frozenset({"cafe", "coffee", "espresso"}),
    "coffee": frozenset({"cafe", "coffee", "espresso"}),
    "restaurant": frozenset({"restaurant", "diner", "steakhouse", "bistro", "grill", "eatery"}),
    "diner": frozenset({"diner", "restaurant"}),
    "club": frozenset({"club", "nightclub", "lounge"}),
    "theater": frozenset({"theater", "theatre", "cinema", "playhouse"}),
    "bakery": frozenset({"bakery", "patisserie"}),
    "pizzeria": frozenset({"pizza"}),
    "brewery": frozenset({"brewery", "beer"}),
    "bookstore": frozenset({"bookstore", "book"}),
    "gallery": frozenset({"art gallery", "gallery"}),
    "museum": frozenset({"museum"}),
    "market": frozenset({"market", "grocery"}),
    "hotel": frozenset({"hotel", "inn"}),
    "deli": frozenset({"deli", "sandwich"}),
}

# Category-word families that must NOT count toward a POI family, even though
# they share a substring with a family keyword (e.g. "Publisher"/"Public Art"
# contain "pub"). Checked as whole words via POI_CATEGORY_MATCH_RE, so this
# set exists only for the demotion side: a category made ENTIRELY of these
# non-family words (e.g. "Art Gallery" vs a "bar" query) still demotes.
_POI_DEMOTE_UNRELATED = frozenset({
    "art gallery", "antique store", "gallery", "sushi restaurant", "furniture and home store",
})

W_POI_CATEGORY_MATCH = 0.08   # boost when venue category matches the detected POI family
W_POI_CATEGORY_MISMATCH = -0.05  # demotion when category is a different, unrelated POI family


def _category_word_hit(category: Optional[str], keywords: frozenset) -> bool:
    """Whole-word (not substring) match of any keyword against category text.
    Guards against "Publisher"/"Public Art" false-matching "pub", etc."""
    if not category:
        return False
    cat_l = category.lower()
    cat_words = set(re.split(r"[^\w]+", cat_l))
    for kw in keywords:
        if " " in kw:
            if kw in cat_l:
                return True
        elif kw in cat_words:
            return True
    return False


def poi_category_adjustment(category: Optional[str], poi_noun: Optional[str]) -> float:
    """Rank nudge for a venue hit under poi intent. Returns 0.0 when there's
    no detected noun, no category, or no family entry for the noun (neutral —
    never a hard filter, just an additive adjustment on the fused score)."""
    if not poi_noun or not category:
        return 0.0
    family = POI_CATEGORY_FAMILIES.get(poi_noun)
    if not family:
        return 0.0
    if _category_word_hit(category, family):
        return W_POI_CATEGORY_MATCH
    if category.strip().lower() in _POI_DEMOTE_UNRELATED:
        return W_POI_CATEGORY_MISMATCH
    return 0.0


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


# Intents where "near me" is core to the request — radius_m stays a hard
# filter for these (poi/name/address/event). For the rest (style/architect/
# lore/prose) radius is a SOFT signal only: relevance dominates, but a
# proximity bonus still favors closer results among otherwise-similar hits.
# Verbatim requirement: "surface results close to you but city-wide shouldn't
# be out of the question if it's closer to the search term."
HARD_RADIUS_INTENTS = frozenset({"poi", "name", "address", "event"})

# Soft-radius proximity bonus (separate from apply_nudges' W_PROXIMITY, which
# is a tiny universal tie-break) — exponential decay so nearby results get a
# real lift without a hard cutoff. Small relative to a real relevance gap.
PROXIMITY_DECAY_WEIGHT = 0.06
PROXIMITY_DECAY_SCALE_M = 3000.0

# Rank boost for landmark-flagged buildings (building_search_index.is_landmark,
# already populated by embed_buildings.py from the curated `landmark` column —
# no schema change needed) on intents where "fame" should break ties in favor
# of icon-tier buildings, e.g. "art deco" surfacing the Chrysler Building over
# an obscure art-deco-tagged row house. Buildings only (venues/layers have no
# landmark concept in this schema).
LANDMARK_BOOST_INTENTS = frozenset({"style", "prose", "name", "architect"})
W_LANDMARK_BOOST = 0.07


def proximity_decay_bonus(dist_m: Optional[float]) -> float:
    """Exponential proximity bonus for SOFT-radius intents. 0.0 when dist_m is
    None/negative (no location signal, or hit has no lat/lng)."""
    if dist_m is None or dist_m < 0:
        return 0.0
    import math
    return PROXIMITY_DECAY_WEIGHT * math.exp(-dist_m / PROXIMITY_DECAY_SCALE_M)


def landmark_boost(intent: str, is_landmark: Optional[bool]) -> float:
    """Small rank boost for landmark buildings on intents where fame should
    matter. 0.0 for non-landmark hits, non-boosted intents, or hits with no
    landmark flag (None — e.g. venues/layers, or pre-migration rows)."""
    if intent not in LANDMARK_BOOST_INTENTS:
        return 0.0
    return W_LANDMARK_BOOST if is_landmark else 0.0


# ---------------------------------------------------------------------------
# Dedup near-identical hits — same normalized name AND within DEDUPE_DIST_M
# of each other (e.g. "Court Name: Roosevelt" repeated across adjacent BINs
# of one housing development). Keeps the highest-scored of each cluster.
# ---------------------------------------------------------------------------

DEDUPE_DIST_M = 150.0


def _normalize_name_for_dedupe(name: Optional[str]) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", " ", n)
    return n


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def dedupe_near_identical(
    hits: List[Dict[str, Any]],
    *,
    dist_m: float = DEDUPE_DIST_M,
    score_key: str = "score",
) -> List[Dict[str, Any]]:
    """Collapse hits sharing the same normalized name AND within `dist_m` of
    each other, keeping the highest-scored per cluster. Order-preserving on
    the SURVIVORS (first occurrence position of the kept hit), so callers that
    already sorted by score keep that order. Hits missing lat/lng or name are
    never merged with anything (can't establish proximity/identity), so they
    always survive untouched — this function only removes hits it's SURE are
    duplicates of a kept hit.
    """
    n = len(hits)
    if n <= 1:
        return list(hits)

    kept_idx: List[int] = []
    absorbed: set = set()

    for i in range(n):
        if i in absorbed:
            continue
        name_i = _normalize_name_for_dedupe(hits[i].get("name"))
        lat_i, lng_i = hits[i].get("lat"), hits[i].get("lng")
        if not name_i or lat_i is None or lng_i is None:
            kept_idx.append(i)
            continue

        cluster = [i]
        for j in range(i + 1, n):
            if j in absorbed:
                continue
            name_j = _normalize_name_for_dedupe(hits[j].get("name"))
            lat_j, lng_j = hits[j].get("lat"), hits[j].get("lng")
            if not name_j or lat_j is None or lng_j is None:
                continue
            if name_j != name_i:
                continue
            if _haversine_m(lat_i, lng_i, lat_j, lng_j) <= dist_m:
                cluster.append(j)

        if len(cluster) == 1:
            kept_idx.append(i)
            continue

        best = max(cluster, key=lambda k: hits[k].get(score_key) or 0.0)
        kept_idx.append(best)
        for k in cluster:
            if k != best:
                absorbed.add(k)

    kept_idx_sorted = sorted(set(kept_idx))
    return [hits[i] for i in kept_idx_sorted]


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
# matched_field inference — the buildings/venues/layers legs previously
# hardcoded "name/architect"/"name"/"title" any time the trigram lex_score
# beat 0.5, even when the actual overlapping token was a STYLE word ("art
# deco bar" -> style match on a random art-deco-tagged row house showed
# "matched: name/architect", which is simply wrong). Infer from which parsed
# field the query tokens actually overlap, comparing q_lex tokens against
# each candidate field's tokens — cheap, deterministic, no new DB round trip.
# ---------------------------------------------------------------------------

def infer_matched_field(
    q_lex: str,
    *,
    name: Optional[str] = None,
    style: Optional[str] = None,
    architect: Optional[str] = None,
    category: Optional[str] = None,
    default: str = "semantic",
) -> str:
    """Pick the best-labeled matched_field by token overlap between q_lex and
    each candidate field, preferring the more specific label on a tie
    (name > architect > style > category) since a name hit is the strongest
    signal a human reads as "found it". Falls back to `default` (typically
    "semantic") when no field shares a token with the query."""
    q_toks = {t for t in _tokens(q_lex) if len(t) >= 3}
    if not q_toks:
        return default

    candidates = [
        ("name", name),
        ("architect", architect),
        ("style", style),
        ("category", category),
    ]
    best_label = default
    best_overlap = 0
    for label, field in candidates:
        if not field:
            continue
        field_toks = {t for t in _tokens(field) if len(t) >= 3}
        overlap = len(q_toks & field_toks)
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = label
    return best_label


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

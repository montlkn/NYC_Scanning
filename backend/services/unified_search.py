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
    # (?<![A-Za-z]) is load-bearing: without a LEFT word boundary the short
    # alternatives match mid-word, so "5 best gothic churches" parsed as an
    # address off the "st" inside "be|st" and never reached the style branch.
    r"(?<![A-Za-z])(street|st|avenue|ave|boulevard|blvd|road|rd|place|pl|drive|dr|lane|ln|way|court|ct|square|sq|broadway|parkway|pkwy)\b",
    re.IGNORECASE,
)

# People drop the street type constantly ("1 south first", "225 lafayette").
# The strict regex above requires one, so those fell through the whole cascade
# and landed on `name` intent — which weights the lore/layers corpus 0.6
# instead of 0.4 and skips the address handling entirely. That is why an
# address query returned unrelated history articles.
#
# Shape: leading house number + 1-3 following word tokens, no vocab hits. Kept
# deliberately tight (a trailing token cap, and only when nothing else in the
# cascade claimed the query) so prose like "5 best gothic churches" is not
# swallowed as an address.
_ADDRESS_LOOSE_RE = re.compile(r"^\s*\d+[\-\d]*\s+[A-Za-z][A-Za-z.'-]*(\s+[A-Za-z][A-Za-z.'-]*){0,2}\s*$")

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

    # Suffix-less address, checked AFTER the vocab cascade so a query that also
    # carries style/lore/POI meaning keeps its richer intent.
    if _ADDRESS_LOOSE_RE.match(q):
        return "address", None

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

W_POI_CATEGORY_MATCH = 0.15   # boost when venue category matches the detected POI family
W_POI_CATEGORY_MISMATCH = -0.12  # demotion when category belongs to a DIFFERENT known family
W_POI_CATEGORY_UNKNOWN = -0.06   # mild demotion when category matches no family at all ("Structure")


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
    never a hard filter, just an additive adjustment on the fused score).

    Three-way: match the noun's own family → boost; word-match a DIFFERENT
    known family (sushi restaurant / gallery vs a "bar" query) → real demotion;
    match nothing we know ("Structure", "Monument") → mild demotion, since FSQ
    category text is noisy and absence of a family isn't proof of irrelevance."""
    if not poi_noun or not category:
        return 0.0
    family = POI_CATEGORY_FAMILIES.get(poi_noun)
    if not family:
        return 0.0
    if _category_word_hit(category, family):
        return W_POI_CATEGORY_MATCH
    if category.strip().lower() in _POI_DEMOTE_UNRELATED:
        return W_POI_CATEGORY_MISMATCH
    for other_noun, other_family in POI_CATEGORY_FAMILIES.items():
        if other_noun == poi_noun or other_family == family:
            continue
        if _category_word_hit(category, other_family):
            return W_POI_CATEGORY_MISMATCH
    return W_POI_CATEGORY_UNKNOWN


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

# RRF produces scores in [0, 1/(k+1)] = [0, 0.0164], with adjacent ranks
# differing by only ~0.00026. The nudge constants below live on a 0–1 scale.
# Added together untreated, a 0.05 proximity nudge was 3x the ENTIRE relevance
# range and a 0.15 category nudge was 9x it — so "tie-breakers" silently became
# the primary sort key, and distance could move a result from last to first
# regardless of relevance. (The old test asserted each nudge was small in
# ABSOLUTE terms and never compared it to an RRF rank gap, so it passed.)
#
# Scaling the fused score onto the nudge scale restores the intended
# relationship: one rank step is now ~0.016, so a 0.01 proximity nudge
# separates near-ties without overturning real relevance gaps.
RRF_SCALE = 60.0

# Small, named personalization/proximity/novelty nudge constants. Sized in
# RANK-STEPS post-RRF_SCALE: one rank step ~= 0.016. Keep every value here
# below ~0.05 (≈3 ranks) unless it is deliberately dominant and documented.
W_PERSONALIZATION = 0.02
W_PROXIMITY = 0.01  # max ~0.6 of a rank step — a true tiebreak
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
# Sized post-RRF_SCALE: max ~1.2 rank steps, so a much closer result wins a
# near-tie but a genuinely better match 4km away still outranks a weak one
# next door. This is the "relevance first, distance as tiebreak" policy.
PROXIMITY_DECAY_WEIGHT = 0.02
PROXIMITY_DECAY_SCALE_M = 3000.0

# Fame boost (building_search_index.fame — final_score from the curated
# buildings DB normalized 0–1 against the corpus max; see backfill_fame.py).
# Continuous and data-derived: no icon lists, no thresholds. is_landmark was
# tried first and is useless as a fame proxy — it's the LPC designation flag
# and 99.4% of the index is designated. Applied on intents where fame should
# break ties ("art deco" → Chrysler over an obscure deco row house).
# Buildings only (venues/layers carry no fame signal in this schema).
FAME_BOOST_INTENTS = frozenset({"style", "prose", "name", "architect"})
W_FAME_BOOST = 0.12  # × fame (0–1): Chrysler ≈ +0.095, median row ≈ +0.01
# Leg-level fame ordering weight (raw fused-score scale, ~0.5–0.9) — keeps
# high-fame rows from being cut at the leg LIMIT before the post-RRF boost
# can act. Applied by routers/search.py::_leg_buildings for FAME_BOOST_INTENTS.
W_LEG_FAME = 0.15


def proximity_decay_bonus(dist_m: Optional[float]) -> float:
    """Exponential proximity bonus for SOFT-radius intents. 0.0 when dist_m is
    None/negative (no location signal, or hit has no lat/lng)."""
    if dist_m is None or dist_m < 0:
        return 0.0
    import math
    return PROXIMITY_DECAY_WEIGHT * math.exp(-dist_m / PROXIMITY_DECAY_SCALE_M)


def fame_boost(intent: str, fame: Optional[float]) -> float:
    """Continuous fame boost on intents where fame should matter. 0.0 for
    non-boosted intents or hits with no fame score (None — venues/layers, or
    rows the backfill hasn't covered)."""
    if intent not in FAME_BOOST_INTENTS or fame is None:
        return 0.0
    return W_FAME_BOOST * max(0.0, min(1.0, fame))


# ---------------------------------------------------------------------------
# Exact-name dominance (name intent only). RRF fused scores top out around
# 0.016 and every other nudge is <= 0.07, so these are deliberately DOMINANT:
# when someone types a building's name, finding that building is the whole
# job — no semantic neighbor or proximity nudge should outrank it.
# ---------------------------------------------------------------------------

W_EXACT_NAME = 0.30    # normalized query == normalized name
W_NAME_SUBSET = 0.20   # every query content-token appears in the name


def exact_name_bonus(q_lex: str, name: Optional[str]) -> float:
    """Dominant bonus for name-intent queries whose tokens are literally the
    hit's name (or a subset of it — "chrysler" ⊆ "Chrysler Building")."""
    if not name:
        return 0.0
    q_norm = _normalize_name_for_dedupe(q_lex)
    n_norm = _normalize_name_for_dedupe(name)
    if not q_norm or not n_norm:
        return 0.0
    if q_norm == n_norm:
        return W_EXACT_NAME
    q_toks = {t for t in _tokens(q_norm) if len(t) >= 3}
    n_toks = {t for t in _tokens(n_norm) if len(t) >= 3}
    if q_toks and q_toks <= n_toks:
        return W_NAME_SUBSET
    return 0.0


# ---------------------------------------------------------------------------
# House-number dominance (name/address intent). A query like "469 broome" has
# no street suffix, so _ADDRESS_RE misses it and it classifies as `name` — where
# fame_boost is live and lets 570 Broome (a famous tower) outrank the exact
# 469-475 Broome. The fuzzy name/vector score never weighs the house NUMBER, so
# the one signal that actually pins an address is ignored. This bonus restores
# it: when the query LEADS with a house number and a building's address range
# contains it, add a dominant bonus (same tier as exact-name) so the correct
# address wins over any fame/vector nudge. Buildings whose display string is the
# address ("469-475 Broome Street") — i.e. the ~all-of-NYC row-house case — are
# matched here; a genuinely named building without the number simply gets 0.
# ---------------------------------------------------------------------------

W_HOUSE_NUMBER = 0.30   # == W_EXACT_NAME: dominant, beats fame (Chrysler ≈ +0.095)
# Number matches but the STREET does not — "1 south first" vs "1 South Oxford
# Street". Deliberately near-zero: these are the wrong building on the right
# house number, which is exactly the noise the old single-tier bonus promoted.
W_NUMBER_ONLY = 0.02


def query_house_number(q: str) -> Optional[int]:
    """Leading house number of an address-shaped query. "469 broome" -> 469;
    "broome street" -> None. Leading-only so style/year queries ("1920s deco")
    that merely contain digits don't trigger address scoring."""
    m = re.match(r"\s*(\d+)", q or "")
    return int(m.group(1)) if m else None


def _address_number_range(s: Optional[str]) -> Optional[tuple]:
    """Leading house-number range of a display string. "469-475 Broome St" ->
    (469, 475); "570 Broome Street" -> (570, 570); "Gunther Building" -> None."""
    if not s:
        return None
    m = re.match(r"\s*(\d+)(?:\s*-\s*(\d+))?", s)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return (min(lo, hi), max(lo, hi))


# Ordinal <-> numeral folding. NYC addresses are full of ordinal streets, and
# the corpus stores the NUMERAL form ("SOUTH 1ST STREET") while people type the
# WORD form ("1 south first"). With no fold, "first" could never match "1st" —
# so the literal target never entered the candidate pool at all and no amount
# of reranking could recover it. Covers 1st-31st, which spans every numbered
# street and avenue in the five boroughs.
_ORDINAL_WORDS = [
    "zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
    "eighteenth", "nineteenth", "twentieth", "twenty-first", "twenty-second",
    "twenty-third", "twenty-fourth", "twenty-fifth", "twenty-sixth",
    "twenty-seventh", "twenty-eighth", "twenty-ninth", "thirtieth",
    "thirty-first",
]


def _ordinal_numeral(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th'."""
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


_ORDINAL_WORD_TO_NUMERAL: Dict[str, str] = {
    w: _ordinal_numeral(i)
    for i, w in enumerate(_ORDINAL_WORDS)
    if i > 0
}
# Both directions, plus the bare-cardinal spelling ("1 street" -> "1st street").
_ORDINAL_NUMERAL_TO_WORD: Dict[str, str] = {
    v: k for k, v in _ORDINAL_WORD_TO_NUMERAL.items()
}


def fold_ordinals(text: Optional[str]) -> str:
    """Rewrite ordinal words to their numeral form ('first' -> '1st') so query
    and corpus meet in one spelling. Idempotent on already-numeral text."""
    if not text:
        return ""
    out = []
    for t in _tokens(text):
        out.append(_ORDINAL_WORD_TO_NUMERAL.get(t, t))
    return " ".join(out)


def ordinal_variants(token: str) -> set:
    """Every spelling of one token: {'first', '1st'} or {'1st', 'first'}."""
    v = {token}
    if token in _ORDINAL_WORD_TO_NUMERAL:
        v.add(_ORDINAL_WORD_TO_NUMERAL[token])
    if token in _ORDINAL_NUMERAL_TO_WORD:
        v.add(_ORDINAL_NUMERAL_TO_WORD[token])
    return v


# Street-type words carry no discriminating power — every address has one, so
# matching on "street" must not count as matching the street NAME.
_STREET_TYPE_WORDS = frozenset({
    "street", "st", "avenue", "ave", "av", "boulevard", "blvd", "road", "rd",
    "drive", "dr", "lane", "ln", "place", "pl", "court", "ct", "square", "sq",
    "parkway", "pkwy", "terrace", "ter", "way", "walk", "plaza", "circle",
})


def _street_name_tokens(s: Optional[str]) -> set:
    """Discriminating (non-numeric, non-street-type) tokens of an address,
    ordinal-folded. '1 South Oxford Street' -> {'south', 'oxford'}."""
    if not s:
        return set()
    toks = set()
    for t in _tokens(fold_ordinals(s)):
        if t.isdigit() or t in _STREET_TYPE_WORDS:
            continue
        toks.add(t)
    return toks


def house_number_bonus(q_lex: str, name: Optional[str], snippet: Optional[str]) -> float:
    """Bonus when an address query's house number falls inside a building's
    leading address range.

    Split by whether the STREET also matches. Previously this checked only the
    leading number, so "1 south first" handed the full dominant bonus to
    1 South Elliott Place, 1 South Oxford Street AND 1 South Portland Avenue
    alike — a three-way tie that proximity then broke arbitrarily. The street
    name is the entire discriminator in that query and it was unused.
    """
    qn = query_house_number(q_lex)
    if qn is None:
        return 0.0
    q_street = _street_name_tokens(q_lex)
    for s in (name, snippet):
        rng = _address_number_range(s)
        if rng and rng[0] <= qn <= rng[1]:
            if not q_street:
                # Bare number query ("469") — nothing to disambiguate against.
                return W_HOUSE_NUMBER
            cand_street = _street_name_tokens(s)
            # SUBSET, not intersection. "1 south first" -> {south, 1st}; all of
            # 1 South Oxford / Elliott / Portland share "south", so any-overlap
            # handed every one of them the full bonus — the exact three-way tie
            # that made this query useless. Requiring every query token to be
            # present means only 1 South 1st Street qualifies.
            if q_street <= cand_street:
                return W_HOUSE_NUMBER
            return W_NUMBER_ONLY
    return 0.0


# ---------------------------------------------------------------------------
# Venue style/era affinity (poi intent). The moat query "art deco bar" wants
# bars whose HOST BUILDING is deco — venues carry building_style and
# building_year, but until now neither was scored. Era windows exist only for
# styles with a well-defined period; unknown style tokens get no era fallback.
# ---------------------------------------------------------------------------

W_VENUE_STYLE = 0.12        # building_style token overlap with query style tokens
W_VENUE_ERA = 0.06          # no style text, but building_year inside the style's era

STYLE_ERA_WINDOWS: Dict[str, tuple] = {
    "deco": (1920, 1941),
    "moderne": (1925, 1945),
    "streamline": (1930, 1945),
    "victorian": (1860, 1901),
    "italianate": (1845, 1885),
    "beaux-arts": (1885, 1925),
    "beaux": (1885, 1925),
    "gothic": (1830, 1930),
    "romanesque": (1870, 1900),
    "federal": (1785, 1830),
    "georgian": (1700, 1780),
    "greek": (1820, 1860),
    "brutalist": (1955, 1980),
    "brutalism": (1955, 1980),
    "midcentury": (1945, 1970),
    "mid-century": (1945, 1970),
    "international": (1930, 1975),
    "postmodern": (1975, 1995),
    "cast-iron": (1850, 1885),
}


def query_style_tokens(q: str, poi_noun: Optional[str] = None) -> frozenset:
    """Style-vocab tokens in the query, minus the POI noun itself.
    "art deco bar" (noun "bar") → {"art", "deco"}."""
    toks = set(_tokens(q))
    toks.discard(poi_noun or "")
    return frozenset(toks & _STYLE_VOCAB)


# ---------------------------------------------------------------------------
# Hedged style values. The corpus stores uncertain attributions verbatim, e.g.
# "simplified colonial revival or art deco". Trigram `word_similarity` scores
# the query against the BEST-MATCHING SUBSTRING of the indexed text, so a query
# for "art deco" scored ~0.66 against that string — well over the 0.45 floor —
# and a block of Sunnyside colonial-revival houses ranked as prime Art Deco.
#
# Splitting on the hedge separators lets a match against the PRIMARY (first)
# attribution count fully while a match that only lands on an alternative is
# discounted. This is a ranking-time repair; materializing the split at ingest
# would be cheaper per query (see embed_buildings.py) but this needs no
# re-index and no migration.
# ---------------------------------------------------------------------------

_STYLE_ALT_SPLIT_RE = re.compile(r"\s+or\s+|\s*/\s*|\s*;\s*|\s*,\s*", re.IGNORECASE)

# Sized post-RRF_SCALE: ~6 rank steps. Enough to sink a hedged secondary match
# beneath any confident primary match without erasing it entirely — the
# building might genuinely be deco, we just have far weaker evidence.
W_HEDGED_STYLE = 0.10


def split_style_alternatives(style: Optional[str]) -> tuple:
    """('simplified colonial revival or art deco') ->
    ('simplified colonial revival', ['art deco']).

    Returns ("", []) for empty input. A style with no separator is all primary.
    """
    if not style:
        return "", []
    parts = [p.strip() for p in _STYLE_ALT_SPLIT_RE.split(style) if p and p.strip()]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def hedged_style_penalty(style_toks: frozenset, style: Optional[str]) -> float:
    """Demote a hit whose only style evidence sits in a hedged ALTERNATIVE.

    Returns 0.0 when the query has no style tokens, when the style value is not
    hedged, or when the primary attribution satisfies the query — i.e. it only
    fires for the specific "matched the 'or ...' tail" case.
    """
    if not style_toks or not style:
        return 0.0
    primary, alts = split_style_alternatives(style)
    if not alts:
        return 0.0
    primary_toks = set(_tokens(primary))
    if style_toks & primary_toks:
        return 0.0  # the confident attribution already answers the query
    alt_toks: set = set()
    for a in alts:
        alt_toks.update(_tokens(a))
    if style_toks & alt_toks:
        return -W_HEDGED_STYLE
    return 0.0


def venue_style_affinity(
    style_toks: frozenset,
    building_style: Optional[str],
    building_year: Optional[int],
) -> float:
    """Boost a venue whose host building matches the queried style — by style
    text if present, else by build year falling inside the style's era."""
    if not style_toks:
        return 0.0
    if building_style:
        b_toks = set(_tokens(building_style))
        if style_toks & b_toks:
            return W_VENUE_STYLE
    if building_year:
        for t in style_toks:
            window = STYLE_ERA_WINDOWS.get(t)
            if window and window[0] <= building_year <= window[1]:
                return W_VENUE_ERA
    return 0.0


# A venue whose NAME contains the query's style tokens ("High Style Deco",
# an antique store, for "art deco bar") wins the lexical pool on a decoy: the
# style words in its name aren't evidence it IS what the noun asked for.
# Penalized unless the venue's category actually matches the noun's family.
W_STYLE_NAME_DECOY = -0.10


def style_name_decoy_penalty(
    q_lex: str,
    name: Optional[str],
    category: Optional[str],
    poi_noun: Optional[str],
) -> float:
    if not name or not poi_noun:
        return 0.0
    family = POI_CATEGORY_FAMILIES.get(poi_noun)
    if family and _category_word_hit(category, family):
        return 0.0  # a real bar named "Deco Bar" is a great hit, not a decoy
    q_toks = {t for t in _tokens(q_lex) if len(t) >= 3}
    n_toks = set(_tokens(name))
    overlap = q_toks & n_toks
    if overlap and overlap <= _STYLE_VOCAB:
        return W_STYLE_NAME_DECOY
    return 0.0


# ---------------------------------------------------------------------------
# Multi-token conjunction (lore/event intents). "demolished theaters" must
# reward hits covering BOTH concepts and demote single-concept matches
# (a demolished skybridge, an extant theater).
# ---------------------------------------------------------------------------

# Sized post-RRF_SCALE (one rank step ~= 0.016). Coverage is the closest thing
# the ranker has to conjunctive AND semantics, so it is deliberately strong:
# a 1-of-2 match drops ~15 ranks relative to a 2-of-2 match.
W_FULL_COVERAGE = 0.10
W_LOW_COVERAGE = 0.25  # subtracted when coverage <= 0.5


def token_coverage(q_lex: str, *texts: Optional[str]) -> float:
    """Fraction of the query's distinct content tokens (len >= 3) found across
    the given texts, with naive plural folding both ways ("theaters" matches
    "theater" and vice versa). 1.0 when the query has no content tokens (no
    evidence either way — callers treat that as neutral)."""
    q_toks = {t for t in _tokens(q_lex) if len(t) >= 3}
    if not q_toks:
        return 1.0
    hit_toks = set()
    for text in texts:
        if text:
            hit_toks.update(_tokens(text))
    folded = set()
    for t in hit_toks:
        # -re/-er spelling fold (theatre/theater, centre/center) — both appear
        # in the corpus — then plural fold both ways.
        variants = {t}
        if t.endswith("re"):
            variants.add(t[:-2] + "er")
        elif t.endswith("er"):
            variants.add(t[:-2] + "re")
        for v in variants:
            folded.add(v)
            folded.add(v + "s")
            if v.endswith("s"):
                folded.add(v[:-1])
    covered = sum(1 for t in q_toks if t in folded or t.rstrip("s") in folded)
    return covered / len(q_toks)


def coverage_adjustment(
    intent: str,
    q_lex: str,
    name: Optional[str],
    snippet: Optional[str],
    *extra: Optional[str],
) -> float:
    """Coverage nudge for multi-token queries.

    Extended beyond lore/event to poi/style/prose. "art deco bars near me"
    classifies as `poi` (the intent cascade returns on the first vocab hit, so
    the "art deco" half is dropped at classification time) — which meant the
    one function implementing conjunctive coverage explicitly excluded the
    exact intent that needed it. Result: a deco building that is not a bar and
    a bar that is not deco each scored as a full match. Coverage is what makes
    the two halves of the query multiply instead of alternate.
    """
    if intent not in {"lore", "event", "poi", "style", "prose"}:
        return 0.0
    q_toks = {t for t in _tokens(q_lex) if len(t) >= 3}
    if len(q_toks) < 2:
        return 0.0
    # `extra` carries the STRUCTURED fields (category, style) so a genuine
    # hit gets credit for a concept it satisfies by data rather than by
    # prose: a real bar has category="Bar" but rarely "bar" in its name,
    # and a deco building carries the style in a column. Without these a
    # correct match would be penalised exactly as hard as a wrong one.
    cov = token_coverage(q_lex, name, snippet, *extra)
    if cov >= 1.0:
        return W_FULL_COVERAGE
    # Graded, not a cliff at 0.5. The old step function scored a 3-token query
    # covering 2 concepts ("art deco bars" matching a gallery mislabelled Bar:
    # 'art' + 'bars', no 'deco') at exactly 0.667 — above the cliff, so zero
    # penalty, so it ranked as a clean match. Scaling by the MISSING fraction
    # penalises every incomplete match in proportion to what it's missing,
    # while still reaching the full -W_LOW_COVERAGE at the canonical
    # half-covered case that this function was originally written for.
    return -min(W_LOW_COVERAGE, W_LOW_COVERAGE * (1.0 - cov) * 2.0)


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


# ---------------------------------------------------------------------------
# Relevance floor. Every leg returns its top `leg_limit` unconditionally, no
# matter how bad the matches are, and there was no threshold anywhere in the
# pipeline — so a query with three good answers still returned forty rows, the
# other thirty-seven being whatever the corpus had. Capping the list lower
# treats the symptom; the floor treats the cause.
#
# Relative, not absolute: post-nudge scores are not calibrated across intents,
# so the only defensible reference point is the best hit for THIS query.
# ---------------------------------------------------------------------------

RELEVANCE_FLOOR_RATIO = 0.40
RELEVANCE_FLOOR_MIN_KEEP = 3   # never return an empty list over a weak top hit


def apply_relevance_floor(
    hits: List[Dict[str, Any]],
    *,
    ratio: float = RELEVANCE_FLOOR_RATIO,
    min_keep: int = RELEVANCE_FLOOR_MIN_KEEP,
    score_key: str = "score",
) -> List[Dict[str, Any]]:
    """Drop hits scoring below `ratio` of the top hit's score.

    Expects `hits` already sorted descending. Always keeps at least `min_keep`
    so a genuinely thin query still shows its best guesses (and still trips the
    client's thin-results path) rather than rendering a bare empty state.

    Negative top scores mean everything got penalised — no meaningful ratio
    exists there, so the floor is skipped rather than applied backwards.
    """
    if not hits:
        return hits
    top = hits[0].get(score_key) or 0.0
    if top <= 0:
        return hits
    threshold = top * ratio
    kept = [h for h in hits if (h.get(score_key) or 0.0) >= threshold]
    if len(kept) < min_keep:
        return hits[:min_keep]
    return kept


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

    # A style only leads the header when it genuinely characterizes the set
    # (majority of hits) — otherwise "demolished theaters" got summarized as
    # "medieval revival lores" because ONE hit happened to carry that style.
    dominant_style = None
    if styles:
        top_style, top_count = styles.most_common(1)[0]
        if top_count * 2 > n:
            dominant_style = top_style
    dominant_type = types.most_common(1)[0][0] if types else None

    _TYPE_NOUNS = {"lore": "lore entries"}
    noun = "results"
    if dominant_type:
        noun = _TYPE_NOUNS.get(dominant_type) or (
            f"{dominant_type}s" if not dominant_type.endswith("s") else dominant_type
        )

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

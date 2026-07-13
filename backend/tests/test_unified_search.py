"""
Unit tests for services/unified_search.py — pure logic only (no DB). Tests
requiring SEARCH_DB_URL (actual retrieval legs / router integration) are
skipped unless that env var is set, matching this repo's silent-fallback
philosophy: nothing here should require live infra to pass in CI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.unified_search import (  # noqa: E402
    RankedHit,
    apply_nudges,
    build_facets,
    build_header,
    build_why,
    classify_intent,
    classify_intent_detailed,
    corpus_weights,
    coverage_adjustment,
    dedupe_near_identical,
    exact_name_bonus,
    infer_matched_field,
    fame_boost,
    poi_category_adjustment,
    profile_similarity,
    proximity_decay_bonus,
    query_style_tokens,
    reciprocal_rank_fusion,
    style_name_decoy_penalty,
    token_coverage,
    venue_style_affinity,
    W_EXACT_NAME,
    W_FULL_COVERAGE,
    W_LOW_COVERAGE,
    W_NAME_SUBSET,
    W_POI_CATEGORY_MISMATCH,
    W_POI_CATEGORY_UNKNOWN,
    W_STYLE_NAME_DECOY,
    W_VENUE_ERA,
    W_VENUE_STYLE,
)

requires_search_db = pytest.mark.skipif(
    not os.environ.get("SEARCH_DB_URL"),
    reason="SEARCH_DB_URL not set; skipping DB-dependent test",
)


# ---------------------------------------------------------------------------
# Intent router — table-driven
# ---------------------------------------------------------------------------

INTENT_CASES = [
    ("Chrysler Building", "name"),
    ("The Dakota", "name"),
    ("Woolworth Building", "name"),
    ("350 Fifth Avenue", "address"),
    ("35-01 Queens Blvd", "address"),
    ("123 Main Street", "address"),
    ("dimly lit speakeasy", "poi"),
    ("best coffee shop near me", "poi"),
    ("rooftop bar with a view", "poi"),
    ("buildings designed by Cass Gilbert", "architect"),
    ("who was the architect of this building", "architect"),
    ("art deco skyscrapers midtown", "style"),
    ("brutalist buildings in nyc", "style"),
    ("demolished penn station", "lore"),
    ("lost buildings of new york", "lore"),
    ("former site of the world trade center", "lore"),
    ("1977 blackout", "event"),
    ("buildings that survived a major fire", "event"),
    ("tell me about buildings that feel like they belong in a noir film", "prose"),
]


@pytest.mark.parametrize("query,expected", INTENT_CASES)
def test_classify_intent(query, expected):
    assert classify_intent(query) == expected


def test_classify_intent_empty_string_is_prose():
    assert classify_intent("") == "prose"
    assert classify_intent("   ") == "prose"


def test_corpus_weights_has_entry_for_every_intent():
    for intent in ("name", "address", "poi", "architect", "style", "lore", "event", "prose"):
        w = corpus_weights(intent)
        assert set(w.keys()) == {"buildings", "venues", "layers"}
        assert all(v > 0 for v in w.values())


def test_corpus_weights_unknown_intent_falls_back_to_prose():
    assert corpus_weights("nonsense") == corpus_weights("prose")


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def test_rrf_top_rank_in_single_corpus_wins():
    legs = {
        "buildings": [RankedHit("buildings", "b1", 1, {}), RankedHit("buildings", "b2", 2, {})],
        "venues": [],
        "layers": [],
    }
    weights = {"buildings": 1.0, "venues": 1.0, "layers": 1.0}
    fused = reciprocal_rank_fusion(legs, weights)
    assert fused[0][0] == "buildings:b1"
    assert fused[0][1] > fused[1][1]


def test_rrf_same_key_across_corpora_does_not_merge():
    # "b1" appearing in both buildings and venues corpora must stay distinct
    # (global key is "corpus:key").
    legs = {
        "buildings": [RankedHit("buildings", "x1", 1, {})],
        "venues": [RankedHit("venues", "x1", 1, {})],
        "layers": [],
    }
    weights = {"buildings": 1.0, "venues": 1.0, "layers": 1.0}
    fused = reciprocal_rank_fusion(legs, weights)
    keys = {gk for gk, _, _ in fused}
    assert keys == {"buildings:x1", "venues:x1"}


def test_rrf_combined_ranks_beat_single_corpus_rank():
    # A hit ranked mid-pack in TWO corpora should outscore a hit ranked #1 in
    # only one corpus once RRF contributions accumulate — this is the premise
    # of fusion (agreement across corpora matters). Global comparison is
    # buildings:agree vs venues:solo (different global keys, so this compares
    # the fused totals rather than a single leg's contribution).
    legs = {
        "buildings": [RankedHit("buildings", "agree", 5, {})],
        "venues": [RankedHit("venues", "agree", 5, {}), RankedHit("venues", "solo", 1, {})],
        "layers": [],
    }
    weights = {"buildings": 1.0, "venues": 1.0, "layers": 1.0}
    fused = reciprocal_rank_fusion(legs, weights)
    scores = {gk: s for gk, s, _ in fused}
    combined_agree = scores["buildings:agree"] + scores["venues:agree"]
    assert combined_agree > scores["venues:solo"]


def test_rrf_weight_zero_excludes_corpus_contribution():
    legs = {
        "buildings": [RankedHit("buildings", "b1", 1, {})],
        "venues": [RankedHit("venues", "v1", 1, {})],
        "layers": [],
    }
    weights = {"buildings": 1.0, "venues": 0.0, "layers": 1.0}
    fused = reciprocal_rank_fusion(legs, weights)
    scores = {gk: s for gk, s, _ in fused}
    assert scores["venues:v1"] == 0.0
    assert scores["buildings:b1"] > 0.0


def test_apply_nudges_are_small_and_only_break_ties():
    base = 0.5
    with_prox = apply_nudges(base, dist_m=0)
    with_novelty = apply_nudges(base, is_novel=True)
    with_personalization = apply_nudges(base, personalization_dot=1.0)
    # None of the nudges should be able to overturn a real 0.1+ score gap.
    assert with_prox - base < 0.1
    assert with_novelty - base < 0.1
    assert with_personalization - base < 0.1
    assert with_prox > base
    assert with_novelty > base
    assert with_personalization > base


def test_apply_nudges_far_distance_decays_to_no_bonus():
    base = 0.5
    far = apply_nudges(base, dist_m=100_000)
    assert far == pytest.approx(base, abs=1e-9)


# ---------------------------------------------------------------------------
# profile_similarity — personalization dot product
# ---------------------------------------------------------------------------

def test_profile_similarity_basic_dot_product():
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert profile_similarity(a, b) == pytest.approx(1.0)


def test_profile_similarity_orthogonal_vectors_are_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert profile_similarity(a, b) == pytest.approx(0.0)


def test_profile_similarity_none_inputs_return_none():
    assert profile_similarity(None, [1.0, 2.0]) is None
    assert profile_similarity([1.0, 2.0], None) is None
    assert profile_similarity(None, None) is None


def test_profile_similarity_empty_inputs_return_none():
    assert profile_similarity([], [1.0, 2.0]) is None
    assert profile_similarity([1.0], []) is None


def test_profile_similarity_mismatched_length_returns_none():
    assert profile_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) is None


def test_profile_similarity_all_zero_vectors_is_zero():
    a = [0.0] * 9
    b = [0.0] * 9
    assert profile_similarity(a, b) == pytest.approx(0.0)


def test_profile_similarity_nine_dim_archetype_vectors():
    a = [0.1] * 9
    b = [0.2] * 9
    assert profile_similarity(a, b) == pytest.approx(9 * 0.1 * 0.2)


# ---------------------------------------------------------------------------
# why / header — pure builders
# ---------------------------------------------------------------------------

def test_build_why_always_non_empty():
    assert build_why() == "match"
    assert build_why(fallback_snippet="") == "match"


def test_build_why_includes_style_year_and_matched_field():
    why = build_why(matched_field="architect", year=1928, style="Art Deco")
    assert "Art Deco" in why
    assert "1928" in why
    assert "matched: architect" in why


def test_build_why_falls_back_to_snippet_when_no_structured_fields():
    why = build_why(fallback_snippet="a lovely cast-iron loft building")
    assert why.startswith("a lovely cast-iron loft")


def test_build_header_empty_hits():
    assert build_header([], "name") == "No results"


def test_build_header_counts_and_era_span():
    hits = [
        {"style": "Art Deco", "year": 1927, "type": "building"},
        {"style": "Art Deco", "year": 1937, "type": "building"},
        {"style": "Art Deco", "year": 1930, "type": "building"},
    ]
    header = build_header(hits, "style")
    assert header.startswith("3 Art Deco")
    assert "1927" in header
    assert "1937" in header


def test_build_header_single_year_no_dash():
    hits = [{"style": "Gothic", "year": 1913, "type": "building"}]
    header = build_header(hits, "style")
    assert "1913" in header
    assert "–" not in header


def test_build_facets_derives_from_provided_values_only():
    facets = build_facets({"style": ["Art Deco", None, ""], "lore_status": ["demolished"]})
    kinds = {f["kind"] for f in facets}
    assert kinds == {"style", "lore_status"}
    assert all(f["value"] not in (None, "") for f in facets)


@requires_search_db
def test_live_search_db_connection_placeholder():
    # Placeholder for future integration tests against a real SEARCH_DB_URL.
    # Intentionally not exercising network here — kept skip-gated per spec.
    assert os.environ.get("SEARCH_DB_URL")


# ---------------------------------------------------------------------------
# classify_intent_detailed — POI noun exposure
# ---------------------------------------------------------------------------

def test_classify_intent_detailed_exposes_poi_noun():
    intent, noun = classify_intent_detailed("art deco bar")
    assert intent == "poi"
    assert noun == "bar"


def test_classify_intent_detailed_singularizes_plural_noun():
    intent, noun = classify_intent_detailed("best bars in brooklyn")
    assert intent == "poi"
    assert noun == "bar"


def test_classify_intent_detailed_prefers_first_matching_token():
    # Query mentions two POI nouns; the FIRST one in query order wins
    # (deterministic, not set-iteration order).
    intent, noun = classify_intent_detailed("cafe with a bar")
    assert intent == "poi"
    assert noun == "cafe"


def test_classify_intent_detailed_non_poi_intent_has_no_noun():
    intent, noun = classify_intent_detailed("Chrysler Building")
    assert intent == "name"
    assert noun is None


def test_classify_intent_still_matches_classify_intent_detailed():
    # classify_intent must stay a thin wrapper — no behavior drift.
    for q, _ in INTENT_CASES:
        assert classify_intent(q) == classify_intent_detailed(q)[0]


# ---------------------------------------------------------------------------
# poi_category_adjustment — category-family rank nudge
# ---------------------------------------------------------------------------

def test_poi_category_adjustment_boosts_matching_family():
    assert poi_category_adjustment("Cocktail Bar", "bar") > 0
    assert poi_category_adjustment("Speakeasy", "bar") > 0
    assert poi_category_adjustment("Pub", "bar") > 0


def test_poi_category_adjustment_demotes_unrelated_family():
    assert poi_category_adjustment("Art Gallery", "bar") < 0
    assert poi_category_adjustment("Antique Store", "bar") < 0


def test_poi_category_adjustment_neutral_when_no_noun_or_category():
    assert poi_category_adjustment("Cocktail Bar", None) == 0.0
    assert poi_category_adjustment(None, "bar") == 0.0


def test_poi_category_adjustment_neutral_for_unmapped_noun():
    # A POI noun with no _POI_CATEGORY_FAMILIES entry is neutral, not an error.
    assert poi_category_adjustment("Cocktail Bar", "venue") == 0.0


def test_poi_category_adjustment_does_not_false_match_substring():
    # "Publisher"/"Public Art" contain "pub" as a substring but aren't in the
    # bar family — word-boundary matching must not treat them as a BOOST.
    # (They match no known family at all, so they get the mild unknown-category
    # demotion, never the family boost.)
    assert poi_category_adjustment("Publisher", "bar") == W_POI_CATEGORY_UNKNOWN
    assert poi_category_adjustment("Public Art", "bar") == W_POI_CATEGORY_UNKNOWN


def test_poi_category_adjustment_demotes_confirmed_junk_case():
    # "Andy Leong Sushi Bar" (category "Sushi Restaurant") was one of the
    # reported junk hits for q="art deco bar" — confirm it demotes.
    assert poi_category_adjustment("Sushi Restaurant", "bar") < 0


def test_poi_category_adjustment_cross_family_demotes():
    # A category that word-matches a DIFFERENT known family (Bookstore vs a
    # "bar" query) is confidently the wrong kind of place — real demotion,
    # stronger than the unknown-category one.
    assert poi_category_adjustment("Bookstore", "bar") == W_POI_CATEGORY_MISMATCH
    assert poi_category_adjustment("Bookstore", "bar") < W_POI_CATEGORY_UNKNOWN


def test_poi_category_adjustment_magnitude_is_small():
    # Never large enough to be a de-facto hard filter.
    assert abs(poi_category_adjustment("Art Gallery", "bar")) < 0.2
    assert poi_category_adjustment("Cocktail Bar", "bar") < 0.2


# ---------------------------------------------------------------------------
# infer_matched_field — attributes a lexical hit to the field that actually
# overlaps the query, instead of a hardcoded "name/architect" label.
# ---------------------------------------------------------------------------

def test_infer_matched_field_picks_style_when_only_style_overlaps():
    label = infer_matched_field("art deco bar", name="123 Random St", style="Art Deco")
    assert label == "style"


def test_infer_matched_field_picks_name_over_style_on_tie_preference():
    # "Deco" overlaps both a name and a style field equally (1 token each) —
    # name wins as the more specific/stronger signal.
    label = infer_matched_field("deco", name="Deco Tower", style="Deco Revival")
    assert label == "name"


def test_infer_matched_field_prefers_field_with_more_overlap():
    label = infer_matched_field("art deco bar", name="The Deco", style="Art Deco Revival Bar Building")
    assert label == "style"


def test_infer_matched_field_falls_back_to_default_on_no_overlap():
    label = infer_matched_field("gothic revival", name="Sunset Diner", style="Art Deco", default="semantic")
    assert label == "semantic"


def test_infer_matched_field_empty_query_returns_default():
    assert infer_matched_field("", name="Chrysler Building") == "semantic"


def test_infer_matched_field_category_is_lowest_priority():
    label = infer_matched_field("bar", name="bar none", category="Bar")
    # "bar" overlaps both name ("bar none") and category ("Bar") with 1 token
    # each; name wins per the name > architect > style > category ordering.
    assert label == "name"


# ---------------------------------------------------------------------------
# proximity_decay_bonus / fame_boost — soft-radius + fame nudges
# ---------------------------------------------------------------------------

def test_proximity_decay_bonus_closer_is_larger():
    assert proximity_decay_bonus(100) > proximity_decay_bonus(3000)
    assert proximity_decay_bonus(0) > proximity_decay_bonus(100)


def test_proximity_decay_bonus_none_or_negative_is_zero():
    assert proximity_decay_bonus(None) == 0.0
    assert proximity_decay_bonus(-5) == 0.0


def test_proximity_decay_bonus_far_away_decays_toward_zero():
    assert proximity_decay_bonus(50_000) == pytest.approx(0.0, abs=1e-3)


def test_proximity_decay_bonus_is_small():
    assert proximity_decay_bonus(0) < 0.1


def test_fame_boost_applies_only_on_boosted_intents():
    assert fame_boost("style", 0.8) > 0.0
    assert fame_boost("poi", 0.8) == 0.0  # not in FAME_BOOST_INTENTS


def test_fame_boost_is_continuous_and_scales_with_fame():
    assert fame_boost("style", 1.0) > fame_boost("style", 0.5) > fame_boost("style", 0.05)
    # An icon-tier score dominates a median row's boost (~0.08 fame).
    assert fame_boost("style", 0.8) > 5 * fame_boost("style", 0.08)


def test_fame_boost_zero_for_missing_fame_and_clamps_range():
    assert fame_boost("style", None) == 0.0
    assert fame_boost("style", 0.0) == 0.0
    assert fame_boost("style", 2.0) == fame_boost("style", 1.0)
    assert fame_boost("style", -1.0) == 0.0


# ---------------------------------------------------------------------------
# dedupe_near_identical — collapse same-name, near-coincident hits
# ---------------------------------------------------------------------------

def _hit(name, lat, lng, score, **extra):
    return {"name": name, "lat": lat, "lng": lng, "score": score, **extra}


def test_dedupe_collapses_same_name_within_distance_keeping_highest_score():
    hits = [
        _hit("Court Name: Roosevelt", 40.7468, -73.9163, 0.5),
        _hit("Court Name: Roosevelt", 40.7469, -73.9164, 0.8),  # ~15m away, higher score
    ]
    out = dedupe_near_identical(hits)
    assert len(out) == 1
    assert out[0]["score"] == 0.8


def test_dedupe_keeps_same_name_far_apart():
    hits = [
        _hit("Roosevelt House", 40.70, -73.90, 0.5),
        _hit("Roosevelt House", 40.80, -74.00, 0.6),  # far away, different building
    ]
    out = dedupe_near_identical(hits)
    assert len(out) == 2


def test_dedupe_keeps_different_names_even_if_close():
    hits = [
        _hit("Chrysler Building", 40.7516, -73.9755, 0.9),
        _hit("Chanin Building", 40.7517, -73.9756, 0.8),
    ]
    out = dedupe_near_identical(hits)
    assert len(out) == 2


def test_dedupe_normalizes_name_case_and_punctuation():
    hits = [
        _hit("Court Name: Roosevelt", 40.7468, -73.9163, 0.5),
        _hit("court name roosevelt", 40.7469, -73.9164, 0.9),
    ]
    out = dedupe_near_identical(hits)
    assert len(out) == 1
    assert out[0]["score"] == 0.9


def test_dedupe_leaves_hits_without_coords_untouched():
    hits = [
        _hit("Some Layer Event", None, None, 0.5),
        _hit("Some Layer Event", None, None, 0.4),
    ]
    out = dedupe_near_identical(hits)
    # No lat/lng -> can't establish proximity -> both survive.
    assert len(out) == 2


def test_dedupe_empty_and_single_hit_lists():
    assert dedupe_near_identical([]) == []
    one = [_hit("X", 0.0, 0.0, 1.0)]
    assert dedupe_near_identical(one) == one


def test_dedupe_clusters_more_than_two_duplicates():
    hits = [
        _hit("Court Name: Roosevelt", 40.7468, -73.9163, 0.3),
        _hit("Court Name: Roosevelt", 40.7469, -73.9164, 0.9),
        _hit("Court Name: Roosevelt", 40.74685, -73.91635, 0.5),
    ]
    out = dedupe_near_identical(hits)
    assert len(out) == 1
    assert out[0]["score"] == 0.9


# ---------------------------------------------------------------------------
# Round 3 — exact-name dominance (name intent)
# ---------------------------------------------------------------------------

def test_exact_name_bonus_exact_match_is_dominant():
    assert exact_name_bonus("chrysler building", "Chrysler Building") == W_EXACT_NAME
    # Dominant over every other nudge combined (all others <= 0.07 each).
    assert W_EXACT_NAME > 0.07 * 3


def test_exact_name_bonus_subset_match():
    # "chrysler" is a subset of the full name — strong but below exact.
    assert exact_name_bonus("chrysler", "Chrysler Building") == W_NAME_SUBSET
    assert W_NAME_SUBSET < W_EXACT_NAME


def test_exact_name_bonus_partial_overlap_is_zero():
    # Sharing one of two tokens is NOT a subset match.
    assert exact_name_bonus("chrysler tower", "Chrysler Building") == 0.0


def test_exact_name_bonus_no_overlap_or_missing_name():
    assert exact_name_bonus("chrysler", "Engine Company No. 14") == 0.0
    assert exact_name_bonus("chrysler", None) == 0.0
    assert exact_name_bonus("", "Chrysler Building") == 0.0


def test_exact_name_bonus_ignores_punctuation_and_case():
    assert exact_name_bonus("the dakota", "The Dakota!") == W_EXACT_NAME


# ---------------------------------------------------------------------------
# Round 3 — venue style/era affinity (poi intent)
# ---------------------------------------------------------------------------

def test_query_style_tokens_strips_poi_noun():
    assert query_style_tokens("art deco bar", "bar") == frozenset({"art", "deco"})


def test_query_style_tokens_empty_for_plain_poi_query():
    assert query_style_tokens("bars near me", "bar") == frozenset()


def test_venue_style_affinity_style_text_match():
    toks = frozenset({"art", "deco"})
    assert venue_style_affinity(toks, "Art Deco", None) == W_VENUE_STYLE


def test_venue_style_affinity_era_fallback():
    # No host style text, but built 1928 = inside the deco window.
    toks = frozenset({"deco"})
    assert venue_style_affinity(toks, None, 1928) == W_VENUE_ERA
    assert W_VENUE_ERA < W_VENUE_STYLE


def test_venue_style_affinity_outside_era_is_zero():
    assert venue_style_affinity(frozenset({"deco"}), None, 1890) == 0.0


def test_venue_style_affinity_no_style_tokens_is_zero():
    assert venue_style_affinity(frozenset(), "Art Deco", 1928) == 0.0


def test_venue_style_affinity_unknown_style_token_no_era_fallback():
    # "revival" has no era window — year alone can't earn the bonus.
    assert venue_style_affinity(frozenset({"revival"}), None, 1928) == 0.0


# ---------------------------------------------------------------------------
# Round 3 — style-name decoy penalty (poi intent)
# ---------------------------------------------------------------------------

def test_style_name_decoy_penalizes_high_style_deco():
    # The confirmed live junk case: antique store whose NAME matches the
    # query's style words.
    p = style_name_decoy_penalty("art deco bar", "High Style Deco", "Antique Store", "bar")
    assert p == W_STYLE_NAME_DECOY


def test_style_name_decoy_spares_real_bar_named_deco():
    # A real bar named after the style is a great hit, not a decoy.
    p = style_name_decoy_penalty("art deco bar", "Deco Bar", "Cocktail Bar", "bar")
    assert p == 0.0


def test_style_name_decoy_zero_when_name_overlap_is_not_style():
    p = style_name_decoy_penalty("art deco bar", "O'Casey's Irish Bar", "Irish Pub", "bar")
    assert p == 0.0


def test_style_name_decoy_zero_without_noun_or_name():
    assert style_name_decoy_penalty("art deco", "High Style Deco", "Antique Store", None) == 0.0
    assert style_name_decoy_penalty("art deco bar", None, "Antique Store", "bar") == 0.0


# ---------------------------------------------------------------------------
# Round 3 — multi-token coverage (lore/event intents)
# ---------------------------------------------------------------------------

def test_token_coverage_full():
    assert token_coverage("demolished theaters", "Center Theatre — demolished 1954", None) == 1.0


def test_token_coverage_plural_folding_both_ways():
    # Query plural, text singular:
    assert token_coverage("demolished theaters", "a demolished theater") == 1.0
    # Query singular, text plural:
    assert token_coverage("demolished theater", "demolished theaters of Broadway") == 1.0


def test_token_coverage_partial():
    cov = token_coverage("demolished theaters", "Gimbels Skybridge (demolished)", None)
    assert cov == 0.5


def test_token_coverage_no_content_tokens_is_neutral_one():
    assert token_coverage("", "anything") == 1.0


def test_coverage_adjustment_rewards_full_and_demotes_low():
    assert coverage_adjustment("lore", "demolished theaters", "Center Theatre", "demolished in 1954") == W_FULL_COVERAGE
    # Single-concept match on a 2-token query = 0.5 coverage — the canonical
    # defect case (skybridge matching only "demolished") MUST demote.
    assert coverage_adjustment("lore", "demolished theaters", "Gimbels Skybridge (demolished)", None) == -W_LOW_COVERAGE
    assert coverage_adjustment("lore", "demolished theaters unbuilt", "Gimbels Skybridge (demolished)", None) == -W_LOW_COVERAGE


def test_coverage_adjustment_only_lore_and_event_intents():
    assert coverage_adjustment("style", "demolished theaters", "Skybridge", None) == 0.0
    assert coverage_adjustment("name", "demolished theaters", "Skybridge", None) == 0.0


def test_coverage_adjustment_single_token_query_is_neutral():
    assert coverage_adjustment("lore", "demolished", "Skybridge", None) == 0.0


# ---------------------------------------------------------------------------
# Round 3 — header fixes
# ---------------------------------------------------------------------------

def test_build_header_style_needs_majority():
    # One styled hit out of ten must NOT brand the whole set.
    hits = [{"type": "lore", "style": None, "year": 1900 + i} for i in range(9)]
    hits.append({"type": "lore", "style": "medieval revival", "year": 1925})
    assert "medieval revival" not in build_header(hits, "lore")


def test_build_header_lore_pluralizes_as_entries():
    hits = [{"type": "lore", "style": None, "year": 1900 + i} for i in range(3)]
    h = build_header(hits, "lore")
    assert "lore entries" in h
    assert "lores" not in h

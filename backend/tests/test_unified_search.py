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
    corpus_weights,
    profile_similarity,
    reciprocal_rank_fusion,
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

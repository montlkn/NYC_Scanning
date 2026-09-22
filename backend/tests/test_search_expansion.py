"""LLM expansion, place/category scoring, and venue hygiene (2026-09-22)."""
import json

from scripts.enrich_venues import base_parts, build_text, is_searchable, normalize_category
from scripts.ingest_overture_places import wanted
from services.unified_search import (
    INTERP_VERSION,
    RankedHit,
    W_LLM_CATEGORY,
    W_PLACE_MATCH,
    W_PLACE_MISMATCH,
    has_direct_match,
    llm_category_bonus,
    parse_interpretation,
    place_adjustment,
    reciprocal_rank_fusion,
)


class TestParseInterpretation:
    def test_valid_json(self):
        raw = json.dumps({"queries": ["cemetery mausoleum", "murder"], "categories": [],
                          "neighborhoods": [], "boroughs": ["Brooklyn", "Jersey"]})
        d = parse_interpretation(raw, "spooky spots")
        assert d["v"] == INTERP_VERSION
        assert d["queries"] == ["cemetery mausoleum", "murder"]
        assert d["boroughs"] == ["Brooklyn"]  # not a borough, dropped

    def test_garbage_is_dropped_not_guessed(self):
        assert parse_interpretation("not json", "x") is None
        assert parse_interpretation("[1,2]", "x") is None
        assert parse_interpretation(None, "x") is None

    def test_code_fence_tolerated(self):
        d = parse_interpretation('```json\n{"queries":["a b"]}\n```', "q")
        assert d["queries"] == ["a b"]

    def test_echo_of_the_query_is_removed(self):
        d = parse_interpretation('{"queries":["Seagram Bar","Seagram Building"]}', "seagram bar")
        assert d["queries"] == ["Seagram Building"]


class TestPlaceAdjustment:
    req = {"neighborhoods": ["Midtown"], "boroughs": []}

    def test_match_by_nta_tokens(self):
        assert place_adjustment(self.req, {"neighborhood": "East Midtown-Turtle Bay"}) == W_PLACE_MATCH

    def test_known_elsewhere_is_demoted(self):
        assert place_adjustment(self.req, {"neighborhood": "Park Slope"}) == W_PLACE_MISMATCH

    def test_unknown_place_is_left_alone(self):
        assert place_adjustment(self.req, {"neighborhood": None}) == 0.0

    def test_token_outside_nta_vocab_cannot_force_a_mismatch(self):
        vocab = {"midtown", "times", "square", "park", "slope"}
        req = {"neighborhoods": ["Midtown Manhattan"], "boroughs": []}
        assert place_adjustment(req, {"neighborhood": "Midtown-Times Square"}, vocab) == W_PLACE_MATCH

    def test_apostrophes(self):
        req = {"neighborhoods": ["hells kitchen"], "boroughs": []}
        assert place_adjustment(req, {"neighborhood": "Hell's Kitchen"}) == W_PLACE_MATCH

    def test_borough(self):
        req = {"neighborhoods": [], "boroughs": ["Brooklyn"]}
        assert place_adjustment(req, {"borough": "Brooklyn"}) == W_PLACE_MATCH
        assert place_adjustment(req, {"borough": "Queens"}) == W_PLACE_MISMATCH


def test_llm_category_bonus_is_exact_and_venue_only():
    interp = {"categories": ["Cocktail Bar", "Lounge"]}
    assert llm_category_bonus(interp, {"type": "venue", "category": "Cocktail Bar"}) == W_LLM_CATEGORY
    assert llm_category_bonus(interp, {"type": "venue", "category": "Seafood Restaurant"}) == 0.0
    assert llm_category_bonus(interp, {"type": "building", "category": "Cocktail Bar"}) == 0.0


class TestDirectMatch:
    def test_exact_name_is_direct(self):
        legs = {"buildings": [{"name": "Chrysler Building"}]}
        assert has_direct_match("chrysler", "name", legs)

    def test_archetype_word_alone_is_not_direct(self):
        # "romantic" is an archetype; one word of three does not answer
        # "romantic dinner brooklyn".
        legs = {"buildings": [{"name": "Holy Trinity Church", "aesthetic": "romantic",
                               "neighborhood": "Brooklyn Heights"}]}
        assert not has_direct_match("romantic dinner brooklyn", "name", legs)

    def test_archetype_query_is_direct(self):
        legs = {"buildings": [{"name": "X", "aesthetic": "austerist"}]}
        assert has_direct_match("austerist", "style", legs)

    def test_single_coincidental_title_is_not_direct(self):
        legs = {"layers": [{"name": "The spooky spider web windows"}],
                "buildings": [{"name": "First Houses"}]}
        assert not has_direct_match("spooky", "name", legs)

    def test_many_covering_hits_are_direct(self):
        bars = [{"name": f"Bar {i}", "category": "Bar", "style": "art deco"} for i in range(6)]
        assert has_direct_match("art deco bar", "poi", {"venues": bars})


def test_rrf_merges_a_rewrite_leg_into_the_same_entry():
    h = {"id": "1"}
    legs = {
        "buildings": [RankedHit("buildings", "1", 1, h)],
        "buildings~0": [RankedHit("buildings", "1", 1, h)],
    }
    fused = reciprocal_rank_fusion(legs, {"buildings": 1.0, "buildings~0": 1.0})
    assert len(fused) == 1
    assert fused[0][0] == "buildings:1"


class TestVenueHygiene:
    def test_new_jersey_is_not_searchable(self):
        assert not is_searchable("Some Bar", "Bar", (), False)

    def test_non_places(self):
        assert not is_searchable("1204 Broadway", "Structure", (), True)
        assert not is_searchable("414", "Night Club", (), True)
        assert not is_searchable("Restaurant", "Mexican Restaurant", (), True)
        assert not is_searchable("375 Park Food Llc", "Restaurant", (), True)
        assert not is_searchable("Tobias Meyer", "Law Office", ("Business and Professional Services",), True)
        assert not is_searchable("Mimvi SEO", "Marketing Agency", (), True, "overture")

    def test_real_places(self):
        assert is_searchable("The Bar", "Cocktail Bar", ("Dining and Drinking",), True)
        assert is_searchable("787 Coffee", "Coffee Shop", (), True)
        assert is_searchable("Green-Wood", "Cemetery", (), True, "overture")

    def test_category_normalization(self):
        assert normalize_category("cocktail_bar") == "Cocktail Bar"
        assert normalize_category("landmark_and_historical_building") == "Landmark and Historical Building"
        assert normalize_category("Cocktail Bar") == "Cocktail Bar"

    def test_text_rebuild_is_idempotent(self):
        t = build_text(["The Pool", "Seafood Restaurant"], None, None, "Seagram Building",
                       1958, "international style", "East Midtown-Turtle Bay", "Manhattan", "")
        again = build_text(base_parts(t), None, None, "Seagram Building",
                           1958, "international style", "East Midtown-Turtle Bay", "Manhattan", "")
        assert t == again
        assert "in the Seagram Building" in t and "Manhattan" in t


class TestOvertureFilterIsTokenAnchored:
    def test_substring_false_positives_are_gone(self):
        for slug in ("marketing_agency", "courier_and_delivery_services", "public_relations",
                     "topic_publisher", "notary_public", "martial_arts_club", "interior_design"):
            assert not wanted(slug), slug

    def test_destinations_survive(self):
        for slug in ("caribbean_restaurant", "spanish_restaurant", "cocktail_bar", "pub",
                     "delicatessen", "farmers_market", "church_cathedral", "cemetery", "art_supply_store"):
            assert wanted(slug), slug


def test_llm_style_bonus_matches_the_rows_own_style():
    from services.unified_search import W_LLM_STYLE, llm_style_bonus
    interp = {"styles": ["international style", "brutalist"]}
    assert llm_style_bonus(interp, {"style": "International Style"}) == W_LLM_STYLE
    assert llm_style_bonus(interp, {"style": "modern brutalist"}) == W_LLM_STYLE
    assert llm_style_bonus(interp, {"style": "italianate"}) == 0.0
    assert llm_style_bonus(interp, {"style": None}) == 0.0


def test_web_domains_are_not_venue_names():
    assert not is_searchable("Elove.com", "Bar", (), True)
    assert is_searchable("Dante NYC", "Bar", (), True)

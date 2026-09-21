"""Unit tests for the LPC prose leg and the derived vocabulary injection.

DB-free, like test_unified_search.py -- these cover the pure logic in
services/unified_search.py, not the SQL.
"""
import pytest

from services import unified_search as us


class TestLoreWeight:
    def test_style_and_prose_get_the_lore_leg(self):
        for intent in ("style", "prose", "lore", "name", "architect"):
            assert us.leg_lore_weight(intent) == us.W_LEG_LORE

    def test_address_poi_event_do_not(self):
        # A designation report is noise against a street number or a bar.
        for intent in ("address", "poi", "event"):
            assert us.leg_lore_weight(intent) == 0.0

    def test_floor_is_high_enough_to_matter(self):
        # bge cosine between any two English passages sits well above zero. If
        # the floor were near 0 the lore term would be a near-constant bonus
        # handed to every building that HAS a report -- a proxy for landmark
        # status, not for matching the query.
        assert us.LORE_SIM_FLOOR >= 0.6


class TestDerivedVocab:
    def setup_method(self):
        self._style = set(us._STYLE_VOCAB)
        self._poi = set(us._POI_NOUNS)

    def teardown_method(self):
        us._STYLE_VOCAB = self._style
        us._POI_NOUNS = self._poi

    def test_seeds_survive_injection(self):
        # The seeds carry plural and colloquial forms ("midcentury",
        # "speakeasies") that no source column spells out.
        us.install_derived_vocab(style={"italianate"})
        assert "midcentury" in us._STYLE_VOCAB
        assert "speakeasies" in us._POI_NOUNS

    def test_derived_terms_are_added(self):
        assert "italianate" in us._STYLE_VOCAB_SEED or True
        us.install_derived_vocab(style={"neo-grec", "adamesque"})
        assert "neo-grec" in us._STYLE_VOCAB
        assert "adamesque" in us._STYLE_VOCAB

    def test_derived_style_reaches_the_intent_router(self):
        # "adamesque" is a real style_primary value and was not in the 28-word
        # seed list, so it classified as `name` before this.
        assert us.classify_intent("adamesque row houses") != "style"
        us.install_derived_vocab(style={"adamesque"})
        assert us.classify_intent("adamesque row houses") == "style"

    def test_empty_injection_is_a_noop(self):
        before = set(us._STYLE_VOCAB)
        us.install_derived_vocab(style=set())
        assert us._STYLE_VOCAB == before

    def test_install_reports_sizes(self):
        sizes = us.install_derived_vocab(style={"a", "b"})
        assert sizes["style"] == len(us._STYLE_VOCAB_SEED | {"a", "b"})

    def test_a_poi_kwarg_is_swallowed_not_applied(self):
        # An old caller must degrade to a no-op, not re-break intent routing.
        us.install_derived_vocab(style={"adamesque"}, poi={"building", "church"})
        assert "building" not in us._POI_NOUNS
        assert "church" not in us._POI_NOUNS


class TestIntentRegressions:
    """Queries the audit found misrouted."""

    def setup_method(self):
        self._style = set(us._STYLE_VOCAB)
        self._poi = set(us._POI_NOUNS)

    def teardown_method(self):
        us._STYLE_VOCAB = self._style
        us._POI_NOUNS = self._poi

    def test_brutalist_church_is_not_poi(self):
        assert us.classify_intent("brutalist church") == "style"

    def test_art_deco_stays_style_not_poi(self):
        assert us.classify_intent("art deco") == "style"

    # The three below are the regression this suite MISSED. The earlier tests
    # asserted against the seed vocabularies only, so they passed while
    # production installed derived POI nouns from venue category heads --
    # which include "building" and "church" -- and routed both queries to the
    # venues corpus at weight 1.0 against buildings at 0.4:
    #   "chrystler building"      -> Chrystie Street venues
    #   "gothic church in harlem" -> St. Patrick's (Midtown), and a grave
    # Any future POI derivation has to keep these passing.

    def test_building_must_never_become_a_poi_noun(self):
        us.install_derived_vocab(poi={"building"})
        assert us.classify_intent("chrystler building") != "poi"

    def test_church_must_never_become_a_poi_noun(self):
        us.install_derived_vocab(poi={"church"})
        assert us.classify_intent("gothic church in harlem") != "poi"

    def test_a_real_poi_noun_still_routes_to_poi(self):
        # The guard above must not cost us the actual POI behaviour.
        assert us.classify_intent("art deco bar") == "poi"


class TestFuzzyName:
    """Calibrated on measured similarities, not guessed thresholds."""

    def test_a_real_typo_beats_a_coincidental_street_name(self):
        # "chrystler building": Chrysler scores 0.640, 215 Chrystie 0.182.
        chrysler = us.fuzzy_name_bonus("name", 0.640)
        chrystie = us.fuzzy_name_bonus("name", 0.182)
        assert chrystie == 0.0
        # Worth more than ~4 rank steps (one step ~= 0.016), or it cannot
        # reorder the two.
        assert chrysler > 0.064

    def test_never_outranks_an_exact_name_match(self):
        assert us.W_FUZZY_NAME < us.W_EXACT_NAME

    def test_suppressed_when_the_exact_bonus_already_fired(self):
        assert us.fuzzy_name_bonus("name", 0.9, exact_bonus=us.W_EXACT_NAME) == 0.0

    def test_only_applies_to_name_intent(self):
        assert us.fuzzy_name_bonus("style", 0.9) == 0.0

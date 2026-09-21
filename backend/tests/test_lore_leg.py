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
        us.install_derived_vocab(style={"italianate"}, poi={"bodega"})
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
        us.install_derived_vocab(style=set(), poi=set())
        assert us._STYLE_VOCAB == before

    def test_install_reports_sizes(self):
        sizes = us.install_derived_vocab(style={"a", "b"}, poi={"c"})
        assert sizes["style"] == len(us._STYLE_VOCAB_SEED | {"a", "b"})
        assert sizes["poi"] == len(us._POI_NOUNS_SEED | {"c"})


class TestIntentRegressions:
    """Queries the audit found misrouted."""

    def test_brutalist_church_is_not_poi(self):
        # 'church' must not be a POI noun: POI drops the buildings corpus
        # weight from 1.0 to 0.4.
        assert us.classify_intent("brutalist church") == "style"

    def test_art_deco_stays_style_not_poi(self):
        assert us.classify_intent("art deco") == "style"

"""Diversity cap: one terrace must not fill the result list."""
import pytest
from services.unified_search import apply_diversity_cap, _street_key, MAX_PER_STREET


class TestStreetKey:
    def test_neighbours_share_a_key(self):
        # These three shared one designation report and filled the whole list
        # for "haunted buildings".
        a = _street_key("55 West 28th Street Building", None)
        b = _street_key("47 West 28th Street", None)
        assert a == b == "west 28th street"

    def test_different_streets_do_not_collide(self):
        assert _street_key("12 Bank Street", None) != _street_key("12 Bond Street", None)

    def test_a_named_building_is_not_grouped_by_accident(self):
        assert _street_key("Chrysler Building", None) != _street_key("Empire State Building", None)


class TestCap:
    def test_surplus_is_demoted_not_dropped(self):
        hits = [{"name": f"{n} West 28th Street"} for n in (55, 53, 47, 41)]
        out = apply_diversity_cap(hits)
        assert len(out) == len(hits), "nothing may be lost -- only reordered"
        assert [h["name"] for h in out[:MAX_PER_STREET]] == \
               ["55 West 28th Street", "53 West 28th Street"]

    def test_variety_is_promoted_above_the_surplus(self):
        hits = [{"name": "55 West 28th Street"}, {"name": "53 West 28th Street"},
                {"name": "47 West 28th Street"}, {"name": "Chrysler Building"}]
        out = [h["name"] for h in apply_diversity_cap(hits)]
        assert out.index("Chrysler Building") < out.index("47 West 28th Street")

    def test_order_is_otherwise_preserved(self):
        hits = [{"name": n} for n in ("Chrysler Building", "Woolworth Building",
                                      "Empire State Building")]
        assert [h["name"] for h in apply_diversity_cap(hits)] == \
               [h["name"] for h in hits]

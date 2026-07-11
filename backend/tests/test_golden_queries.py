"""
Golden query suite for GET /api/search/unified.

Two tiers:

(a) Pure intent-router assertions — run always, no network/DB. Imports
    classify_intent() directly from services/unified_search.py (same pattern
    as test_unified_search.py's INTENT_CASES) and checks ~30 representative
    queries route to the expected intent bucket.

(b) Live end-to-end assertions — gated behind SEARCH_DB_URL (matching this
    repo's requires_search_db convention) AND a reachable server, configured
    via GOLDEN_QUERY_BASE_URL (default http://localhost:8000). No TestClient
    precedent exists in this repo (fastapi isn't even guaranteed importable
    in the test env — see test_unified_search.py's DB-free philosophy), so
    this uses a plain httpx.Client against a running uvicorn instance, same
    library already pinned in requirements.txt for the app itself.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.unified_search import classify_intent  # noqa: E402

requires_search_db = pytest.mark.skipif(
    not os.environ.get("SEARCH_DB_URL"),
    reason="SEARCH_DB_URL not set; skipping live golden-query test",
)

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

requires_httpx = pytest.mark.skipif(
    httpx is None, reason="httpx not importable; skipping live golden-query test"
)

BASE_URL = os.environ.get("GOLDEN_QUERY_BASE_URL", "http://localhost:8000")


# ---------------------------------------------------------------------------
# (a) Pure intent-router golden queries — no network, no DB.
# ---------------------------------------------------------------------------
# Categories mirror INTENTS in services/unified_search.py:
#   name / address / poi / architect / style / lore / event / prose
#
# Notes on tricky cases (all confirmed against the actual classify_intent
# rules cascade in services/unified_search.py, not guessed):
#   - Typo'd exact names ("chrysler bulding", "empire states building",
#     "flatiorn building") still route to "name": the address regex doesn't
#     match, none of the vocab sets hit, and they're short + NOT capitalized.
#     The capitalization branch (`cap_words >= len(words) - 1`) requires
#     almost every word capitalized; lowercase typo'd queries fail that
#     check, so they instead fall through to `len(words) <= 6 -> "name"`.
#   - "OMA" / "SOM" (bare acronyms, no ARCHITECT_MARKERS keyword like
#     "architect"/"designed") are short + fully capitalized -> "name", not
#     "architect". The router has no acronym/proper-noun-firm heuristic;
#     only explicit marker words ("architect", "designed", "firm", ...)
#     trigger the architect bucket.
#   - "Frank Lloyd Wright" is 3 capitalized words, no architect marker word
#     -> "name" (indistinguishable from a building name at the router level;
#     disambiguation happens downstream via corpus weighting, not intent).
#   - Event/nightlife queries like "techno tonight" and "live jazz this
#     weekend" contain no EVENT_VOCAB (disaster/fire/riot/etc.) or POI noun,
#     and both are <=6 words, so both fall through the capitalization check
#     (all-lowercase) straight to the `len(words) <= 6 -> "name"` rule.
#   - "buildings that burned down" similarly has no vocab hit ("burned" is
#     not in EVENT_VOCAB — only "fire" is) and is <=6 words -> "name".
#   - "rooftop bar open now" contains POI noun "bar" -> "poi" (event/
#     nightlife has no dedicated intent bucket in this router; POI is the
#     closest match when a venue noun is present).
INTENT_CASES = [
    # Exact building names
    ("Chrysler Building", "name"),
    ("Flatiron Building", "name"),
    ("One World Trade Center", "name"),
    ("Woolworth Building", "name"),
    ("The Dakota", "name"),
    # Typos on exact names — still short, lowercase -> falls to "name" via
    # the <=6-words fallback, not the capitalization branch.
    ("chrysler bulding", "name"),
    ("empire states building", "name"),
    ("flatiorn building", "name"),
    # Addresses
    ("480 Broadway", "address"),
    ("350 Fifth Avenue", "address"),
    ("20 West 34th Street", "address"),
    ("35-01 Queens Blvd", "address"),
    # Architect names / acronyms — only explicit marker words route here;
    # bare firm names/acronyms fall through to "name".
    ("OMA", "name"),
    ("SOM", "name"),
    ("Frank Lloyd Wright", "name"),
    ("McKim Mead & White", "name"),
    ("buildings designed by McKim Mead White", "architect"),
    ("who was the architect of the Chrysler Building", "architect"),
    # Style prose
    ("quiet gothic churches in brooklyn", "style"),
    ("brutalist buildings in the bronx", "style"),
    ("art nouveau facades", "style"),
    ("art deco skyscrapers midtown", "style"),
    # POI + era moat
    ("original midcentury bar", "poi"),
    ("art deco bar", "poi"),
    ("1920s speakeasy", "poi"),
    ("dimly lit speakeasy", "poi"),
    # Lore — "unbuilt" IS in _LORE_VOCAB (not _EVENT_VOCAB), so this routes
    # to "lore" despite reading like a building-type query.
    ("demolished theaters", "lore"),
    ("unbuilt skyscrapers", "lore"),
    # "burned" is not in _EVENT_VOCAB (only "fire" is) and no other vocab set
    # matches -> falls through to the <=6-words "name" branch (all lowercase,
    # so it's not caught by the capitalization check either way).
    ("buildings that burned down", "name"),
    ("lost buildings of new york", "lore"),
    # Event / nightlife — no dedicated intent bucket; falls to poi (has POI
    # noun) or name (<=6 words, no vocab match at all — the router has no
    # length ceiling below "name" until 6 words).
    ("techno tonight", "name"),
    ("live jazz this weekend", "name"),
    ("rooftop bar open now", "poi"),
]


@pytest.mark.parametrize("query,expected", INTENT_CASES)
def test_golden_intent_routing(query, expected):
    assert classify_intent(query) == expected


def test_golden_query_count_covers_all_categories():
    """Sanity check: the golden set actually spans every intent bucket the
    router can produce (minus 'event', which has no reliable trigger word
    in these particular phrasings — see INTENT_CASES notes)."""
    seen = {expected for _, expected in INTENT_CASES}
    assert len(INTENT_CASES) >= 28
    assert {"name", "address", "architect", "style", "poi", "lore"} <= seen


# ---------------------------------------------------------------------------
# (b) Live end-to-end golden queries against a running server.
# ---------------------------------------------------------------------------

LIVE_CASES = [
    ("Chrysler Building", "building"),
    ("Flatiron Building", "building"),
    ("350 Fifth Avenue", "building"),
    ("20 West 34th Street", "building"),
    ("art deco skyscrapers midtown", "building"),
    ("dimly lit speakeasy", "venue"),
    ("original midcentury bar", "venue"),
    ("demolished theaters", "lore"),
    ("lost buildings of new york", "lore"),
]


@requires_search_db
@requires_httpx
class TestLiveGoldenQueries:
    @classmethod
    def setup_class(cls):
        cls.client = httpx.Client(base_url=BASE_URL, timeout=10.0)
        try:
            cls.client.get("/api/search/unified", params={"q": "warmup"})
        except httpx.HTTPError as e:
            pytest.skip(f"search server not reachable at {BASE_URL}: {e}")

    @classmethod
    def teardown_class(cls):
        cls.client.close()

    @pytest.mark.parametrize("query,expected_type", LIVE_CASES)
    def test_golden_query_returns_expected_top_hit_type(self, query, expected_type):
        resp = self.client.get("/api/search/unified", params={"q": query})
        assert resp.status_code == 200
        data = resp.json()
        hits = data.get("hits", [])
        assert len(hits) > 0, f"no hits for query {query!r}"
        assert hits[0].get("type") == expected_type, (
            f"query {query!r}: expected top hit type {expected_type!r}, "
            f"got {hits[0].get('type')!r}"
        )

    def test_golden_query_warm_latency_under_2s(self):
        query = "Chrysler Building"
        # First call warms caches/connections; only the second is timed.
        self.client.get("/api/search/unified", params={"q": query})
        start = time.monotonic()
        resp = self.client.get("/api/search/unified", params={"q": query})
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        assert elapsed < 2.0, f"warm request took {elapsed:.2f}s (limit 2.0s)"

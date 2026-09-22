"""
Latency guards for the search legs.

WHY THIS FILE EXISTS: adding four retrieval pools took the buildings leg from
~0.9s to 5.65s and every one of the 231 existing tests still passed. They
assert what search RETURNS and nothing about what it COSTS, so a 6x regression
was invisible -- and the iOS client gives up at 8s, which would have pushed
real users onto the unranked fallback path without a single failing test.

The specific bug was `word_similarity(a, b) > k`, which cannot use a
gin_trgm_ops index: 693ms for one pool. The indexable form is the `<%`
operator. That mistake is easy to repeat, so it gets a test rather than a
comment.

These hit the real search DB and are skipped when SEARCH_DB_URL is unset, so
they do not break a checkout without credentials. Thresholds are deliberately
loose -- this catches an ORDER-OF-MAGNITUDE regression, not normal variance.
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SEARCH_DB_URL"),
    reason="needs SEARCH_DB_URL (the pgvector search DB)",
)

# Generous: a laptop pays WAN latency to Railway that the co-located backend
# does not, so these are ceilings for "something is structurally wrong", not
# performance targets. Server-side the same queries run in ~1.1s.
POOL_CEILING_MS = 400.0
LEG_CEILING_S = 8.0


@pytest.fixture(scope="module")
def cur():
    import psycopg2
    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    c = conn.cursor()
    yield c
    c.close()
    conn.close()


def _exec_ms(cur, sql, params) -> float:
    cur.execute("EXPLAIN (ANALYZE) " + sql, params)
    for (row,) in cur.fetchall():
        if "Execution Time" in row:
            return float(row.split(":")[1].split("ms")[0])
    raise AssertionError("no Execution Time in plan")


class TestLexicalPoolsUseAnIndex:
    """The lore pool must keep its indexable operator."""

    def test_lore_lex_pool_is_index_backed(self, cur):
        ms = _exec_ms(
            cur,
            "SELECT bin FROM building_lore_index "
            " WHERE lower(%s) <%% lower(text) "
            "   AND word_similarity(lower(%s), lower(text)) > 0.6 LIMIT 72",
            ("gargoyles", "gargoyles"),
        )
        assert ms < POOL_CEILING_MS, (
            f"lore_lex_pool took {ms:.0f}ms. The `<%` operator is what makes this "
            "index-backed; the bare word_similarity() call cannot use "
            "idx_bli_trgm and measured 3.81s."
        )

    def test_bare_word_similarity_is_the_slow_form(self, cur):
        """Documents WHY the operator is there, so removing it fails loudly."""
        fast = _exec_ms(
            cur,
            "SELECT bin FROM building_lore_index WHERE lower(%s) <%% lower(text) "
            "AND word_similarity(lower(%s), lower(text)) > 0.6 LIMIT 72",
            ("mansard", "mansard"),
        )
        slow = _exec_ms(
            cur,
            "SELECT bin FROM building_lore_index "
            "WHERE word_similarity(lower(%s), lower(text)) > 0.6 LIMIT 72",
            ("mansard",),
        )
        assert fast < slow, (
            "the <% operator is supposed to be faster than the bare function; "
            "if this flips, the index or the GUC changed"
        )


class TestPoolsStayBounded:
    def test_vector_pool_uses_hnsw(self, cur):
        ms = _exec_ms(
            cur,
            "SELECT bin FROM building_search_index ORDER BY embedding <=> %s::vector LIMIT 72",
            ("[" + ",".join(["0.01"] * 384) + "]",),
        )
        assert ms < POOL_CEILING_MS, (
            f"vector pool took {ms:.0f}ms -- an unbounded ORDER BY without a "
            "LIMIT stops HNSW being used at all (search_buildings_by_vector "
            "returned 35,219 rows this way)."
        )

    def test_venue_vector_pool_uses_hnsw(self, cur):
        ms = _exec_ms(
            cur,
            "SELECT fsq_id FROM venues ORDER BY embedding <=> %s::vector LIMIT 72",
            ("[" + ",".join(["0.01"] * 384) + "]",),
        )
        assert ms < POOL_CEILING_MS, f"venues vector pool took {ms:.0f}ms"


class TestFallbackRpcStaysUnderAnonTimeout:
    """buildings_text_search runs on Supabase under anon's 3s statement_timeout.

    It was 3,446ms before being narrowed to an index-backed candidate pool --
    i.e. it was timing out for real users on the path taken whenever the
    backend is slow.
    """

    def test_is_well_under_three_seconds(self):
        url = os.environ.get("DATABASE_URL")
        if not url:
            pytest.skip("needs DATABASE_URL (the BUILDINGS project)")
        import psycopg2
        with psycopg2.connect(url) as conn, conn.cursor() as c:
            t = time.time()
            c.execute("SELECT count(*) FROM buildings_text_search(%s, 20, false)",
                      ("art deco theater",))
            c.fetchall()
            elapsed = time.time() - t
        assert elapsed < 2.0, (
            f"buildings_text_search took {elapsed:.2f}s; anon's statement_timeout "
            "is 3s and this is the client's fallback path."
        )

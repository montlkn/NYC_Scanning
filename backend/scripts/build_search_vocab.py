"""
Derive the intent-router vocabularies from the corpus instead of hand-listing.

_STYLE_VOCAB was 28 hardcoded words against 761 distinct style_primary values,
and _POI_NOUNS had no "church", "synagogue", "library" or "firehouse" against
442 distinct building_type values. A miss swings _INTENT_WEIGHTS by 2.5x on the
buildings/venues split.

Membership is MEASURED, not curated. For each candidate token:

    ratio = (rows whose source column contains it, at a word boundary)
          / (rows whose embedded text contains it, at a word boundary)

A real style word ("italianate", "neo-grec") appears in the text almost only
because it is that row's style, so its ratio is near 1. A generic word that
merely happens to occur inside some style string ("the", "new", "classic",
"free", "later") appears everywhere else too, so its ratio collapses. That
rejects junk on evidence and needs no stoplist -- which would just be the same
hand-maintained word list one layer down.

Word boundaries matter: with plain LIKE substring matching, "art" matched
"apartment" and "war" matched "Warren", which wrongly rejected real style
tokens.

Run: python -m scripts.build_search_vocab [--min-ratio 0.5] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("vocab")

MIN_RATIO = 0.5
MIN_DF_SOURCE = 3   # a token seen on one or two rows is a typo, not vocabulary
MIN_LEN = 3

# (kind, SQL expression for the source column)
SOURCES = {
    "style": "coalesce(style_primary,'') || ' ' || coalesce(style_secondary,'')",
    "material": "coalesce(material,'')",
}

VOCAB_SQL = """
WITH cand AS (
    SELECT DISTINCT lower(tok) AS tok
      FROM building_search_index,
           LATERAL unnest(regexp_split_to_array({src}, '[^a-zA-Z-]+')) tok
     WHERE length(tok) >= {min_len}
), m AS (
    SELECT c.tok,
           (SELECT count(*) FROM building_search_index b
             WHERE lower({src_b}) ~ ('\\y' || c.tok || '\\y')) AS df_source,
           (SELECT count(*) FROM building_search_index b
             WHERE lower(b.text) ~ ('\\y' || c.tok || '\\y')) AS df_text
      FROM cand c
)
SELECT tok, df_source, df_text,
       (df_source::numeric / nullif(df_text, 0))::real AS ratio
  FROM m
 WHERE df_text > 0 AND df_source >= {min_df}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ratio", type=float, default=MIN_RATIO)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    rows_out: list[tuple] = []

    with conn.cursor() as cur:
        for kind, src in SOURCES.items():
            sql = VOCAB_SQL.format(
                src=src, src_b=src.replace("style_", "b.style_").replace("material", "b.material"),
                min_len=MIN_LEN, min_df=MIN_DF_SOURCE,
            )
            cur.execute(sql)
            got = cur.fetchall()
            kept = [r for r in got if r[3] is not None and r[3] >= args.min_ratio]
            log.info("%s: %d candidates -> %d kept (ratio >= %.2f)",
                     kind, len(got), len(kept), args.min_ratio)
            log.info("  sample: %s", ", ".join(r[0] for r in kept[:12]))
            rows_out += [(kind, t, ds, dt, rt) for t, ds, dt, rt in kept]

        # POI nouns come from the Supabase building_type column, which the
        # search index does not mirror; read it through the venues corpus's
        # category leaf plus the index snippet instead.
        # HEAD NOUN only -- the last token of the category leaf ("Cocktail
        # Bar" -> bar, "Coffee Shop" -> shop). Tokenizing the whole category
        # pulled in modifiers like "american", "african" and "academic", and a
        # POI classification is expensive: it drops the buildings corpus weight
        # from 1.0 to 0.4, so "american architecture" would have been routed to
        # venues.
        cur.execute("""
            SELECT head, count(*) FROM (
                SELECT lower((regexp_match(btrim(category), '([A-Za-z]+)\\s*$'))[1]) AS head
                  FROM venues
                 WHERE category IS NOT NULL AND btrim(category) <> ''
            ) h
             WHERE head IS NOT NULL AND length(head) >= 3
             GROUP BY 1 HAVING count(*) >= 20
        """)
        poi = cur.fetchall()
        log.info("poi: %d category tokens", len(poi))
        log.info("  sample: %s", ", ".join(t for t, _ in poi[:12]))
        rows_out += [("poi", t, c, c, 1.0) for t, c in poi]

        if args.dry_run:
            log.info("dry run: %d rows not written", len(rows_out))
            return 0

        cur.execute("DELETE FROM search_vocab")
        cur.executemany(
            "INSERT INTO search_vocab (kind, term, df_source, df_text, ratio)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (kind, term) DO NOTHING",
            rows_out,
        )
        conn.commit()
        cur.execute("SELECT kind, count(*) FROM search_vocab GROUP BY 1 ORDER BY 1")
        for k, n in cur.fetchall():
            log.info("wrote %s: %d terms", k, n)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

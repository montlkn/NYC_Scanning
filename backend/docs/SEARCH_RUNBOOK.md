# Search runbook

How `/api/search/unified` stays correct. Read this before importing venues,
changing the rewrite prompt, or tuning ranking.

## Importing venues

Run the ingest as normal:

```
python -m scripts.seed_venues --citywide --shards 8     # Foursquare
python -m scripts.ingest_overture_places                  # Overture
```

Both finish by calling `scripts/enrich_venues.py`, which:

- sets `in_nyc`, `neighborhood` and `borough` from NYC's NTA 2020 polygons
  (downloaded to `data/geo/` on first run)
- normalises Overture category slugs (`cocktail_bar` becomes `Cocktail Bar`)
- sets `searchable` (see "What search hides" below)
- adds the lot's PLUTO building class (`host_class`: church, theater, factory,
  store...) and fills a missing `building_year` from PLUTO, so "bar in a former
  church" can match
- rebuilds `lex_text` and `text`, then re-embeds only the rows whose text changed

**If enrichment was skipped or failed** (network drop, killed run), run it yourself.
It is resumable: rows already done are skipped.

```
python -m scripts.enrich_venues               # full pass
python -m scripts.enrich_venues --dry-run     # counts + sample texts, no writes
python -m scripts.enrich_venues --flags-only  # recompute `searchable` only, fast
```

Un-enriched rows are **visible** (`searchable IS NULL` counts as searchable), so
skipping enrichment puts New Jersey bars and address-named offices back in
results. `seed_venues` also overwrites `text`/`embedding` on every row it
upserts, which reverts that row's neighborhood and host-building text until
enrichment runs.

After a large import, check the result:

```sql
SELECT source, count(*), count(*) FILTER (WHERE searchable) AS searchable,
       count(*) FILTER (WHERE searchable IS NULL) AS unenriched
  FROM venues GROUP BY 1;
```

`unenriched` should be 0.

## What search hides

Nothing is deleted. `venues.searchable = false` hides a row when it:

- is outside NYC
- is FSQ `Structure` or `Neighborhood` (offices and address-named apartment buildings)
- has a name with no letters, a web domain, or a legal-entity suffix (LLC, Inc)
- is named for its own category ("Restaurant")
- is an errand category (the `DROP` list in `scripts/ingest_overture_places.py`)
- is pure B2B (its only FSQ domain is Business and Professional Services)

Change the rule in `is_searchable()` in `scripts/enrich_venues.py`, then run
`--flags-only`.

Lore rows outside NYC are hidden the same way through
`layer_search_index.in_nyc`. Re-run `python -m scripts.flag_layers_in_nyc`
after re-ingesting layers.

## LLM query rewrites

Vague queries ("spooky spots") are rewritten by gpt-5.6-luna into phrases the
corpus uses. The rewrites are cached per query in `search_interpretation_cache`.

- **The cache is permanent.** Each distinct query pays the ~2.5s model wait
  once. After that it is served from the cache, and a query that needed its
  rewrite runs it in parallel with the first pass.
- **Changing the prompt**: bump `INTERP_VERSION` in `services/unified_search.py`.
  Old rows are then ignored.
- **Warm-up is automatic.** Sixty seconds after each deploy the backend re-fills
  rewrites for the 200 most-searched queries that lack a current-version row
  (see `_warm_search_rewrites` in `main.py`). No cron is needed. To force it by
  hand: `python -m scripts.warm_search_rewrites`.

## How results are ordered

On top of the fused relevance score, every hit gets a tier
(`order_by_tier` / `tier_of` in `services/unified_search.py`):

| tier | what | order |
|---|---|---|
| 0 | **the actual thing**: every distinctive word of a building or venue name (or, when the query also asks for a kind of place, its host building's name) is in the query. "seagram bar" -> The Bar | relevance, wherever it is |
| 1 | **a real match**: covers every query word in its name, category, style, neighborhood, host building or (multi-word queries only) its report sentence; or the rewrite's category + style/era | **nearest first** |
| 2 | everything else | relevance |

- A word counts as a *name* only if the designation reports write it
  capitalized >= 80% of the time (Chrysler 119/119, haunted 1/8), checked per
  word and cached. Words the reports never use are presumed names.
- With the named thing found, other hits stay tier 1 only if they carry the
  name AND the query asked for a kind of place ("bars near grand central").
- Fewer than 3 real matches inside the near-me radius re-runs the query
  citywide. `area=true` ("search this area") never widens.

## Checking a change: the eval set

```
python -m scripts.search_eval                          # production
python -m scripts.search_eval --base http://localhost:8011
```

`tests/search_eval_cases.json` holds the queries that were once wrong and the
property that makes each right. Exit code is the number of failures. Run it
before and after any ranking change. Add a case whenever a bug is fixed.

## Wikipedia

`python -m scripts.ingest_wikipedia_geo` harvests NYC's geotagged Wikipedia
articles (about 7.8k) into `layer_search_index` with `layer = 'wiki'`. Re-runs
only re-embed changed extracts, so monthly is plenty. Hits come back as
`type: "wiki"` with `url` and `summary`; the app opens them in the Wikipedia
layer's own sheet.

## Debugging a bad result

Add `debug=true` to any `/api/search/unified` call. Every hit gets `_debug`
(the score parts and which legs found it) and the response gets `_timing`
(first pass, LLM wait, expansion, and whether the rewrite ran).
Profile against production. From a laptop every leg crosses the WAN to
Railway and looks 2-5x slower than it is.

## Database settings this depends on

- `hnsw.iterative_scan = relaxed_order` on the `railway` database. Without it a
  vector query with a radius or `searchable` filter sees only 40 candidates
  before filtering; a 1.5km radius returned 6 of 80.
- Trigram GIN index `idx_venues_lex_text_trgm` on `venues.lex_text`.
- DDL on `venues` (292k rows) needs `SET lock_timeout = '3s'` and
  `CREATE INDEX CONCURRENTLY`. A blocking ALTER took search down on 2026-09-21.

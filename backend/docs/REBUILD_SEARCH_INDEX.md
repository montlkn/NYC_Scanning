# Rebuilding the search index from scratch

The pgvector database holds only derived data. Every row can be rebuilt from
Supabase (BUILDINGS, MAIN), PostGIS (`landmark_chunks`, footprints) and public
sources. This is the order that rebuilt it after the volume was lost on
2026-09-30. It takes about four hours, mostly the venue embeddings.

A restore from a dump takes minutes. Keep one (see "Backups" below).

## Before you start

- Everything runs inside the `NYC_Scanning` container (`railway ssh --service
  NYC_Scanning`). `pgvector.railway.internal` only resolves inside Railway.
- Writes need the owner login. Use `SEARCH_DB_WRITE_URL` from `index-cron`'s
  variables and export it as both `SEARCH_DB_URL` and `SEARCH_DB_WRITE_URL` for
  the session. Never put it on the API service. Unset it when done.
- Lore, plaques and contributions read MAIN: export `MAIN_DB_URL` (the
  `DATABASE_URL` on `index-cron`). The API's `DATABASE_URL` is BUILDINGS, which
  `embed_layers.py` needs as `BUILDINGS_DB_URL`.
- The API image lacks two tools the venue scripts need. Install them in the
  running container. They are gone after the next deploy:
  `apt-get update && apt-get install -y curl` and `pip install duckdb`.
- `HF_TOKEN` (Hugging Face read token, license-gated FSQ dataset) is not a
  Railway variable. Pass it to the `seed_venues` command only.
- Do not redeploy `NYC_Scanning` while a job runs. A redeploy kills
  background jobs and wipes `/tmp`. Adding a variable redeploys too.
- Run long jobs with `setsid nohup ... > /tmp/x.log 2>&1 < /dev/null &` so they
  survive the ssh session closing.
- `railway ssh -- <command>` needs `< /dev/null` and `psql -P pager=off`, or it
  waits forever on the pager.

## 1. Schema and database settings (owner, psql on pgvector)

Apply `migrations/*.sql` in filename order (skip the `_APPLIED` ones, which
belong to PostGIS), create the `jink_search_app` read-only role, then:

```
psql "$SEARCH_DB_WRITE_URL" -f migrations/20261001_search_db_settings.sql
```

This sets `hnsw.iterative_scan`, the trigram index,
`layer_search_index.in_nyc`, `venues.source`, and the API role's write grants
on `search_query_log` and `search_interpretation_cache`. No other migration has them. Without `in_nyc`,
`ingest_wikipedia_geo` fails at its final write, after about 9 minutes of
fetching.

## 2. Buildings

```
python scripts/embed_buildings.py                 # ~35,374 rows, ~20 min
python3 scripts/backfill_fame.py
python -m scripts.backfill_index_neighborhood
python -m scripts.backfill_index_aesthetic
python -m scripts.backfill_index_material
python -m scripts.backfill_index_name_norm
python -m scripts.backfill_lpc_attribution
python -m scripts.backfill_material_from_lpc
python -m scripts.build_search_vocab               # again after step 4
```

All but `backfill_fame` take `--dry-run`. About 4 minutes in total.

## 3. Lore and layers

These are independent of each other and can run in parallel.

```
python -m scripts.embed_building_lore             # ~68.8k rows, ~30 min
python -m scripts.ingest_wikipedia_geo            # ~7.8k articles
python3 scripts/embed_layers.py                   # lore, plaques, contributions, places
python3 -m scripts.embed_grok_narratives          # ~398 narratives
python -m scripts.flag_layers_in_nyc              # after the two above and wiki
```

`embed_layers` and `embed_grok_narratives` are what `index-cron` runs. A
redeploy of a cron service does not run it; it waits for the schedule. Run
them by hand.

## 4. Venues

```
python -m scripts.seed_venues --citywide --shards 100   # ~226k FSQ rows, needs HF_TOKEN
python -m scripts.ingest_overture_places                # ~73k Overture rows
python -m scripts.fix_overture_rows                     # contacts, BIN join, and DELETEs (see below)
python -m scripts.enrich_venues                         # re-embeds rows the fix changed
python -m scripts.build_search_vocab
```

- `fix_overture_rows` without `--contacts-only` deletes Overture rows outside
  NYC (~19.3k) and Overture rows that duplicate an FSQ row (~600).
  `searchable` already hides the first group. The 2026-09-30 rebuild ran
  `--contacts-only`, pending a decision on the deletes.
- `--shards 100`, not 8. The FSQ release (dt=2024-12-03) has 100 shards; 8
  yields ~18k venues.
- Seed and Overture both call `enrich_venues` at the end. Check `unenriched`
  is 0 with the SQL in SEARCH_RUNBOOK.md.
- FSQ ids are stable (pinned release), so `cards/venue_cards.jsonl` and
  `cards/venue_notes.jsonl` match again by themselves.
- Overture ids were built from Python's salted `hash()` until 2026-10-01, so
  the 2026-09-30 re-ingest got new ids. The carded Overture rows were renamed
  back to their old ids by exact name within 15m. Ids are now sha1-based and
  stable across runs.

## 5. Finish

1. Redeploy `NYC_Scanning`. It reloads the cards and opens new sessions with
   the database settings.
2. Check the counts below, then the queries: `oculus`, `clandestinos`,
   `seagram bar`, `modernist bars in midtown`, `baroque vibe`.
3. `python -m scripts.search_eval`. Its exit code is the number of failures.
4. Take a backup.

## Counts after the 2026-09-30 rebuild

| table | rows |
|---|---|
| building_search_index | 35,373 |
| building_lore_index | 69,238 (68,840 LPC chunks + 398 Kit narratives) |
| layer_search_index | 9,755 (7,767 wiki, 1,803 lore, 105 plaques, 55 places, 25 contributions) |
| venues | 285,025 (225,810 FSQ + 59,215 Overture), `unenriched` 0 |
| search_vocab | 353 |

All 577 venue cards and 2 editor notes matched a venue. `search_eval`: 33/34
(`bars id like` ranks a furniture store in its top 3).

## Backups

The pgvector volume has no Railway backups on the current plan. Dump it after
any rebuild or large ingest:

```
railway ssh --service pgvector
pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/index_backup.pgdump
```

Copy it off the container (base64 over `railway ssh`, since the ssh session is a
PTY and corrupts raw binary), compare `sha256sum` on both ends, check it with
`pg_restore -l`, and delete the copy in the container. Restore:

```
pg_restore -U "$POSTGRES_USER" -d railway --clean --if-exists index_backup.pgdump
```

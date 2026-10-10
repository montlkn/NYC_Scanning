# Search index jobs

The search index (`layer_search_index` and friends) lives in the Railway
`pgvector` Postgres. The API reads it as `jink_search_app`, which is
read-only on purpose. Anything that WRITES the index needs the owner login
(pgvector's `POSTGRES_USER` / `POSTGRES_PASSWORD`), passed as
`SEARCH_DB_WRITE_URL`. Never put that variable on the API service.

## What needs re-indexing, and when

| Source | Script | When |
|---|---|---|
| Places (BUILDINGS `places`) | `scripts/embed_layers.py --layer place` | after adding places |
| Lore, plaques, contributions (MAIN) | `scripts/embed_layers.py` | as they grow |
| Kit narratives (MAIN `narratives`) | `scripts.embed_grok_narratives` | as they grow |
| Buildings whose generated lore changed | `scripts/embed_buildings.py --changed` | as lore is written |

All of them embed only rows not yet indexed. `cron/reindex_search.sh` runs
them all.

## Automatic (recommended): a Railway cron service

1. In the Railway project, **New → GitHub Repo → NYC_Scanning** (a second
   service from the same repo). Name it `index-cron`.
2. Settings → **Root Directory**: same as the API service's.
3. Settings → **Custom Start Command**: `bash cron/reindex_search.sh`
4. Settings → **Cron Schedule**: `15 9 * * *` (daily, 09:15 UTC).
5. Variables: reference the API's (`DATABASE_URL`, `SUPABASE_URL`,
   `SUPABASE_KEY`, `SUPABASE_SERVICE_KEY`, `SEARCH_DB_URL`) and add
   `SEARCH_DB_WRITE_URL` =
   `postgresql://${{pgvector.POSTGRES_USER}}:${{pgvector.POSTGRES_PASSWORD}}@pgvector.railway.internal:5432/${{pgvector.POSTGRES_DB}}`
   (URL-encode the password if it has `@ : / # ?`).
   Lore/plaques/contributions read MAIN's Postgres: add `MAIN_DB_URL` too,
   or `embed_layers.py` reads `DATABASE_URL` (BUILDINGS) for them and finds
   no such tables.

A cron service runs the command and exits; it is not a web server.

## By hand

```bash
railway ssh --service NYC_Scanning
read -r -p "POSTGRES_USER: " PGU
read -r -s -p "POSTGRES_PASSWORD: " PGP; echo
PGP_ENC=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PGP")
SEARCH_DB_WRITE_URL="postgresql://$PGU:$PGP_ENC@pgvector.railway.internal:5432/railway" \
  python scripts/embed_layers.py --layer place
unset PGU PGP PGP_ENC
```

Run inside the container (bash): `pgvector.railway.internal` only resolves
inside Railway, and the Mac's zsh `read` takes different flags.

## Kit voice jobs (run by hand, in this order)

Two jobs write in Kit's voice. Both read and write MAIN, so they need
`MAIN_DB_URL` (the owner login) and `OPENAI_API_KEY`. Apply
`20261009_narratives_rename_MAIN.sql` and `20261007_building_hooks_MAIN.sql`
(in the Jink_Swift repo, `supabase/migrations/`) first.

```bash
railway ssh --service NYC_Scanning
# 1. Look first: calls the model, prints before/after, writes nothing.
python -m scripts.rewrite_narratives_voice --dry-run --limit 5
# 2. Rewrite every story in place. Backs up to narratives_voice_backup first,
#    keeps the SOURCES/FACTS tail, skips any rewrite that adds a fact.
python -m scripts.rewrite_narratives_voice
# 3. One-line hooks into MAIN.building_hooks (needs FOOTPRINTS_DB_URL for LPC).
python -m scripts.generate_building_hooks --dry-run --limit 10
python -m scripts.generate_building_hooks
# Undo step 2:
python -m scripts.rewrite_narratives_voice --restore
```

After step 2, refresh search. `embed_grok_narratives` only ADDS rows (the key
is bin + text hash), so the old-voice text would stay indexed beside the new.
Clear Kit's rows first, then re-embed (about 400 rows, a minute):

```sql
-- on the pgvector DB, as the owner login (SEARCH_DB_WRITE_URL)
DELETE FROM building_lore_index WHERE source = 'kit';
```
```bash
SEARCH_DB_URL="$SEARCH_DB_WRITE_URL" python -m scripts.embed_grok_narratives
```

Every rewrite and hook also goes through a second model call
(`services/kit_judge.py`) that acts as a hostile fact checker: it compares the
candidate to its source and rejects any claim the source does not support
(changed timing, changed wording, invented asides). A rejected candidate keeps
the old story or writes no hook; the log says why. `--no-judge` skips it, which
is not recommended.

Both jobs are resumable and cost cents (rewriting supplied text, no web search;
the fact check roughly doubles the model calls).
`services/kit_voice.py` holds the voice and the checks; its voice text is a
copy of `KitAIService.kitVoice` in the Jink_Swift app. The script name
`embed_grok_narratives` is unchanged.

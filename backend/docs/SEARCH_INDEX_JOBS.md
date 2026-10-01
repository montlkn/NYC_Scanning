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
| Kit narratives (MAIN `grok_narratives`) | `scripts.embed_grok_narratives` | as they grow |
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

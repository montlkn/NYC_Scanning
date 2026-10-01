#!/usr/bin/env bash
# Keep the search index current: new places, lore, plaques and contributions
# (embed_layers, only rows not yet indexed) and Kit's new narratives.
# Each step embeds only what is new, so a quiet run costs a few queries.
#
# Runs as its own Railway service (cron), never on the API: it needs
# SEARCH_DB_WRITE_URL, the index owner's login, and the API must stay on the
# read-only jink_search_app user. Setup: docs/SEARCH_INDEX_JOBS.md.
set -uo pipefail
cd "$(dirname "$0")/.."
: "${SEARCH_DB_WRITE_URL:?set SEARCH_DB_WRITE_URL (index owner) on the cron service}"

status=0
python3 scripts/embed_layers.py || status=1
python3 -m scripts.embed_grok_narratives || status=1
# Buildings whose generated lore changed. The API can't re-index them itself:
# it reads the index as the read-only jink_search_app. embed_buildings reads
# BUILDINGS through DATABASE_URL, which on this service is MAIN.
DATABASE_URL="${BUILDINGS_DB_URL:-$DATABASE_URL}" SEARCH_DB_URL="$SEARCH_DB_WRITE_URL" \
  python3 scripts/embed_buildings.py --changed || status=1
exit $status

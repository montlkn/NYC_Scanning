#!/usr/bin/env bash
# Re-index Kit's cached narratives into the lore corpus.
#
# grok_narratives grows every time the app generates a narrative, so this is
# not a one-off backfill -- without it the newest and most interesting prose
# in the product is the only prose search cannot see.
#
# The script embeds ONLY what is new (keyed on bin + text hash), so a run with
# nothing to do costs one query and exits. Safe to run often; daily is plenty.
#
#   0 4 * * *  /app/cron/reindex_narratives.sh >> /var/log/reindex.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m scripts.embed_grok_narratives

"""
Rewrite the stored building stories in Kit's voice, in place.

Why in place and not clear-and-regenerate: clearing leaves a gap where the app
shows "no story" and pays for a cold generation per building; a rewrite keeps
every story on screen until its replacement is ready, costs a fraction of a
cent each (rewriting supplied text, no search), and keeps the facts that were
already grounded.

Safety, in this order:
  1. Every story is copied to narratives_voice_backup before it is touched.
  2. The SOURCES/FACTS tail the app parses is preserved verbatim.
  3. A rewrite is written only if it passes services.kit_voice.check_rewrite
     (no new numbers or proper nouns, sane length, clean ending, no rat puns).
     A rejected rewrite leaves the old story as it was.
  4. Each row commits alone and sets voice_version, so a stopped run resumes.
  5. --restore puts every backed-up story back.

Run on Railway (or anywhere with the env below):
    python -m scripts.rewrite_narratives_voice --dry-run --limit 5   # look first
    python -m scripts.rewrite_narratives_voice                       # all
    python -m scripts.rewrite_narratives_voice --bin 1015862         # one
    python -m scripts.rewrite_narratives_voice --restore             # undo

Env: MAIN_DB_URL (MAIN Postgres, owner login), OPENAI_API_KEY.
Needs supabase/migrations/20261009_narratives_rename_MAIN.sql applied first.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from services.kit_voice import (  # noqa: E402
    REWRITE_SYSTEM, VOICE_VERSION, check_rewrite, split_tail,
)
from services.kit_judge import judge  # noqa: E402
from services.openai_text import is_configured, openai_text  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rewrite_narratives")

BACKUP_DDL = """
CREATE TABLE IF NOT EXISTS public.narratives_voice_backup (
  bin text PRIMARY KEY,
  narrative text NOT NULL,
  backed_up_at timestamptz NOT NULL DEFAULT now()
)"""


def restore(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(BACKUP_DDL)
        cur.execute("""
            UPDATE public.narratives n
               SET narrative = b.narrative, voice_version = 0
              FROM public.narratives_voice_backup b
             WHERE n.bin = b.bin""")
        log.info("restored %d stories from backup", cur.rowcount)
    conn.commit()


async def rewrite_one(sem: asyncio.Semaphore, bin_: str, narrative: str, use_judge: bool = True):
    prose, tail = split_tail(narrative)
    async with sem:
        new = await openai_text(
            system=REWRITE_SYSTEM,
            user=prose,
            max_tokens=1400,
            timeout_s=90.0,
            cache_key="jink-voice-rewrite",
        )
    reason = check_rewrite(prose, new)
    if reason:
        return bin_, None, reason
    if use_judge:
        async with sem:
            reason = await judge(prose, new.strip(), label="rewritten story")
        if reason:
            return bin_, None, reason
    return bin_, new.strip() + tail, None


async def main_async(args) -> int:
    url = os.environ.get("MAIN_DB_URL")
    if not url:
        log.error("MAIN_DB_URL is not set")
        return 1
    conn = psycopg.connect(url)

    if args.restore:
        restore(conn)
        return 0
    if not is_configured():
        log.error("OPENAI_API_KEY is not set")
        return 1

    with conn.cursor() as cur:
        cur.execute(BACKUP_DDL)
        where, params = "voice_version < %s AND length(narrative) >= 90", [VOICE_VERSION]
        if args.bin:
            where += " AND bin = %s"
            params.append(args.bin)
        sql = f"SELECT bin, narrative FROM public.narratives WHERE {where} ORDER BY generated_at DESC"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    log.info("%d stories to rewrite (voice_version < %d)", len(rows), VOICE_VERSION)

    sem = asyncio.Semaphore(args.concurrency)
    ok = rejected = 0
    reasons: dict[str, int] = {}
    step = args.concurrency * 4
    for i in range(0, len(rows), step):
        batch = rows[i:i + step]
        results = await asyncio.gather(*(rewrite_one(sem, b, n, not args.no_judge) for b, n in batch))
        old_by_bin = dict(batch)
        for bin_, new, reason in results:
            if reason:
                rejected += 1
                key = reason.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
                log.warning("  [%s] kept old story: %s", bin_, reason)
                continue
            if args.dry_run:
                log.info("  [%s] BEFORE: %s", bin_, split_tail(old_by_bin[bin_])[0][:700].replace("\n", " "))
                log.info("  [%s] AFTER:  %s", bin_, split_tail(new)[0][:700].replace("\n", " "))
                ok += 1
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.narratives_voice_backup (bin, narrative) VALUES (%s, %s) "
                    "ON CONFLICT (bin) DO NOTHING", (bin_, old_by_bin[bin_]))
                cur.execute(
                    "UPDATE public.narratives SET narrative = %s, voice_version = %s WHERE bin = %s",
                    (new, VOICE_VERSION, bin_))
            conn.commit()
            ok += 1
        log.info("progress %d/%d  written=%d kept_old=%d", min(i + step, len(rows)), len(rows), ok, rejected)

    log.info("done: %d %s, %d kept old %s", ok, "previewed" if args.dry_run else "rewritten", rejected, reasons or "")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="call the model and print before/after; write nothing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--bin", default=None, help="rewrite just this BIN")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--restore", action="store_true", help="put backed-up stories back")
    ap.add_argument("--no-judge", action="store_true", help="skip the fact-check pass (not recommended)")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Ingest per-building entries from MODERN-format LPC district reports into
`landmark_chunks`, so tier-1 lore stops serving one district blurb to every
building in the neighbourhood.

Scope, and why it is deliberately partial
─────────────────────────────────────────
The 94-report survey measured what each format actually yields:

    modern : 51 reports  11,705/14,904 bins  79%   (33 reports >=90%)
    legacy : 43 reports   1,386/15,469 bins   9%   (41 of 43 yield ZERO)

So this ingests the modern half only. Legacy reports are a different document
shape — narrative prose by street block with no per-building markers, buildings
named inline ("No. 158, by unusually steep slate...") — and need their own
strategy. Ingesting them on the current parser would write almost nothing while
looking like it worked.

Safety properties
─────────────────
* ADDITIVE. District rows are never deleted; they remain the fallback for the
  ~17k buildings still uncovered. A building-specific row is added alongside.
* IDEMPOTENT. Re-running deletes only rows this script previously wrote for the
  same source_file (specificity='building') before re-inserting.
* KEYED BY BIN, NOT BBL. A BBL can carry several BINs (1,801 of 32,720 do), and
  the same address can too, so one report entry legitimately fans out to every
  BIN on its lot. BBL is stored for provenance but BIN is the retrieval key.
* DRY RUN BY DEFAULT. `--commit` is required to write.

Usage:
  python scripts/ingest_district_entries.py --survey survey.jsonl            # dry run
  python scripts/ingest_district_entries.py --survey survey.jsonl --limit 2
  python scripts/ingest_district_entries.py --survey survey.jsonl --commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2  # noqa: E402
from psycopg2.extras import execute_batch  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from extract_district_entries import extract_modern, looks_modern  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

UA = "JinkLandmarkIngest/2.0 (contact@jinkapp.co)"
MIN_CHARS = 200
CHUNK_MAX = 1500  # matches the existing corpus (max observed 1500)


def ensure_schema(rail_url: str, commit: bool) -> None:
    """Add the `specificity` marker if absent. Additive and nullable, so every
    existing row keeps working and NULL simply means 'district-level'."""
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='landmark_chunks' AND column_name='specificity'
    """)
    if cur.fetchone():
        print("schema: specificity column present")
    elif commit:
        cur.execute("ALTER TABLE landmark_chunks ADD COLUMN specificity text")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_landmark_chunks_bin_spec "
                    "ON landmark_chunks (bin, specificity)")
        conn.commit()
        print("schema: added specificity column + (bin, specificity) index")
    else:
        print("schema: WOULD add specificity column + index")
    cur.close()
    conn.close()


def district_borough(rail_url: str, source_file: str) -> str | None:
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT left(bin,1) b, count(*) n FROM landmark_chunks
        WHERE source_file=%s AND bin IS NOT NULL AND bin<>'' GROUP BY 1
        ORDER BY n DESC LIMIT 1
    """, (source_file,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row and row[0] in "12345" else None


def district_name(rail_url: str, source_file: str) -> str:
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT building_name FROM landmark_chunks
        WHERE source_file=%s AND building_name IS NOT NULL LIMIT 1
    """, (source_file,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else ""


def bbl_to_bins(buildings_url: str, bbls: list[str]) -> dict[str, list[str]]:
    """BBL -> every BIN on it. Both sides stripped of the '.0' the DB stores."""
    if not bbls:
        return {}
    conn = psycopg2.connect(buildings_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT replace(bbl,'.0','') AS b, replace(bin,'.0','') AS i, address
        FROM buildings_full_merge_scanning
        WHERE replace(bbl,'.0','') = ANY(%s) AND bin IS NOT NULL
    """, (bbls,))
    out: dict[str, list[str]] = {}
    addr: dict[str, str] = {}
    for b, i, a in cur.fetchall():
        out.setdefault(b, []).append(i)
        addr.setdefault(i, a or "")
    cur.close()
    conn.close()
    bbl_to_bins.addresses = addr  # type: ignore[attr-defined]
    return out


def chunkify(text: str, size: int = CHUNK_MAX) -> list[str]:
    """Split on whitespace boundaries so a chunk never ends mid-word."""
    words = text.split()
    chunks, cur_, n = [], [], 0
    for w in words:
        if n + len(w) + 1 > size and cur_:
            chunks.append(" ".join(cur_))
            cur_, n = [], 0
        cur_.append(w)
        n += len(w) + 1
    if cur_:
        chunks.append(" ".join(cur_))
    return chunks


def download(url: str, dest: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as fh:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                fh.write(buf)
        return True
    except Exception as exc:
        print(f"  download failed: {exc}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--survey", required=True, help="survey.jsonl from the survey run")
    ap.add_argument("--tmp", default="/tmp/jink_ingest.pdf")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--commit", action="store_true", help="actually write")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    rail = os.environ.get("FOOTPRINTS_DB_URL")
    bldg = os.environ.get("DATABASE_URL")
    if not rail or not bldg:
        print("FOOTPRINTS_DB_URL and DATABASE_URL required", file=sys.stderr)
        return 2

    recs = [json.loads(l) for l in open(args.survey) if l.strip()]
    targets = [r for r in recs
               if r.get("format") == "modern" and r.get("matched", 0) > 0]
    targets.sort(key=lambda r: -r["matched"])
    if args.limit:
        targets = targets[: args.limit]

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"[{mode}] {len(targets)} modern reports to ingest\n")
    ensure_schema(rail, args.commit)

    tot_rows = tot_bins = 0
    for i, rec in enumerate(targets, 1):
        src = rec["source_file"]
        name = rec["report"]
        print(f"[{i}/{len(targets)}] {name}", flush=True)
        if not download(src, args.tmp):
            continue
        try:
            if not looks_modern(args.tmp):
                print("  skipped: no longer detects as modern")
                continue
            boro = district_borough(rail, src)
            dname = district_name(rail, src)
            ents = [e for e in extract_modern(args.tmp, name, default_borough=boro)
                    if len(e.text) >= MIN_CHARS]
            bbls = sorted({e.bbl for e in ents if e.bbl})
            mapping = bbl_to_bins(bldg, bbls)
            addresses = getattr(bbl_to_bins, "addresses", {})

            rows = []
            bins_hit = set()
            for e in ents:
                for b in mapping.get(e.bbl, []):
                    bins_hit.add(b)
                    for idx, ck in enumerate(chunkify(e.text)):
                        rows.append((dname, b, e.bbl,
                                     addresses.get(b) or e.address,
                                     ck, idx, src, e.page, "building"))
            print(f"  entries={len(ents)} bins={len(bins_hit)} rows={len(rows)}")
            tot_rows += len(rows)
            tot_bins += len(bins_hit)

            if args.commit and rows:
                conn = psycopg2.connect(rail)
                cur = conn.cursor()
                # Idempotent: drop only what this script wrote for this report.
                cur.execute("DELETE FROM landmark_chunks "
                            "WHERE source_file=%s AND specificity='building'", (src,))
                execute_batch(cur, """
                    INSERT INTO landmark_chunks
                      (building_name, bin, bbl, address, chunk_text,
                       chunk_index, source_file, page_number, specificity)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, rows, page_size=500)
                conn.commit()
                cur.close()
                conn.close()
                print("  committed")
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}"[:200], flush=True)
        finally:
            if os.path.exists(args.tmp):
                os.remove(args.tmp)
        time.sleep(args.sleep)

    print(f"\n[{mode}] totals: {tot_rows} rows across {tot_bins} BIN-report pairs")
    if not args.commit:
        print("dry run only — nothing written. re-run with --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

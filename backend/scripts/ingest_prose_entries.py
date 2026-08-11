#!/usr/bin/env python3
"""
Ingest per-building entries from PROSE-format LPC district reports.

Complements `ingest_district_entries.py`, which handles the modern Block/Lot
reports and resolves buildings by BBL. Prose reports have no Block/Lot lines at
all — they name buildings by house number inside running narrative — so the
resolution path is house number + normalised street against the district's own
`landmark_chunks` addresses.

Survey measured 8,210/15,469 bins (53%) reachable this way, against 9% before
the prose parser existed.

Two deliberate restrictions
───────────────────────────
1. Only `head` and `lead` confidence tiers are ingested. `inline` means the
   building was named in a LATER sentence, which is often a comparison to a
   neighbour rather than a description of the building — the parser's own notes
   record it attributing text to "No. 263" from the phrase "(Nos. 261, 263...
   have been omitted from the street numbering)", a building that does not
   exist. head+lead is ~85% of matches and is where the confident attributions
   live.
2. Text comes from `text_parts` only, never the `text` property — that property
   concatenates `inline_parts`, which would smuggle the excluded tier back in
   through the entry it is attached to.

Safety, matching the modern ingest: additive (district rows are never deleted),
idempotent (re-running clears only rows this script wrote for the same report),
keyed by BIN, and dry-run unless --commit.

Usage:
  python scripts/ingest_prose_entries.py --survey survey.jsonl
  python scripts/ingest_prose_entries.py --survey survey.jsonl --limit 2
  python scripts/ingest_prose_entries.py --survey survey.jsonl --commit
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2  # noqa: E402
from psycopg2.extras import execute_batch  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from extract_district_entries import (  # noqa: E402
    detect_format, extract, extract_prose, normalize_street,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

UA = "JinkLandmarkIngest/2.1 (contact@jinkapp.co)"
MIN_CHARS = 200
CHUNK_MAX = 1500
ACCEPTED_TIERS = ("head", "lead")

ABBR = {"PLCE": "PLACE", "PL": "PLACE", "AVE": "AVENUE", "AV": "AVENUE",
        "SQ": "SQUARE", "RD": "ROAD", "TER": "TERRACE",
        "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST", "SO": "SOUTH"}


def norm_db_address(addr: str) -> tuple[int | None, str | None]:
    """'199 WEST 10 STREET' -> (199, 'WEST 10 STREET').

    Note ST is NOT expanded: blanket ST->STREET turns ST JOHN'S PLACE into
    STREET JOHN'S PLACE and loses the whole street.
    """
    a = re.sub(r"\s+", " ", (addr or "").upper()).strip()
    m = re.match(r"^(\d+)\s+(.*)$", a)
    if not m:
        return None, None
    toks = [ABBR.get(t, t) for t in m.group(2).split()]
    return int(m.group(1)), normalize_street(" ".join(toks))


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def build_street_resolver(streets: list[str]):
    """OCR-tolerant street folding, guarded on digits.

    EAST 68 STREET and EAST 78 STREET are 0.93 similar; folding them collapses a
    numbered grid onto one street and mis-files every building on it. So a fold
    is only allowed when the digits match exactly.
    """
    cache: dict[str, str] = {}

    def resolve(s: str) -> str:
        if s in cache:
            return cache[s]
        if s in streets:
            cache[s] = s
            return s
        out = s
        for cand in difflib.get_close_matches(s, streets, n=3, cutoff=0.86):
            if _digits(cand) == _digits(s):
                out = cand
                break
        cache[s] = out
        return out

    return resolve


def district_addresses(rail_url: str, source_file: str) -> list[tuple[str, str]]:
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT bin, address FROM landmark_chunks
        WHERE source_file = %s AND address IS NOT NULL AND bin IS NOT NULL
    """, (source_file,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


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


def chunkify(text: str, size: int = CHUNK_MAX) -> list[str]:
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
    ap.add_argument("--survey", required=True)
    ap.add_argument("--tmp", default="/tmp/jink_prose_ingest.pdf")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    rail = os.environ.get("FOOTPRINTS_DB_URL")
    if not rail:
        print("FOOTPRINTS_DB_URL required", file=sys.stderr)
        return 2

    recs = [json.loads(l) for l in open(args.survey) if l.strip()]
    # The survey ran before the prose parser existed, so these are the reports
    # it labelled 'legacy'. detect_format re-checks each PDF below.
    targets = [r for r in recs if r.get("format") == "legacy" and not r.get("error")]
    targets.sort(key=lambda r: -r["bins_in_district"])
    if args.limit:
        targets = targets[: args.limit]

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"[{mode}] {len(targets)} candidate reports\n")

    tot_rows = tot_bins = 0
    for i, rec in enumerate(targets, 1):
        src, name = rec["source_file"], rec["report"]
        print(f"[{i}/{len(targets)}] {name} ({rec['bins_in_district']} bins)", flush=True)
        if not download(src, args.tmp):
            continue
        try:
            fmt = detect_format(args.tmp)
            if fmt == "prose":
                entries = [
                    e for e in extract_prose(args.tmp, name)
                    if getattr(e, "kind", "building") == "building"
                    and getattr(e, "confidence", "inline") in ACCEPTED_TIERS
                    and len(" ".join(e.text_parts)) >= MIN_CHARS
                ]
            elif fmt == "legacy":
                # Marginal-marker reports (LP-0489 Greenwich Village and kin)
                # resolve by the SAME house-number + street key as prose, so they
                # belong here rather than in the BBL-keyed modern ingest — which
                # skips them, leaving ~1,361 Greenwich Village buildings with no
                # ingest path at all. Their attribution comes from a marker in
                # the margin, not from sentence position, so there is no
                # confidence tier to filter on.
                entries = [
                    e for e in extract(args.tmp, name)
                    if len(" ".join(e.text_parts)) >= MIN_CHARS
                ]
            else:
                print(f"  skipped: detected {fmt} (BBL-keyed ingest handles it)")
                continue
            rows_db = district_addresses(rail, src)
            streets = sorted({e.street for e in entries})
            resolve = build_street_resolver(streets)

            # (number, street) -> entry
            index: dict[tuple[int, str], object] = {}
            for e in entries:
                for n in e.numbers:
                    index.setdefault((n, e.street), e)

            dname = district_name(rail, src)
            rows = []
            bins_hit = set()
            for bin_, addr in rows_db:
                n, s = norm_db_address(addr)
                if n is None:
                    continue
                e = index.get((n, resolve(s)))
                if e is None:
                    continue
                body = re.sub(r"\s+", " ", " ".join(e.text_parts)).strip()
                if len(body) < MIN_CHARS:
                    continue
                bins_hit.add(bin_)
                for idx, ck in enumerate(chunkify(body)):
                    rows.append((dname, bin_.replace(".0", ""), None, addr, ck,
                                 idx, src, (e.pages or [0])[0], "building"))

            print(f"  entries={len(entries)} bins={len(bins_hit)} rows={len(rows)}")
            tot_rows += len(rows)
            tot_bins += len(bins_hit)

            if args.commit and rows:
                conn = psycopg2.connect(rail)
                cur = conn.cursor()
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

    print(f"\n[{mode}] totals: {tot_rows} rows across {tot_bins} bins")
    if not args.commit:
        print("dry run only — nothing written. re-run with --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

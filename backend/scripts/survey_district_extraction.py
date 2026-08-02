#!/usr/bin/env python3
"""
Survey what address-keyed extraction would actually yield, across every LPC
district report — WITHOUT writing anything.

Why read-only: the ingest mutates `landmark_chunks`, which the live scan path
reads. Extrapolating that from two hand-checked reports would be a guess, and
the legacy parser silently returned ZERO entries on the modern format until it
was tested — exactly the failure that a 2-report extrapolation hides. So this
measures all 94 first, and the write is gated on the number it produces.

For each district report:
  * download the PDF (deleted immediately after — one on disk at a time; these
    run to 499MB and the full set is 10-20GB)
  * auto-detect legacy vs modern layout
  * extract entries and match them to buildings
      - modern: exact BBL against buildings_full_merge_scanning
      - legacy: house-number + normalised street against the district's own
        landmark_chunks addresses, with OCR-tolerant street resolution
  * append one JSON line of results

Resumable: reports already present in the output file are skipped, so an
interrupted run continues where it stopped.

Env (from backend/.env):
  FOOTPRINTS_DB_URL  Railway — landmark_chunks (district membership, addresses)
  DATABASE_URL       BUILDINGS Supabase — BBL/BIN truth for modern reports

Usage:
  python scripts/survey_district_extraction.py --out survey.jsonl
  python scripts/survey_district_extraction.py --out survey.jsonl --limit 5
  python scripts/survey_district_extraction.py --out survey.jsonl --summary-only
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
from dotenv import load_dotenv  # noqa: E402

from extract_district_entries import (  # noqa: E402
    extract, extract_modern, looks_modern, normalize_street,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

UA = "JinkLandmarkSurvey/1.0 (contact@jinkapp.co)"
MIN_CHARS = 200

# DB addresses abbreviate; report headers spell out. Structural forms only.
ABBR = {"PLCE": "PLACE", "PL": "PLACE", "AVE": "AVENUE", "AV": "AVENUE",
        "ST": "STREET", "SQ": "SQUARE", "RD": "ROAD", "TER": "TERRACE",
        "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST", "SO": "SOUTH"}


def norm_db_address(addr: str) -> tuple[int | None, str | None]:
    a = re.sub(r"\s+", " ", (addr or "").upper()).strip()
    m = re.match(r"^(\d+)\s+(.*)$", a)
    if not m:
        return None, None
    toks = [ABBR.get(t, t) for t in m.group(2).split()]
    return int(m.group(1)), normalize_street(" ".join(toks))


def district_reports(rail_url: str) -> list[tuple[str, int]]:
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT source_file, count(DISTINCT bin) nb
        FROM landmark_chunks
        GROUP BY 1 HAVING count(DISTINCT bin) > 50
        ORDER BY nb DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def district_borough(rail_url: str, source_file: str) -> str | None:
    """Borough digit for a district, taken from the modal first digit of its
    BINs. The alternate Block/Lot layout omits the borough, and guessing it
    would silently produce BBLs for the wrong borough."""
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT left(bin, 1) b, count(*) n FROM landmark_chunks
        WHERE source_file = %s AND bin IS NOT NULL AND bin <> ''
        GROUP BY 1 ORDER BY n DESC LIMIT 1
    """, (source_file,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row and row[0] in "12345" else None


def district_addresses(rail_url: str, source_file: str) -> list[tuple[str, str]]:
    conn = psycopg2.connect(rail_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT bin, address FROM landmark_chunks
        WHERE source_file = %s AND address IS NOT NULL
    """, (source_file,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def match_legacy(entries, rows) -> int:
    index: dict[tuple[int, str], bool] = {}
    for e in entries:
        for n in e.numbers:
            index[(n, e.street)] = True
    streets = sorted({e.street for e in entries})
    cache: dict[str, str] = {}

    def resolve(s: str) -> str:
        if s not in cache:
            if any(k[1] == s for k in index):
                cache[s] = s
            else:
                c = difflib.get_close_matches(s, streets, n=1, cutoff=0.86)
                cache[s] = c[0] if c else s
        return cache[s]

    hit = 0
    for _bin, addr in rows:
        n, s = norm_db_address(addr)
        if n is None:
            continue
        if (n, resolve(s)) in index:
            hit += 1
    return hit


def match_modern(entries, buildings_url: str) -> int:
    bbls = sorted({e.bbl for e in entries if e.bbl})
    if not bbls:
        return 0
    conn = psycopg2.connect(buildings_url)
    cur = conn.cursor()
    cur.execute("""
        SELECT count(*) FROM buildings_full_merge_scanning
        WHERE replace(bbl, '.0', '') = ANY(%s)
    """, (bbls,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


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


def summarize(path: str) -> None:
    if not os.path.exists(path):
        print("no results yet")
        return
    recs = [json.loads(l) for l in open(path) if l.strip()]
    ok = [r for r in recs if not r.get("error")]
    bins = sum(r["bins_in_district"] for r in ok)
    matched = sum(r["matched"] for r in ok)
    modern = [r for r in ok if r["format"] == "modern"]
    legacy = [r for r in ok if r["format"] == "legacy"]

    def rate(rs):
        b = sum(r["bins_in_district"] for r in rs)
        m = sum(r["matched"] for r in rs)
        return b, m, (100 * m / b if b else 0)

    print(f"\nreports surveyed : {len(ok)} ok, {len(recs)-len(ok)} failed")
    for label, rs in (("modern", modern), ("legacy", legacy)):
        b, m, pct = rate(rs)
        print(f"  {label:6}: {len(rs):3d} reports  {m:6d}/{b:6d} bins  {pct:.0f}%")
    print(f"  TOTAL : {matched}/{bins} bins = {100*matched/bins if bins else 0:.0f}%")
    worst = sorted(ok, key=lambda r: r["matched"] / max(r["bins_in_district"], 1))[:8]
    print("\nlowest-yield reports (inspect these):")
    for r in worst:
        pct = 100 * r["matched"] / max(r["bins_in_district"], 1)
        print(f"  {r['report']:>10} {r['format']:6} {r['matched']:5d}/{r['bins_in_district']:5d} = {pct:3.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="survey.jsonl")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tmp", default="/tmp/jink_survey.pdf")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="pause between downloads; this hits a city server 94 times")
    args = ap.parse_args()

    if args.summary_only:
        summarize(args.out)
        return 0

    rail = os.environ.get("FOOTPRINTS_DB_URL")
    bldg = os.environ.get("DATABASE_URL")
    if not rail or not bldg:
        print("FOOTPRINTS_DB_URL and DATABASE_URL required", file=sys.stderr)
        return 2

    done = set()
    if os.path.exists(args.out):
        for line in open(args.out):
            try:
                done.add(json.loads(line)["source_file"])
            except Exception:
                pass

    reports = district_reports(rail)
    todo = [r for r in reports if r[0] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(reports)} district reports, {len(done)} already done, {len(todo)} to go",
          flush=True)

    for i, (src, nbins) in enumerate(todo, 1):
        name = src.split("/")[-1]
        print(f"[{i}/{len(todo)}] {name} ({nbins} bins)", flush=True)
        rec: dict = {"source_file": src, "report": name, "bins_in_district": nbins}
        if not download(src, args.tmp):
            rec["error"] = "download"
            with open(args.out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            continue
        try:
            size_mb = os.path.getsize(args.tmp) / 1e6
            modern = looks_modern(args.tmp)
            if modern:
                boro = district_borough(rail, src)
                ents = [e for e in extract_modern(args.tmp, name, default_borough=boro)
                        if len(e.text) >= MIN_CHARS]
                matched = match_modern(ents, bldg)
                rec.update(format="modern", entries=len(ents), matched=matched)
            else:
                ents = [e for e in extract(args.tmp, name) if len(e.text) >= MIN_CHARS]
                rows = district_addresses(rail, src)
                matched = match_legacy(ents, rows)
                rec.update(format="legacy", entries=len(ents), matched=matched)
            rec["size_mb"] = round(size_mb, 1)
            pct = 100 * rec["matched"] / max(nbins, 1)
            print(f"  {rec['format']}: {rec['entries']} entries, "
                  f"{rec['matched']}/{nbins} matched ({pct:.0f}%)", flush=True)
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
            print(f"  extract failed: {rec['error']}", flush=True)
        finally:
            if os.path.exists(args.tmp):
                os.remove(args.tmp)
        with open(args.out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        time.sleep(args.sleep)

    summarize(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Ingest NYC LPC designation report PDFs into the `landmark_chunks` table so the
lore chain's tier-1 (BIN-keyed LPC retrieval in services/lore_generator.py) has
a corpus to read. Today `landmark_chunks` is EMPTY, so tier-1 never fires and
every building falls through to Wikipedia/Grok.

How it works — no scraping, no manual collection:
  1. Read every designated landmark (BIN + lp_number) from the BUILDINGS Supabase
     (`DATABASE_URL`) — these are populated by scripts/seed_lpc_landmarks.py
     (Jink_Swift repo). After that seed, ~2,400 rows carry an `LP-#####`.
  2. For each LP number, the report PDF lives at a DETERMINISTIC URL:
        LP-00962 -> https://s-media.nyc.gov/agencies/lpc/lp/0962.pdf
  3. Fetch the PDF, extract text with pypdf, chunk it, and upsert rows into
     `landmark_chunks` keyed by BIN (the exact key lore_generator reads).

Retrieval is BIN-keyed exact match (NOT vector) — you always want the report for
THIS building, so no embeddings are needed here (unlike the search indexes).

Env:
  DATABASE_URL       BUILDINGS Supabase — source of BIN + lp_number      [required]
  FOOTPRINTS_DB_URL  Railway Postgres — where landmark_chunks lives       [required]

Usage:
  python scripts/ingest_landmark_reports.py --dry-run        # preview, no writes
  python scripts/ingest_landmark_reports.py                  # ingest missing only
  python scripts/ingest_landmark_reports.py --include-districts  # + district reports
  python scripts/ingest_landmark_reports.py --rebuild        # re-ingest all
  python scripts/ingest_landmark_reports.py --limit 20       # first N (testing)

Coverage: ~1,609 individual/interior/scenic reports cover ~2,400 BINs with
building-SPECIFIC text. Adding --include-districts fans 160 Historic District
reports out to ~36k member BINs (district-level context; an individual report
always wins for a BIN that has one). Total distinct reports in NYC: ~1,769.
"""

import argparse
import io
import logging
import os
import re
import sys
import time

import httpx
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import pypdf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_landmark_reports")

UA = "JinkLandmarkIngest/1.0 (contact@jinkapp.co)"
CHUNK_CHARS = 1500      # ~chunk size; reports are split into chunk_index 0..n
CHUNK_OVERLAP = 150     # carry a little context across boundaries
MIN_CHUNK_CHARS = 80    # drop trailing scraps

_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")
# pypdf splits letters across the two-column LPC layout ("La ndmarks"). Collapse
# runs of single spaces inside words conservatively: join a lone space between
# two word chars only when it produces no real word boundary loss is hard to do
# perfectly — so we just normalize whitespace and let Grok synthesis clean the
# rest (it already handles raw LPC text in lore_generator).


def clean_text(raw: str) -> str:
    raw = raw.replace("\r", "\n")
    raw = _WS.sub(" ", raw)
    raw = _NL.sub("\n\n", raw)
    return raw.strip()


def lp_to_pdf_url(lp: str) -> str | None:
    """'LP-00962' -> 'https://s-media.nyc.gov/agencies/lpc/lp/0962.pdf' (4-digit)."""
    try:
        n = int(lp.replace("LP-", "").strip())
        return f"https://s-media.nyc.gov/agencies/lpc/lp/{n:04d}.pdf"
    except (ValueError, AttributeError):
        return None


def chunk_text(text: str) -> list[str]:
    """Greedy char-window chunks with small overlap, split on paragraph breaks
    where possible so a chunk doesn't end mid-sentence."""
    text = clean_text(text)
    if not text:
        return []
    chunks, start = [], 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_CHARS, n)
        # prefer to break on a paragraph/sentence boundary near the window end
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". "):
                idx = window.rfind(sep)
                if idx > CHUNK_CHARS * 0.5:
                    end = start + idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if len(chunk) >= MIN_CHUNK_CHARS:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


SOCRATA_URL = "https://data.cityofnewyork.us/resource/ncre-qhxs.json"


def load_landmarks(buildings_url: str, limit: int | None) -> list[dict]:
    """BIN + lp_number + name/address/bbl for every individually-designated
    landmark with an LP number. One report per lp_number, but a report can cover
    multiple BINs — we ingest per (bin, lp) so retrieval by BIN always resolves.
    These were backfilled by scripts/seed_lpc_landmarks.py."""
    sql = """
        SELECT REPLACE(bin, '.0', '') AS bin, lp_number, building_name, bbl,
               COALESCE(address, '') AS address
        FROM buildings_full_merge_scanning
        WHERE lp_number ~ '^LP-' AND bin IS NOT NULL
    """
    with psycopg.connect(buildings_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def load_district_members(client: httpx.Client, limit: int | None) -> list[dict]:
    """Historic-district member BINs + their district LP number, straight from
    the LPC Socrata dataset. Phase 1 (seed_lpc_landmarks) deliberately skips
    districts, so the buildings DB has no lp_number for these rows. A single
    district report fans out to all its member BINs — accurate-but-generic
    district context for buildings that have no individual report."""
    rows, offset = [], 0
    while True:
        resp = client.get(
            SOCRATA_URL,
            params={
                "$where": "lm_type='Historic District' AND bin_number IS NOT NULL AND lp_number IS NOT NULL",
                "$select": "bin_number,bbl,lp_number,lm_name,pluto_addr,desig_addr",
                "$limit": 5000, "$offset": offset,
            },
            headers={"User-Agent": UA}, timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        for r in page:
            rows.append({
                "bin": (r.get("bin_number") or "").replace(".0", "").strip(),
                "lp_number": r.get("lp_number"),
                "building_name": r.get("lm_name"),
                "bbl": r.get("bbl"),
                "address": r.get("pluto_addr") or r.get("desig_addr") or "",
                "is_district": True,
            })
        offset += len(page)
        if len(page) < 5000:
            break
    rows = [r for r in rows if r["bin"] and r["lp_number"]]
    if limit:
        rows = rows[:limit]
    return rows


def load_indexed(rail_url: str) -> set:
    """BINs that already have chunks — so a normal run only ingests new ones."""
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT bin FROM landmark_chunks WHERE bin IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


def fetch_pdf_text(url: str, client: httpx.Client) -> str | None:
    try:
        resp = client.get(url, headers={"User-Agent": UA}, timeout=40, follow_redirects=True)
        if resp.status_code != 200 or "pdf" not in resp.headers.get("content-type", "").lower():
            return None
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        logger.warning(f"  fetch/parse failed for {url}: {e}")
        return None


def replace_bin_chunks(rail_url: str, bin_: str, lp: str, name, address, chunks: list[str]):
    """Idempotent: clear this BIN's existing chunks, then insert fresh ones."""
    src = lp_to_pdf_url(lp) or lp
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM landmark_chunks WHERE bin = %s", (bin_,))
        cur.executemany(
            """
            INSERT INTO landmark_chunks
                (building_name, bin, bbl, address, chunk_text, chunk_index, source_file, page_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(name, bin_, None, address, c, i, src, None) for i, c in enumerate(chunks)],
        )
        conn.commit()


async def synthesize_district_blurb(name: str, raw_chunks: list[str]) -> str | None:
    """A Historic District report describes the whole neighborhood (often a
    400-page PDF that's mostly legal boundary metes-and-bounds). Fanning its raw
    chunks to every member BIN is both huge (22M rows) and low-value. Instead we
    distill ONE clean 3-4 sentence blurb per district and fan that — accurate
    district context for every building inside it, no per-BIN duplication.
    Run once per district (~160 calls)."""
    from services.grok import grok_text

    # Use the front matter (designation rationale lives near the top) plus a
    # mid-report sample, trimmed — skip the boundary survey tail.
    head = "\n".join(raw_chunks[:6])
    mid = "\n".join(raw_chunks[len(raw_chunks) // 3: len(raw_chunks) // 3 + 4]) if len(raw_chunks) > 12 else ""
    source = (head + "\n" + mid)[:6000]

    system = (
        "You are an architecture writer for Jink, a NYC discovery app. From a "
        "Landmarks Preservation Commission historic-district designation report, "
        "write 3-4 punchy sentences on what this DISTRICT is and why it was "
        "designated: its era, dominant architectural styles, and what makes the "
        "neighborhood distinctive. Strict grounding: use only facts present in "
        "the source text. The source is OCR'd and may have spacing artifacts — "
        "read through them. No clichés ('quiet sentinel', 'time capsule', "
        "'bustling streets'), no markdown, no boundary/lot descriptions, no "
        "designation list numbers."
    )
    user = f"Historic District: {name}\n\nSource material:\n{source}"
    result = await grok_text(system=system, user=user, max_tokens=260, temperature=0.3)
    return result.strip() if result and len(result) > 40 else None


def write_district_blurb(rail_url: str, members: list[dict], lp: str, blurb: str):
    """Fan ONE district blurb (single chunk) to every member BIN. Idempotent per
    BIN. One small row per building — not the full report duplicated."""
    src = lp_to_pdf_url(lp) or lp
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        bins = [m["bin"] for m in members]
        cur.execute("DELETE FROM landmark_chunks WHERE bin = ANY(%s)", (bins,))
        cur.executemany(
            """
            INSERT INTO landmark_chunks
                (building_name, bin, bbl, address, chunk_text, chunk_index, source_file, page_number)
            VALUES (%s, %s, %s, %s, %s, 0, %s, NULL)
            """,
            [(m["building_name"], m["bin"], None, m["address"], blurb, src) for m in members],
        )
        conn.commit()


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-ingest BINs that already have chunks")
    ap.add_argument("--limit", type=int, default=None, help="only the first N landmarks (testing)")
    ap.add_argument("--include-districts", action="store_true",
                    help="also give Historic District member BINs a synthesized "
                         "district blurb (only where no individual report exists)")
    args = ap.parse_args()

    buildings_url = os.environ.get("DATABASE_URL")
    rail_url = os.environ.get("FOOTPRINTS_DB_URL")
    if not buildings_url or not rail_url:
        logger.error("DATABASE_URL and FOOTPRINTS_DB_URL must be set")
        sys.exit(1)

    landmarks = load_landmarks(buildings_url, args.limit)
    logger.info(f"{len(landmarks)} individually-designated landmark rows with an LP number")

    district: list[dict] = []
    if args.include_districts:
        individual_bins = {lm["bin"] for lm in landmarks}
        with httpx.Client() as c:
            district = load_district_members(c, args.limit)
        # An individual report always wins over district-level context.
        district = [d for d in district if d["bin"] not in individual_bins]
        logger.info(f"{len(district)} district-member BINs (excluding those with an individual report)")

    indexed = set() if args.rebuild else load_indexed(rail_url)
    logger.info(f"{len(indexed)} BINs already have chunks")

    # ---- Individual / interior / scenic: raw building-specific chunks ----
    todo = [lm for lm in landmarks if args.rebuild or lm["bin"] not in indexed]
    by_lp: dict[str, list[dict]] = {}
    for lm in todo:
        by_lp.setdefault(lm["lp_number"], []).append(lm)
    logger.info(f"individual: {len(todo)} BINs over {len(by_lp)} reports")

    total_chunks = bins_done = pdf_misses = 0
    with httpx.Client() as client:
        for n, (lp, members) in enumerate(by_lp.items(), 1):
            url = lp_to_pdf_url(lp)
            if not url:
                continue
            raw = fetch_pdf_text(url, client)
            chunks = chunk_text(raw) if raw else []
            time.sleep(0.3)
            if not chunks:
                pdf_misses += 1
                continue
            if args.dry_run:
                if n <= 4:
                    logger.info(f"  [{lp}] {len(chunks)} chunks → {len(members)} BIN(s)")
                total_chunks += len(chunks) * len(members)
                bins_done += len(members)
                continue
            for lm in members:
                replace_bin_chunks(rail_url, lm["bin"], lp, lm["building_name"], lm["address"], chunks)
                bins_done += 1
                total_chunks += len(chunks)
            if n % 50 == 0:
                logger.info(f"  individual {n}/{len(by_lp)} · {bins_done} BINs")

    # ---- Historic districts: one synthesized blurb per district, fanned out ----
    dist_bins = dist_reports = dist_misses = 0
    if district:
        dist_todo = [d for d in district if args.rebuild or d["bin"] not in indexed]
        by_dist: dict[str, list[dict]] = {}
        for d in dist_todo:
            by_dist.setdefault(d["lp_number"], []).append(d)
        logger.info(f"districts: {len(dist_todo)} BINs over {len(by_dist)} reports")
        with httpx.Client() as client:
            for n, (lp, members) in enumerate(by_dist.items(), 1):
                url = lp_to_pdf_url(lp)
                if not url:
                    continue
                raw = fetch_pdf_text(url, client)
                chunks = chunk_text(raw) if raw else []
                time.sleep(0.3)
                if not chunks:
                    dist_misses += 1
                    continue
                name = members[0]["building_name"] or "Historic District"
                if args.dry_run:
                    if n <= 4:
                        logger.info(f"  [{lp}] {name}: {len(chunks)} raw chunks → 1 blurb → {len(members)} BIN(s)")
                    dist_reports += 1
                    dist_bins += len(members)
                    continue
                blurb = await synthesize_district_blurb(name, chunks)
                if not blurb:
                    dist_misses += 1
                    continue
                write_district_blurb(rail_url, members, lp, blurb)
                dist_reports += 1
                dist_bins += len(members)
                if n % 20 == 0:
                    logger.info(f"  district {n}/{len(by_dist)} · {dist_bins} BINs blurbed")

    if args.dry_run:
        logger.info(f"dry-run: individual ~{total_chunks} chunks over {bins_done} BINs "
                    f"({pdf_misses} misses); districts {dist_reports} reports → {dist_bins} BINs "
                    f"(1 blurb each); no writes")
        return
    logger.info(f"✅ done — individual: {bins_done} BINs / {total_chunks} chunks ({pdf_misses} misses); "
                f"districts: {dist_reports} reports → {dist_bins} BINs ({dist_misses} misses)")


if __name__ == "__main__":
    import asyncio
    asyncio.run(amain())

#!/usr/bin/env python3
"""
Process NYC Landmarks PDF reports into text chunks for Railway RAG.

Uses CSV LP→BIN mapping for accurate BIN assignment (no fuzzy matching needed).
Text-only: no embeddings.

Usage:
    python scripts/process_landmark_pdfs.py /path/to/pdfs --csv /path/to/csv.csv --railway
    python scripts/process_landmark_pdfs.py /path/to/pdfs --csv ... --railway --dry-run
    python scripts/process_landmark_pdfs.py /path/to/pdfs --csv ... --railway --limit 5
"""

import os
import sys
import re
import csv
import argparse
from pathlib import Path
from typing import List, Dict, Optional

import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

load_dotenv()

CHUNK_SIZE = 500   # tokens (~4 chars each)
CHUNK_OVERLAP = 50
BATCH_SIZE = 20

RAILWAY_URL = "postgres://postgres:FgefB6c14fGCGbG4EdEb2a3D2F4b4cEB@metro.proxy.rlwy.net:56050/railway"


# ── LP / CSV helpers ─────────────────────────────────────────────────────────

def normalize_lp_number(filename: str) -> str | None:
    """Convert [LP-0992] in filename to LP-00992."""
    m = re.search(r'\[LP-(\d+)\]', filename)
    if not m:
        return None
    return f"LP-{int(m.group(1)):05d}"


def load_csv_maps(csv_path: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Build LP_NUMBER → BIN list and LP_NUMBER → building name from CSV."""
    lp_map: dict[str, list[str]] = {}
    name_map: dict[str, str] = {}
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            lp = row.get('LP_NUMBER', '').strip()
            if not lp:
                continue
            name = row.get('BUILDING_NAME', '') or row.get('NAME', '') or ''
            if name.strip() and lp not in name_map:
                name_map[lp] = name.strip()
            if row.get('STATUS', '').strip().upper() != 'DESIGNATED':
                continue
            bin_val = row.get('BIN_NUMBER', '').strip().replace('.0', '')
            if not bin_val or bin_val == '0':
                continue
            lp_map.setdefault(lp, [])
            if bin_val not in lp_map[lp]:
                lp_map[lp].append(bin_val)
    return lp_map, name_map


def load_lp_to_bins(csv_path: str) -> dict[str, list[str]]:
    """Build LP_NUMBER → list of BIN_NUMBER (DESIGNATED, BIN != 0)."""
    lp_map: dict[str, list[str]] = {}
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('STATUS', '').strip().upper() != 'DESIGNATED':
                continue
            lp = row.get('LP_NUMBER', '').strip()
            bin_val = row.get('BIN_NUMBER', '').strip().replace('.0', '')
            if not lp or not bin_val or bin_val == '0':
                continue
            lp_map.setdefault(lp, [])
            if bin_val not in lp_map[lp]:
                lp_map[lp].append(bin_val)
    return lp_map


def get_building_name_from_csv(lp: str, csv_path: str) -> str:
    try:
        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('LP_NUMBER', '').strip() == lp:
                    name = row.get('BUILDING_NAME', '') or row.get('NAME', '') or ''
                    if name.strip():
                        return name.strip()
    except Exception:
        pass
    return lp


# ── PDF helpers ───────────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> List[Dict]:
    """Extract text from PDF with page numbers using PyPDF2."""
    try:
        import PyPDF2
        pages = []
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append({"page": i + 1, "text": text})
        return pages
    except Exception as e:
        print(f"  ❌ Error reading {pdf_path}: {e}")
        return []


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks."""
    chars_per_chunk = chunk_size * 4
    overlap_chars = overlap * 4
    chunks = []
    start = 0
    while start < len(text):
        end = start + chars_per_chunk
        chunk = text[start:end]
        if end < len(text):
            last_period = chunk.rfind('. ')
            if last_period > chars_per_chunk * 0.5:
                chunk = chunk[:last_period + 1]
                end = start + last_period + 1
        chunks.append(chunk.strip())
        start = end - overlap_chars
    return [c for c in chunks if len(c) > 100]


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_processed_pdfs(conn) -> set:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source_file FROM landmark_chunks")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"⚠️  Could not fetch processed PDFs: {e}")
        return set()


def insert_chunks_batch(conn, chunks: List[tuple]):
    try:
        with conn.cursor() as cur:
            execute_batch(
                cur,
                """
                INSERT INTO landmark_chunks
                (building_name, bin, address, chunk_text, chunk_index, source_file, page_number)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                chunks
            )
        conn.commit()
    except Exception as e:
        print(f"  ❌ Insert error: {e}")
        conn.rollback()


# ── Per-PDF processing ────────────────────────────────────────────────────────

def process_pdf(pdf_path: str, building_name: str, bins: list[str], conn, dry_run: bool) -> int:
    """Process single PDF into text chunks and store to Railway."""
    pages = extract_text_from_pdf(pdf_path)
    if not pages:
        print(f"  ⚠️  No text extracted")
        return 0

    # Use first BIN for the chunk record (historic districts may have many)
    primary_bin = bins[0] if bins else None

    all_chunks = []
    for page_data in pages:
        for i, chunk in enumerate(chunk_text(page_data["text"])):
            all_chunks.append((chunk, i, page_data["page"]))

    total = len(all_chunks)

    if dry_run:
        print(f"  → {total} chunks  |  BINs: {bins}")
        return total

    rows = [
        (building_name, primary_bin, None, chunk, idx, Path(pdf_path).name, page)
        for chunk, idx, page in all_chunks
    ]

    for batch_start in range(0, len(rows), BATCH_SIZE):
        insert_chunks_batch(conn, rows[batch_start:batch_start + BATCH_SIZE])

    return total


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Process LPC PDFs into Railway landmark_chunks (text-only, CSV BIN mapping)"
    )
    parser.add_argument("pdf_dir", help="Directory containing PDFs")
    parser.add_argument("--csv", required=True, help="LP→BIN mapping CSV")
    parser.add_argument("--railway", action="store_true", help="Write to Railway (required for real run)")
    parser.add_argument("--limit", type=int, help="Max PDFs to process")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    args = parser.parse_args()

    if not args.railway and not args.dry_run:
        print("❌ Pass --railway to write to Railway, or --dry-run to preview.")
        sys.exit(1)

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.exists():
        print(f"❌ Directory not found: {pdf_dir}")
        sys.exit(1)

    print(f"📋 Loading LP→BIN map from {args.csv}...")
    lp_map, name_map = load_csv_maps(args.csv)
    print(f"  {len(lp_map)} LP numbers with DESIGNATED BINs")

    pdfs = sorted(pdf_dir.glob("**/*.pdf"))
    print(f"📚 Found {len(pdfs)} PDFs")

    if args.limit:
        pdfs = pdfs[:args.limit]
        print(f"📌 Limiting to first {args.limit}")

    conn = None
    if not args.dry_run:
        if not args.railway:
            settings = get_settings()
            conn = psycopg2.connect(settings.database_url)
            print("✅ Connected to Supabase")
        else:
            conn = psycopg2.connect(RAILWAY_URL)
            print("✅ Connected to Railway")

        # Resume support
        processed_files = get_processed_pdfs(conn)
        if processed_files:
            before = len(pdfs)
            pdfs = [p for p in pdfs if p.name not in processed_files]
            print(f"⏭️  Skipping {before - len(pdfs)} already-processed PDFs ({len(pdfs)} remaining)")

    if not pdfs:
        print("✅ Nothing to process.")
        if conn:
            conn.close()
        return

    # Confirm wipe intent
    if not args.dry_run and not args.limit:
        print(f"\n⚠️  About to ingest {len(pdfs)} PDFs into Railway landmark_chunks.")
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != 'yes':
            print("Aborted.")
            sys.exit(0)

    total_chunks = 0
    total = len(pdfs)

    for i, pdf in enumerate(pdfs, 1):
        lp = normalize_lp_number(pdf.name)
        bins = lp_map.get(lp, []) if lp else []
        building_name = (name_map.get(lp, lp) if lp else pdf.stem.replace('_', ' '))

        if i % 25 == 0 or args.limit:
            print(f"[{i}/{total}] {lp or pdf.name} — {building_name}")

        try:
            n = process_pdf(str(pdf), building_name, bins, conn, dry_run=args.dry_run)
            total_chunks += n
        except Exception as e:
            print(f"  ❌ Error: {e}")
            continue

    if conn:
        conn.close()

    print(f"\n✅ Done! PDFs: {len(pdfs)}  |  Total chunks: {total_chunks}")
    if args.dry_run:
        print("🔍 DRY RUN — no data written")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Link landmark_chunks rows to BINs in buildings_full_merge_scanning.

The PDF processing script didn't populate the bin column in landmark_chunks.
This script matches each unique source_file → BIN using a 3-tier strategy:

  Tier 1: Exact name match  (building_name or wiki_name == extracted PDF name)
  Tier 2: Fuzzy name match  (trigram similarity >= 0.5 on building_name/wiki_name)
  Tier 3: Address match     (des_addres normalized == address extracted from PDF name)

After matching, updates landmark_chunks.bin for all chunks from that source_file.

Usage:
    python scripts/link_chunks_to_bins.py [--dry-run] [--min-similarity 0.5]
"""

import sys
import re
import argparse
from pathlib import Path
from difflib import SequenceMatcher

import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

load_dotenv()

RAILWAY_URL = "postgres://postgres:FgefB6c14fGCGbG4EdEb2a3D2F4b4cEB@metro.proxy.rlwy.net:56050/railway"


def extract_name_from_filename(source_file: str) -> str:
    """
    '[LP-0992] Chrysler Building.pdf' → 'Chrysler Building'
    '[LP-0008] 51 Market Street House.pdf' → '51 Market Street House'
    """
    name = Path(source_file).stem  # strip .pdf
    name = re.sub(r'^\[LP-\d+\]\s*', '', name).strip()
    return name


def normalize(s: str) -> str:
    """Lowercase, strip punctuation/extra spaces for comparison."""
    s = s.lower()
    s = re.sub(r"[''',.\-()]", ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def load_landmark_buildings(supabase_conn) -> list[dict]:
    """Load all landmark buildings with name/address fields for matching."""
    with supabase_conn.cursor() as cur:
        cur.execute("""
            SELECT
                REPLACE(bin, '.0', '') as bin,
                building_name,
                wiki_name,
                des_addres,
                address
            FROM buildings_full_merge_scanning
            WHERE landmark IS NOT NULL AND landmark != ''
              AND bin IS NOT NULL AND bin != '' AND bin != '0'
        """)
        cols = ['bin', 'building_name', 'wiki_name', 'des_addres', 'address']
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_indexes(buildings: list[dict]) -> tuple[dict, dict, dict]:
    """
    Build three indexes for fast lookup:
      exact_idx:  normalize(name) → bin
      token_idx:  first_token → [buildings]  (for fuzzy candidate pruning)
      addr_idx:   normalize(address_prefix) → bin
    """
    exact_idx = {}
    token_idx = {}
    addr_idx = {}

    for b in buildings:
        bin_val = b['bin']
        for field in ('building_name', 'wiki_name'):
            val = b.get(field) or ''
            if not val:
                continue
            norm = normalize(val)
            exact_idx[norm] = bin_val
            # Index by each word token for fast candidate retrieval
            for token in norm.split():
                if len(token) >= 3:
                    token_idx.setdefault(token, []).append(b)

        for field in ('des_addres', 'address'):
            val = b.get(field) or ''
            if val:
                norm = normalize(val)
                addr_idx[norm] = bin_val

    return exact_idx, token_idx, addr_idx


def match_to_bin(
    pdf_name: str,
    exact_idx: dict,
    token_idx: dict,
    addr_idx: dict,
    min_similarity: float
) -> tuple[str | None, str, float]:
    """
    Returns (bin, match_tier, score) or (None, '', 0).
    """
    norm_pdf = normalize(pdf_name)

    # Tier 1: exact name match
    if norm_pdf in exact_idx:
        return exact_idx[norm_pdf], 'exact_name', 1.0

    # Tier 2: fuzzy match — only score candidates that share a token
    tokens = [t for t in norm_pdf.split() if len(t) >= 3]
    candidates = {}
    for token in tokens:
        for b in token_idx.get(token, []):
            candidates[b['bin']] = b

    best_bin, best_score = None, 0.0
    for b in candidates.values():
        for field in ('building_name', 'wiki_name'):
            val = b.get(field) or ''
            if not val:
                continue
            score = similarity(pdf_name, val)
            if score > best_score:
                best_score = score
                best_bin = b['bin']
    if best_score >= min_similarity:
        return best_bin, 'fuzzy_name', best_score

    # Tier 3: address match for PDFs with street numbers
    addr_candidate = re.sub(
        r'\b(house|building|hall|club|church|school|library|hotel|theater|theatre|'
        r'apartments|museum|center|centre|station|tower|plaza|complex|chapel|'
        r'synagogue|temple|convent|rectory|stable|garage|factory|warehouse|loft)\b',
        '', pdf_name, flags=re.IGNORECASE
    ).strip()

    if addr_candidate and re.search(r'\d', addr_candidate):
        norm_addr = normalize(addr_candidate)
        # Check for prefix match in addr_idx
        for addr_norm, bin_val in addr_idx.items():
            if addr_norm.startswith(norm_addr) or norm_addr.startswith(addr_norm.split(',')[0]):
                return bin_val, 'address', 0.9

    return None, '', 0.0


def update_chunks_bin(railway_conn, source_file: str, bin_val: str, dry_run: bool) -> int:
    """Update all chunks for a source_file with the matched BIN. Returns row count."""
    with railway_conn.cursor() as cur:
        if dry_run:
            cur.execute(
                "SELECT COUNT(*) FROM landmark_chunks WHERE source_file = %s",
                (source_file,)
            )
            return cur.fetchone()[0]
        cur.execute(
            "UPDATE landmark_chunks SET bin = %s WHERE source_file = %s",
            (bin_val, source_file)
        )
        count = cur.rowcount
    railway_conn.commit()
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--min-similarity', type=float, default=0.55,
                        help='Min fuzzy match score (0-1) for tier 2 matching')
    parser.add_argument('--verbose', action='store_true', help='Print all matches including skipped')
    args = parser.parse_args()

    settings = get_settings()
    supabase_conn = psycopg2.connect(settings.database_url)
    railway_conn = psycopg2.connect(RAILWAY_URL)
    print(f"✅ Connected to Supabase and Railway")

    # Load all landmark buildings and build indexes
    buildings = load_landmark_buildings(supabase_conn)
    print(f"Loaded {len(buildings)} landmark buildings from Supabase")
    exact_idx, token_idx, addr_idx = build_indexes(buildings)
    print(f"Indexes built: {len(exact_idx)} exact, {len(token_idx)} token buckets, {len(addr_idx)} addresses")

    # Get distinct source files from Railway
    with railway_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_file FROM landmark_chunks ORDER BY source_file")
        source_files = [r[0] for r in cur.fetchall()]
    print(f"Found {len(source_files)} distinct PDFs in landmark_chunks\n")

    matched = 0
    unmatched = 0
    total_chunks_linked = 0

    unmatched_files = []

    for source_file in source_files:
        pdf_name = extract_name_from_filename(source_file)
        bin_val, tier, score = match_to_bin(pdf_name, exact_idx, token_idx, addr_idx, args.min_similarity)

        if bin_val:
            chunk_count = update_chunks_bin(railway_conn, source_file, bin_val, args.dry_run)
            total_chunks_linked += chunk_count
            matched += 1
            if args.verbose or tier != 'exact_name':
                tag = '[DRY]' if args.dry_run else '    '
                print(f"{tag} ✅ [{tier:<12} {score:.2f}] {pdf_name[:50]:<50} → BIN {bin_val} ({chunk_count} chunks)")
        else:
            unmatched += 1
            unmatched_files.append(pdf_name)
            if args.verbose:
                print(f"     ❌ NO MATCH: {pdf_name}")

    supabase_conn.close()
    railway_conn.close()

    print(f"\n{'='*60}")
    print(f"Matched:   {matched:>4} PDFs  ({total_chunks_linked} chunks linked)")
    print(f"Unmatched: {unmatched:>4} PDFs")

    if unmatched_files:
        print(f"\nFirst 20 unmatched PDFs:")
        for name in unmatched_files[:20]:
            print(f"  - {name}")


if __name__ == '__main__':
    main()

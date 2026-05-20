#!/usr/bin/env python3
"""
Restore corrupted storytelling column from landmark_chunks.

The enrich_landmarks.py script wrote float scores (e.g. 0.90207738340985) into
the storytelling column instead of text. This script:
  1. Nulls out all rows where storytelling looks like a float
  2. For each landmark with null storytelling, pulls top 2-3 chunks from
     landmark_chunks, synthesizes with Gemini into punchy app-quality copy,
     and writes back to storytelling.

Usage:
    python scripts/restore_storytelling.py [--dry-run] [--limit N] [--no-synthesize]
"""

import os
import sys
import re
import argparse
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

load_dotenv()

FLOAT_PATTERN = re.compile(r'^\d+\.\d+$')


def null_corrupted_rows(conn, dry_run: bool) -> int:
    """Set storytelling = NULL where the value looks like a float score."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM buildings_full_merge_scanning
            WHERE storytelling ~ '^[0-9]+\\.[0-9]+$'
        """)
        count = cur.fetchone()[0]
        print(f"Found {count} rows with float-corrupted storytelling")

        if not dry_run and count > 0:
            cur.execute("""
                UPDATE buildings_full_merge_scanning
                SET storytelling = NULL
                WHERE storytelling ~ '^[0-9]+\\.[0-9]+$'
            """)
            conn.commit()
            print(f"  ✅ Nulled {count} corrupted rows")
    return count


def get_landmark_bins_missing_storytelling(conn, limit: int, resynthesize: bool = False) -> list:
    """Return list of (bin, building_name) for landmarks lacking storytelling (or with raw LPC text)."""
    with conn.cursor() as cur:
        if resynthesize:
            # Also grab rows that contain raw LPC dump text
            cur.execute("""
                SELECT REPLACE(bin, '.0', ''), building_name
                FROM buildings_full_merge_scanning
                WHERE (landmark IS NOT NULL AND landmark != '')
                  AND (
                    storytelling IS NULL OR storytelling = ''
                    OR storytelling ~ 'LP-[0-9]+'
                    OR storytelling ILIKE '%%Landmarks Preservation Commission%%'
                    OR storytelling ~ '^[0-9]+\\.[0-9]+$'
                  )
                LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
                SELECT REPLACE(bin, '.0', ''), building_name
                FROM buildings_full_merge_scanning
                WHERE (landmark IS NOT NULL AND landmark != '')
                  AND (storytelling IS NULL OR storytelling = '')
                LIMIT %s
            """, (limit,))
        return cur.fetchall()


def synthesize_with_gemini(raw_text: str, building_name: str) -> str | None:
    """Use Gemini to synthesize raw PDF chunks into punchy app-quality copy."""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt = (
            "You are writing for Jink, a stylish NYC architecture discovery app. "
            "Given raw source material about a building, extract the most interesting, "
            "surprising, and historically rich details. Write 3-4 punchy sentences that "
            "feel like a knowledgeable friend telling you about this place — not a textbook. "
            "Lead with the most fascinating fact. Skip boilerplate like designation dates, "
            "LP numbers, legal language. Focus on: who built it and why, what made it "
            "architecturally daring or significant, any surprising history or cultural resonance.\n\n"
            f"Building: {building_name}\n"
            f"Source material:\n{raw_text}"
        )
        response = model.generate_content(prompt)
        result = response.text.strip() if response.text else None
        if result and len(result) > 30:
            return result
    except Exception as e:
        print(f"  ⚠️  Gemini synthesis failed: {e}")
    return None


def get_chunks_for_bin(railway_conn, bin_val: str, building_name: str, top_n: int = 3) -> list:
    """Fetch top N chunks from landmark_chunks on Railway for a given BIN or building name."""
    with railway_conn.cursor() as cur:
        # Try by BIN first
        cur.execute("""
            SELECT chunk_text
            FROM landmark_chunks
            WHERE bin = %s
            ORDER BY chunk_index ASC
            LIMIT %s
        """, (bin_val, top_n))
        rows = cur.fetchall()

        # Fall back to name match if no BIN match
        if not rows and building_name:
            name_pattern = f"%{building_name.strip()}%"
            cur.execute("""
                SELECT chunk_text
                FROM landmark_chunks
                WHERE source_file ILIKE %s
                ORDER BY chunk_index ASC
                LIMIT %s
            """, (name_pattern, top_n))
            rows = cur.fetchall()

    return [r[0] for r in rows if r[0]]


def write_storytelling(conn, bin_val: str, text: str, dry_run: bool):
    if dry_run:
        preview = text[:120].replace('\n', ' ')
        print(f"  [DRY RUN] Would write to BIN {bin_val}: {preview}...")
        return
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE buildings_full_merge_scanning
            SET storytelling = %s
            WHERE REPLACE(bin, '.0', '') = %s
        """, (text, bin_val))
    conn.commit()


RAILWAY_URL = "postgres://postgres:FgefB6c14fGCGbG4EdEb2a3D2F4b4cEB@metro.proxy.rlwy.net:56050/railway"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--limit', type=int, default=500, help='Max landmarks to process')
    parser.add_argument('--no-synthesize', action='store_true', help='Skip Gemini synthesis, write raw chunks')
    parser.add_argument('--resynthesize', action='store_true', help='Also re-process rows with raw LPC dump text')
    args = parser.parse_args()

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    railway_conn = psycopg2.connect(RAILWAY_URL)
    print(f"✅ Connected to Supabase (storytelling writes) and Railway (landmark_chunks reads)")

    # Step 1: Null out float-corrupted rows
    null_corrupted_rows(conn, args.dry_run)

    # Step 2: Repopulate from landmark_chunks
    rows = get_landmark_bins_missing_storytelling(conn, args.limit, resynthesize=args.resynthesize)
    print(f"\nFound {len(rows)} landmarks with null storytelling to restore")

    restored = 0
    skipped = 0

    for bin_val, building_name in rows:
        chunks = get_chunks_for_bin(railway_conn, bin_val, building_name or '')
        if not chunks:
            skipped += 1
            continue

        combined = '\n\n'.join(chunks)
        if len(combined) > 3000:
            combined = combined[:3000].rsplit(' ', 1)[0] + '…'

        if args.no_synthesize:
            storytelling = combined
        else:
            storytelling = synthesize_with_gemini(combined, building_name or 'Unknown') or combined

        write_storytelling(conn, bin_val, storytelling, args.dry_run)
        restored += 1
        print(f"  ✅ Restored BIN {bin_val} ({building_name or 'unnamed'}) — {len(storytelling)} chars")

    conn.close()
    railway_conn.close()
    print(f"\nDone. Restored: {restored}  |  Skipped (no chunks): {skipped}")


if __name__ == '__main__':
    main()

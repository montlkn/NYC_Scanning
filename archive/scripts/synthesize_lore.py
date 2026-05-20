#!/usr/bin/env python3
"""
Synthesize Jink lore for NYC landmarks directly from PDFs.

Pipeline: PDFs → DESCRIPTION AND ANALYSIS section → Gemini → Supabase storytelling

Usage:
    python scripts/synthesize_lore.py --pdf-dir /path/to/pdfs --csv /path/to/csv.csv
    python scripts/synthesize_lore.py --pdf-dir ... --csv ... --dry-run
    python scripts/synthesize_lore.py --pdf-dir ... --csv ... --limit 5
    python scripts/synthesize_lore.py --pdf-dir ... --csv ... --lp LP-00992
    python scripts/synthesize_lore.py --pdf-dir ... --csv ... --section-only --lp LP-00992
    python scripts/synthesize_lore.py --pdf-dir ... --csv ... --force
"""

import os
import sys
import re
import csv
import time
import json
import argparse
from pathlib import Path
from typing import Optional

import psycopg2
from dotenv import load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from models.config import get_settings

load_dotenv()

PROGRESS_FILE = Path(__file__).parent / "synthesize_lore_progress.json"

SYNTHESIS_PROMPT = """You are writing 3-4 sentences for Jink, an NYC architecture app shown on the building info screen when someone scans a building.

Rules:
- Every sentence must contain a specific, verifiable fact — name, date, material, person, event, decision.
- No filler phrases: "flipped the bird", "giving X vibes", "talk about", "you'll see why", "bet you didn't know", "word is", "basically".
- Do not start with "Okay", "Alright", "Sure", "So", or "I".
- Lead with the single most architecturally or historically significant fact.
- End with something that reframes how the reader sees the building — an unexpected consequence, a reversal, a detail visible from the street.
- Output ONLY the sentences. No headers, labels, options, or markdown.

Building: {building_name}
Source material:
{source_text}"""


def normalize_lp_number(filename: str) -> str | None:
    """Convert 4-digit [LP-0992] in filename to 5-digit LP-00992."""
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
            # Build name map from all rows
            name = row.get('BUILDING_NAME', '') or row.get('NAME', '') or ''
            if name.strip() and lp not in name_map:
                name_map[lp] = name.strip()
            # BIN map: DESIGNATED only
            if row.get('STATUS', '').strip().upper() != 'DESIGNATED':
                continue
            bin_val = row.get('BIN_NUMBER', '').strip().replace('.0', '')
            if not bin_val or bin_val == '0':
                continue
            lp_map.setdefault(lp, [])
            if bin_val not in lp_map[lp]:
                lp_map[lp].append(bin_val)
    return lp_map, name_map


def extract_description_section(full_text: str, max_chars: int = 5000) -> str:
    """Extract DESCRIPTION AND ANALYSIS section from LPC PDF text."""
    # Find the section header (case-insensitive, handles extra spaces)
    pattern = re.compile(r'DESCRIPTION\s+AND\s+ANALYSIS', re.IGNORECASE)
    m = pattern.search(full_text)
    if not m:
        # Fallback: skip first 500 chars of boilerplate, take next max_chars
        return full_text[500:500 + max_chars].strip()

    start = m.end()
    section = full_text[start:]

    # Stop at next major section
    stop_pattern = re.compile(r'\b(FINDINGS|RESOLUTION|APPENDIX)\b', re.IGNORECASE)
    stop = stop_pattern.search(section)
    if stop:
        section = section[:stop.start()]

    return section[:max_chars].strip()


def extract_text_pypdf(pdf_path: str) -> str:
    """Extract full text from PDF using PyPDF2."""
    try:
        import PyPDF2
        text_parts = []
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"  ⚠️  PyPDF2 error: {e}")
        return ""


def synthesize_with_gemini_text(source_text: str, building_name: str, model) -> str | None:
    """Call Gemini with text prompt."""
    prompt = SYNTHESIS_PROMPT.format(building_name=building_name, source_text=source_text)
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            result = response.text.strip() if response.text else None
            if result and len(result) > 30:
                return result
        except Exception as e:
            err = str(e)
            if '429' in err or 'ResourceExhausted' in err:
                wait = 2 ** attempt * 10
                print(f"  ⏳ Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  ⚠️  Gemini error: {e}")
                return None
    return None


def synthesize_with_gemini_pdf(pdf_path: str, building_name: str, genai, model) -> str | None:
    """Fallback: upload PDF to Gemini for scanned/image-only PDFs."""
    uploaded = None
    try:
        print(f"  📤 Uploading PDF to Gemini (scanned PDF fallback)...")
        uploaded = genai.upload_file(pdf_path)
        prompt = SYNTHESIS_PROMPT.format(
            building_name=building_name,
            source_text="[See attached PDF]"
        )
        response = model.generate_content([uploaded, prompt])
        result = response.text.strip() if response.text else None
        return result if result and len(result) > 30 else None
    except Exception as e:
        print(f"  ⚠️  Gemini PDF upload error: {e}")
        return None
    finally:
        if uploaded:
            try:
                genai.delete_file(uploaded.name)
            except Exception:
                pass


def is_already_done(conn, bins: list[str]) -> bool:
    """Return True if any of these BINs already has valid non-boilerplate storytelling."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM buildings_full_merge_scanning
            WHERE REPLACE(bin::text, '.0', '') = ANY(%s)
              AND storytelling IS NOT NULL
              AND storytelling !~ 'LP-[0-9]+'
              AND storytelling NOT ILIKE '%%Landmarks Preservation Commission%%'
              AND LENGTH(storytelling) BETWEEN 100 AND 800
        """, (bins,))
        return cur.fetchone()[0] > 0


def write_storytelling(conn, bins: list[str], text: str, dry_run: bool):
    """Update storytelling for all BINs in one query."""
    if dry_run:
        preview = text[:150].replace('\n', ' ')
        print(f"  [DRY RUN] → {preview}...")
        return
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE buildings_full_merge_scanning
            SET storytelling = %s
            WHERE REPLACE(bin::text, '.0', '') = ANY(%s)
              AND (landmark IS NOT NULL AND landmark != '')
        """, (text, bins))
    conn.commit()


def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()


def save_progress(done: set[str]):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(sorted(done), f)


def get_building_name_from_csv(lp: str, csv_path: str) -> str:
    """Get a building name from the CSV for display purposes."""
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


def main():
    parser = argparse.ArgumentParser(description="Synthesize Jink lore from LPC PDFs via Gemini")
    parser.add_argument('--pdf-dir', required=True, help='Directory of LPC PDFs')
    parser.add_argument('--csv', required=True, help='LP→BIN mapping CSV')
    parser.add_argument('--dry-run', action='store_true', help='Print output, no DB writes')
    parser.add_argument('--limit', type=int, help='Process first N PDFs only')
    parser.add_argument('--force', action='store_true', help='Re-synthesize even if already done')
    parser.add_argument('--lp', help='Process single LP number only (e.g. LP-00992)')
    parser.add_argument('--section-only', action='store_true', help='Print extracted section, skip Gemini')
    parser.add_argument('--yes', action='store_true', help='Skip confirmation prompt')
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.exists():
        print(f"❌ PDF directory not found: {pdf_dir}")
        sys.exit(1)

    gemini_key = os.getenv('GEMINI_API_KEY')
    if not gemini_key:
        print("❌ GEMINI_API_KEY not set")
        sys.exit(1)

    # Load LP→BIN and LP→name maps
    print(f"📋 Loading LP→BIN map from {args.csv}...")
    lp_map, name_map = load_csv_maps(args.csv)
    print(f"  {len(lp_map)} LP numbers with DESIGNATED BINs")

    # Configure Gemini
    import google.generativeai as genai
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-2.0-flash')

    # Connect to Supabase (skip in section-only mode)
    conn = None
    if not args.dry_run and not args.section_only:
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        print("✅ Connected to Supabase")

    # Confirm wipe intent if running full (not dry-run, not single LP, not limit)
    if not args.dry_run and not args.lp and not args.limit and not args.section_only and not args.yes:
        print("\n⚠️  This will write synthesized lore for ALL ~1,745 buildings.")
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != 'yes':
            print("Aborted.")
            sys.exit(0)

    # Find PDFs
    all_pdfs = sorted(pdf_dir.glob("**/*.pdf"))
    print(f"📚 Found {len(all_pdfs)} PDFs")

    # Filter to single LP if requested
    if args.lp:
        all_pdfs = [p for p in all_pdfs if normalize_lp_number(p.name) == args.lp]
        if not all_pdfs:
            print(f"❌ No PDF found for {args.lp}")
            sys.exit(1)
        print(f"  Found {len(all_pdfs)} PDF(s) for {args.lp}")

    if args.limit:
        all_pdfs = all_pdfs[:args.limit]

    # Load progress
    done_lps = load_progress()

    processed = 0
    skipped = 0
    total = len(all_pdfs)

    for i, pdf_path in enumerate(all_pdfs, 1):
        lp = normalize_lp_number(pdf_path.name)
        if not lp:
            print(f"[{i}/{total}] ⚠️  No LP number in filename: {pdf_path.name}")
            continue

        bins = lp_map.get(lp, [])
        building_name = name_map.get(lp, lp)

        if i % 25 == 0 or args.lp:
            print(f"[{i}/{total}] {lp} — {building_name}")

        # Skip if already done (unless --force)
        if not args.force and lp in done_lps:
            skipped += 1
            continue

        if not bins:
            if args.lp:
                print(f"  ⚠️  No DESIGNATED BINs found in CSV for {lp}")
            skipped += 1
            continue

        if not args.force and not args.dry_run and conn and is_already_done(conn, bins):
            done_lps.add(lp)
            skipped += 1
            continue

        # Extract text
        full_text = extract_text_pypdf(str(pdf_path))
        section = extract_description_section(full_text)

        if args.section_only:
            print(f"\n{'='*60}")
            print(f"LP: {lp}  |  BINs: {bins}")
            print(f"{'='*60}")
            print(section)
            continue

        # Fallback to Gemini PDF upload if text extraction failed
        if len(section) < 100:
            lore = synthesize_with_gemini_pdf(str(pdf_path), building_name, genai, model)
        else:
            lore = synthesize_with_gemini_text(section, building_name, model)

        if not lore:
            print(f"  ⚠️  No lore generated for {lp}")
            skipped += 1
            continue

        if args.dry_run or args.lp:
            print(f"\n{'='*60}")
            print(f"LP: {lp}  |  BINs: {bins}")
            print(f"{'='*60}")
            print(lore)
            print(f"[{len(lore)} chars]")
        else:
            write_storytelling(conn, bins, lore, dry_run=False)
            done_lps.add(lp)
            save_progress(done_lps)

        processed += 1

        if not args.dry_run and not args.lp:
            time.sleep(0.5)

    if conn:
        conn.close()

    if not args.section_only:
        print(f"\n✅ Done. Processed: {processed}  |  Skipped: {skipped}")
        if args.dry_run:
            print("🔍 DRY RUN — no writes made")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Propagate historic district lore to all buildings in each district.

Flow:
  1. Download gpmc-yuvp (38k buildings): bin + hist_dist + arch metadata
  2. Download ncre-qhxs (39k rows): lm_name + lp_number (district name → LP)
  3. Join: gpmc bin → hist_dist → lp_number → lore already in DB
  4. For buildings with no district match, synthesize from arch metadata via Gemini

Usage:
    python scripts/propagate_district_lore.py [--dry-run] [--limit N] [--no-synthesize]
"""

import os
import sys
import json
import time
import argparse
import urllib.request
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))
from models.config import get_settings

load_dotenv()

GPMC_URL = "https://data.cityofnewyork.us/resource/gpmc-yuvp.json?$limit=50000&$select=bin,hist_dist,arch_build,own_devel,style_prim,date_low,date_high,build_nme,use_orig,des_addres,borough"
NCRE_URL = "https://data.cityofnewyork.us/resource/ncre-qhxs.json?$limit=50000&$select=lp_number,lm_name,bin_number,status&$where=status='DESIGNATED'"

SYNTH_PROMPT = """You are writing 3-4 sentences for Jink, an NYC architecture app shown on the building info screen.

Rules:
- Every sentence must contain a specific, verifiable fact — name, date, material, person, event, decision.
- No filler phrases: "flipped the bird", "giving X vibes", "talk about", "you'll see why", "bet you didn't know", "word is", "basically".
- Do not start with "Okay", "Alright", "Sure", "So", or "I".
- Lead with the single most architecturally or historically significant fact about this specific building.
- End with something that reframes how the reader sees the building — an unexpected consequence, a reversal, a detail visible from the street.
- If the building metadata is sparse, draw on the district context to place this building in its historical moment.
- Output ONLY the sentences. No headers, labels, options, or markdown.

Building: {name}
Address: {address}, {borough}
Style: {style}
Date built: {date}
Architect/Builder: {architect}
Original use: {use}
Historic district: {district}
District context: {district_lore}"""


def fetch_json(url: str, label: str) -> list:
    print(f"📡 Fetching {label}...")
    req = urllib.request.urlopen(url, timeout=30)
    data = json.loads(req.read())
    print(f"  {len(data)} rows")
    return data


def synthesize(model, row: dict, district_lore: str = "") -> str | None:
    name = row.get("build_nme", "0")
    if name == "0":
        name = row.get("des_addres", "Unknown")
    prompt = SYNTH_PROMPT.format(
        name=name,
        address=row.get("des_addres", ""),
        borough=row.get("borough", ""),
        style=row.get("style_prim", "Not determined"),
        date=row.get("date_low", "Unknown"),
        architect=row.get("arch_build", "Unknown"),
        use=row.get("use_orig", "Unknown"),
        district=row.get("hist_dist", ""),
        district_lore=district_lore or "N/A",
    )
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            result = response.text.strip() if response.text else None
            if result and len(result) > 30:
                return result
        except Exception as e:
            if "429" in str(e) or "ResourceExhausted" in str(e):
                wait = 2 ** attempt * 10
                print(f"  ⏳ Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                return None
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-synthesize", action="store_true", help="Skip Gemini for buildings with no district lore")
    args = parser.parse_args()

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    print("✅ Connected to Supabase")

    # Step 1: Fetch datasets
    gpmc = fetch_json(GPMC_URL, "gpmc-yuvp (building details)")
    ncre = fetch_json(NCRE_URL, "ncre-qhxs (LP→district name)")

    # Step 2: Build district name → LP map from ncre
    district_to_lp: dict[str, str] = {}
    for row in ncre:
        name = row.get("lm_name", "").strip()
        lp = row.get("lp_number", "").strip()
        if name and lp and name not in district_to_lp:
            district_to_lp[name] = lp
    print(f"  {len(district_to_lp)} district name → LP mappings")

    # Also build ncre bin→LP for individual buildings
    ncre_bin_to_lp: dict[str, str] = {}
    for row in ncre:
        b = row.get("bin_number", "").strip()
        lp = row.get("lp_number", "").strip()
        if b and lp:
            ncre_bin_to_lp[b] = lp

    # Step 3: Load existing lore from DB (bin → lore)
    print("📖 Loading existing lore from DB...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT REPLACE(bin::text, '.0', ''), storytelling
            FROM buildings_full_merge_scanning
            WHERE storytelling IS NOT NULL
              AND LENGTH(storytelling) BETWEEN 100 AND 800
              AND storytelling NOT ILIKE '%Okay, here are%'
              AND storytelling NOT ILIKE '%Option%'
        """)
        bin_to_lore = {r[0]: r[1] for r in cur.fetchall()}
    print(f"  {len(bin_to_lore)} BINs with good lore")

    # Build LP → lore (take first good lore found for each LP)
    lp_to_lore: dict[str, str] = {}
    for bin_val, lore in bin_to_lore.items():
        lp = ncre_bin_to_lp.get(bin_val)
        if lp and lp not in lp_to_lore:
            lp_to_lore[lp] = lore
    print(f"  {len(lp_to_lore)} LP numbers have lore")

    # Step 4: Get missing buildings from DB
    with conn.cursor() as cur:
        cur.execute("""
            SELECT REPLACE(bin::text, '.0', ''), building_name
            FROM buildings_full_merge_scanning
            WHERE landmark IS NOT NULL AND landmark != ''
              AND (storytelling IS NULL OR storytelling = '')
        """)
        missing_db = {r[0]: r[1] for r in cur.fetchall()}
    print(f"  {len(missing_db)} landmark buildings still missing lore")

    # Build gpmc bin lookup
    gpmc_by_bin = {row["bin"]: row for row in gpmc if row.get("bin")}

    # Step 5: Setup Gemini if needed
    model = None
    if not args.no_synthesize:
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel("gemini-2.0-flash")

    targets = list(missing_db.keys())
    if args.limit:
        targets = targets[:args.limit]

    propagated = 0
    synthesized = 0
    skipped = 0

    with conn.cursor() as cur:
        for i, bin_val in enumerate(targets, 1):
            if i % 100 == 0:
                print(f"  [{i}/{len(targets)}] propagated={propagated} synthesized={synthesized} skipped={skipped}")

            lore = None
            district_lore = None

            # Find district lore context via gpmc hist_dist → LP
            gpmc_row = gpmc_by_bin.get(bin_val)
            if gpmc_row:
                dist = gpmc_row.get("hist_dist", "").strip()
                if dist:
                    lp = district_to_lp.get(dist)
                    if lp:
                        district_lore = lp_to_lore.get(lp)

            # Try direct ncre bin→LP for district lore fallback
            if not district_lore:
                lp = ncre_bin_to_lp.get(bin_val)
                if lp:
                    district_lore = lp_to_lore.get(lp)

            # Synthesize per-building lore using metadata + district context
            if model and gpmc_row:
                arch = gpmc_row.get("arch_build", "0")
                date = gpmc_row.get("date_low", "0")
                style = gpmc_row.get("style_prim", "0")
                has_metadata = any(v not in ("0", "", "Not determined") for v in [arch, date, style])
                if has_metadata or district_lore:
                    lore = synthesize(model, gpmc_row, district_lore or "")
                    if lore:
                        synthesized += 1
                        time.sleep(0.3)

            # Last resort: copy district lore verbatim
            if not lore and district_lore:
                lore = district_lore

            if not lore:
                skipped += 1
                continue

            if args.dry_run:
                name = missing_db[bin_val] or bin_val
                print(f"  [DRY] BIN {bin_val} ({name}): {lore[:80]}...")
            else:
                cur.execute("""
                    UPDATE buildings_full_merge_scanning
                    SET storytelling = %s
                    WHERE REPLACE(bin::text, '.0', '') = %s
                """, (lore, bin_val))
            propagated += 1

    if not args.dry_run:
        conn.commit()

    conn.close()
    print(f"\n✅ Done.")
    print(f"  Propagated from district lore: {propagated - synthesized}")
    print(f"  Synthesized from metadata:     {synthesized}")
    print(f"  No match found:                {skipped}")
    if args.dry_run:
        print("  🔍 DRY RUN — no writes made")


if __name__ == "__main__":
    main()

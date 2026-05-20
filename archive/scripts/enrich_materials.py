"""
Batch enrichment script: infer mat_prim for buildings where it is null/empty/unknown.

Strategy:
  1. Rule-based pass — covers ~90% of NYC building stock deterministically
     using bldgclass + year_built + style (no API cost, instant)
  2. Gemini pass — only for rows that rules couldn't resolve (~10%)

Usage:
    python backend/scripts/enrich_materials.py [--rules-only] [--gemini-only]

Requires env vars: SUPABASE_URL, SUPABASE_KEY (service key)
Gemini pass also requires: GEMINI_API_KEY
"""

import os
import sys
import time
import argparse
import httpx

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

PAGE_SIZE = 500
DELAY_BETWEEN_GEMINI_CALLS = 6.0  # Free tier: 15 RPM


# ---------------------------------------------------------------------------
# Rule-based material inference
# ---------------------------------------------------------------------------

# NYC bldgclass prefix → building type context
# https://www.nyc.gov/assets/finance/jump/hlpbldgcode.html
BLDGCLASS_TYPE = {
    "A": "one_family_residential",
    "B": "two_family_residential",
    "C": "walk_up_apartment",
    "D": "elevator_apartment",
    "E": "warehouse_factory",
    "F": "factory_industrial",
    "G": "garage",
    "H": "hotel",
    "I": "healthcare",
    "J": "theatre",
    "K": "store_retail",
    "L": "loft",
    "M": "religious",
    "N": "asylums",
    "O": "office",
    "P": "public_assembly",
    "Q": "outdoor_recreational",
    "R": "condo",
    "S": "mixed_use",
    "T": "transportation",
    "U": "utility",
    "V": "vacant",
    "W": "education",
    "Y": "government",
    "Z": "misc",
}

# Style keywords → strong material signal
STYLE_MATERIAL_MAP = [
    # Cast iron / early commercial
    (["cast iron"], "Cast Iron"),
    # Art deco FIRST — before colonial revival catches "simplified colonial revival or art deco"
    (["art deco", "art moderne", "simplified colonial revival or art deco"], "Brick and Terra Cotta"),
    # Brownstone era
    (["romanesque revival", "neo-grec", "italianate"], "Brownstone"),
    # Terra cotta heavy styles
    (["beaux-arts", "french renaissance", "baroque"], "Limestone and Terra Cotta"),
    # Gothic
    (["gothic revival", "collegiate gothic", "gothic"], "Limestone"),
    # Renaissance brick
    (["renaissance revival", "roman brick", "neo-renaissance"], "Brick"),
    # Classical / limestone dominant (after art deco + renaissance to avoid false matches)
    (["classical revival", "greek revival", "federal", "georgian", "colonial revival",
      "neoclassical", "neo-classical"], "Limestone"),
    # Craftsman / tudor = brick or stone
    (["tudor revival", "tudor", "jacobethan"], "Brick and Stone"),
    # Garden homes / vernacular brick
    (["anglo-american garden home", "storybook", "english cottage",
      "dutch colonial revival"], "Brick"),
    # Modernist / international = concrete or glass
    (["international style", "modernist", "brutalist", "brutalism",
      "expressionist"], "Concrete"),
    # Glass curtain wall
    (["high-tech", "corporate modernism", "postmodern", "post-modern",
      "neo-futurist", "deconstructivist"], "Glass and Steel"),
    # Loft / industrial
    (["industrial", "neo-industrial", "daylight factory"], "Brick"),
]


# Architects with strongly predictable material signatures
# Format: (substring_to_match_in_architect_name, material)
ARCHITECT_MATERIAL_MAP = [
    # Limestone / Beaux-Arts hotel architects
    ("hardenbergh", "Limestone"),           # Plaza, Dakota, Waldorf
    ("mckim, mead", "Limestone"),           # Penn Station, Columbia
    ("carrere & hastings", "Limestone"),    # NY Public Library, Frick
    ("john russell pope", "Limestone"),     # Customs House
    ("cass gilbert", "Limestone"),          # Woolworth (Gothic limestone)
    ("richard morris hunt", "Limestone"),   # Metropolitan Museum
    ("warren & wetmore", "Limestone"),      # Grand Central
    # Brick residential specialists
    ("george f. pelham", "Brick"),          # Prolific NYC apartment architect
    ("emery roth", "Brick"),                # San Remo, Beresford
    ("neville & bagge", "Brick"),
    ("cleverdon & putzel", "Brick"),
    ("benjamin driesler", "Brick"),
    ("slee & bryson", "Brick"),
    ("frank s. lowe", "Brick"),
    ("w. c. dickerson", "Brick"),
    ("william j. bedell", "Brick"),
    ("clarence stein", "Brick"),            # Sunnyside Gardens
    ("henry j. hardenbergh", "Limestone"),
    # Modernist / concrete / glass
    ("skidmore, owings", "Glass and Steel"),  # SOM
    ("som", "Glass and Steel"),
    ("mies van der rohe", "Glass and Steel"),
    ("eero saarinen", "Concrete"),
    ("i.m. pei", "Concrete"),
    ("philip johnson", "Glass and Steel"),
    ("kevin roche", "Concrete"),
    ("paul rudolph", "Concrete"),           # Brutalist
    ("marcel breuer", "Concrete"),
    ("le corbusier", "Concrete"),
    # Cast iron era
    ("james bogardus", "Cast Iron"),
    ("daniel badger", "Cast Iron"),
]


def infer_by_rules(style: str, year_built: str, bldgclass: str, architect: str = "") -> str | None:
    """
    Return a material string if rules can determine it confidently, else None.
    """
    style_lower = (style or "").lower().strip()
    architect_lower = (architect or "").lower().strip()
    bldgclass_prefix = (bldgclass or "")[:1].upper()

    try:
        year = int(float(year_built)) if year_built else 0
    except (ValueError, TypeError):
        year = 0

    # 1. Style-based rules (highest confidence)
    if style_lower and style_lower not in ("not determined", "nd", "unknown", ""):
        for keywords, material in STYLE_MATERIAL_MAP:
            if any(kw in style_lower for kw in keywords):
                return material

    # 2. Architect-based rules (strong signal even when style is "not determined")
    if architect_lower and architect_lower not in ("not determined", "nd", "unknown", ""):
        for substring, material in ARCHITECT_MATERIAL_MAP:
            if substring in architect_lower:
                return material

    # 3. Era + building type rules
    if year > 0:
        # Pre-1840: wood frame or brick (mostly residential)
        if year < 1840:
            if bldgclass_prefix in ("A", "B", "C"):
                return "Wood Frame"
            return "Brick"

        # 1840–1880: brownstone / brick era
        if 1840 <= year < 1880:
            if bldgclass_prefix in ("A", "B", "C"):
                return "Brownstone"
            if bldgclass_prefix in ("K", "L", "E", "F"):
                return "Cast Iron"
            return "Brick"

        # 1880–1940: the great brick era for NYC residential
        if 1880 <= year < 1940:
            if bldgclass_prefix in ("A", "B", "C", "D", "S", "R"):
                return "Brick"
            if bldgclass_prefix in ("O", "H", "K"):
                return "Brick and Limestone"
            if bldgclass_prefix in ("E", "F", "L"):
                return "Brick"
            if bldgclass_prefix in ("M", "W", "Y"):
                return "Limestone"

        # 1940–1970: concrete/brick transition
        if 1940 <= year < 1970:
            if bldgclass_prefix in ("D", "C"):
                return "Brick"
            if bldgclass_prefix in ("O", "H"):
                return "Concrete"
            if bldgclass_prefix in ("E", "F"):
                return "Concrete"

        # 1970+: concrete and glass era
        if year >= 1970:
            if bldgclass_prefix in ("O", "H"):
                return "Glass and Steel"
            if bldgclass_prefix in ("D", "C", "R"):
                return "Concrete"
            if bldgclass_prefix in ("E", "F"):
                return "Concrete"

    return None


# ---------------------------------------------------------------------------
# Gemini inference (for rows rules couldn't resolve)
# ---------------------------------------------------------------------------

def build_prompt(row: dict) -> str:
    style = row.get("style") or "unknown"
    year = str(row.get("year_built") or "unknown").replace(".0", "")
    architect = row.get("architect") or "unknown"
    name = row.get("building_name") or "unknown"
    bldgclass = row.get("bldgclass") or "unknown"
    borough = row.get("borough") or "unknown"
    return (
        f"NYC building: Style={style}, Year={year}, Architect={architect}, "
        f"Name={name}, BuildingClass={bldgclass}, Borough={borough}. "
        "What is the primary exterior building material? "
        "Reply with ONLY a short phrase: 'Brick', 'Limestone', 'Concrete', "
        "'Glass and Steel', 'Brownstone', 'Cast Iron', 'Terra Cotta', "
        "'Brick and Limestone', 'Brick and Terra Cotta', 'Wood Frame', "
        "'Steel and Glass', or similar. "
        "If truly unknown reply: UNKNOWN"
    )


def call_gemini(prompt: str) -> str | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 20, "temperature": 0.1},
    }
    for attempt in range(3):
        resp = httpx.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=15,
        )
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  [429] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        raise Exception("Gemini rate limit exceeded after 3 retries")
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.upper() == "UNKNOWN" or not text:
            return None
        return text
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

REST_URL = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}
TABLE = "buildings_full_merge_scanning"


def fetch_rows(offset: int, limit: int) -> list:
    resp = httpx.get(
        f"{REST_URL}/{TABLE}",
        headers=HEADERS,
        params={
            "select": "bin,building_name,style,year_built,architect,bldgclass,borough",
            "or": "(mat_prim.is.null,mat_prim.eq.,mat_prim.eq.unknown,mat_prim.eq.nd)",
            "offset": offset,
            "limit": limit,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_row(bin_val: str, material: str):
    # Queued — flushed in bulk via flush_updates()
    _pending_updates.setdefault(material, []).append(bin_val)


_pending_updates: dict = {}  # material → [bin_val, ...]


def flush_updates():
    """Send one PATCH per unique material value covering all queued BINs."""
    for material, bins in _pending_updates.items():
        # PostgREST supports IN filter: ?bin=in.(a,b,c)
        in_clause = "(" + ",".join(bins) + ")"
        resp = httpx.patch(
            f"{REST_URL}/{TABLE}",
            headers=HEADERS,
            params={"bin": f"in.{in_clause}"},
            json={"mat_prim": material},
            timeout=30,
        )
        resp.raise_for_status()
    _pending_updates.clear()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true", help="Skip Gemini, only apply rules")
    parser.add_argument("--gemini-only", action="store_true", help="Skip rules, only use Gemini")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    args = parser.parse_args()

    offset = 0
    rules_count = 0
    gemini_count = 0
    skipped_count = 0

    while True:
        rows = fetch_rows(offset, PAGE_SIZE)
        if not rows:
            break

        print(f"Batch offset={offset}: {len(rows)} rows", end="", flush=True)

        for row in rows:
            bin_val = row.get("bin")
            if not bin_val:
                continue

            # --- Rule-based pass ---
            if not args.gemini_only:
                material = infer_by_rules(
                    row.get("style"), row.get("year_built"), row.get("bldgclass"), row.get("architect")
                )
                if material:
                    if not args.dry_run:
                        update_row(bin_val, material)  # queued
                    else:
                        print(f"\n  [DRY rules] BIN {bin_val}: {material}")
                    rules_count += 1
                    continue

            # --- Gemini pass ---
            if args.rules_only:
                skipped_count += 1
                continue

            if not GEMINI_API_KEY:
                skipped_count += 1
                continue

            prompt = build_prompt(row)
            try:
                material = call_gemini(prompt)
            except Exception as e:
                print(f"\n  [SKIP] BIN {bin_val}: Gemini error — {e}")
                skipped_count += 1
                continue

            if not material:
                skipped_count += 1
                continue

            if not args.dry_run:
                try:
                    update_row(bin_val, material)  # queued
                    gemini_count += 1
                except Exception as e:
                    print(f"\n  [ERR] BIN {bin_val}: update failed — {e}")
            else:
                print(f"\n  [DRY gemini] BIN {bin_val}: {material}")
                gemini_count += 1

            time.sleep(DELAY_BETWEEN_GEMINI_CALLS)

        # Flush all queued updates for this batch in bulk (one request per material)
        if not args.dry_run:
            flush_updates()
            print(f" → flushed", flush=True)
        else:
            print()

        offset += PAGE_SIZE

    print(f"\nDone.")
    print(f"  Rules resolved:  {rules_count}")
    print(f"  Gemini resolved: {gemini_count}")
    print(f"  Skipped/unknown: {skipped_count}")
    print(f"  Estimated Gemini cost: ${gemini_count * 0.000075:.4f}")


if __name__ == "__main__":
    main()

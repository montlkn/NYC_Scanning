#!/usr/bin/env python3
"""
Batch-embed curated buildings into the Railway `building_search_index`.

Pulls the curated set from Supabase (`buildings_full_merge_scanning`), builds a
descriptive text per building, embeds it with the SAME bge-small model the
/search query path uses (services.text_embeddings), and upserts BIN → vector +
filter columns into Railway. Idempotent: re-running updates rows; pass
--rebuild to re-embed everything, otherwise only rows missing from the index
are processed.

Env:
  DATABASE_URL         Supabase Postgres (source of truth)         [required]
  SEARCH_DB_URL    Railway Postgres (the search index lives here) [required]

Usage:
  python scripts/embed_buildings.py --dry-run        # preview text + counts, no writes
  python scripts/embed_buildings.py                  # embed rows not yet indexed
  python scripts/embed_buildings.py --rebuild        # re-embed all
  python scripts/embed_buildings.py --limit 500      # cap (testing)
"""

import argparse
import logging
import os
import sys

import psycopg
from psycopg.rows import dict_row

# Allow `from services...` when run as a script from backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load backend/.env so DATABASE_URL / SEARCH_DB_URL resolve when run directly
# (mirrors how the FastAPI app loads settings — no manual `export` needed).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from services.text_embeddings import embed_texts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("embed_buildings")

SOURCE_COLUMNS = (
    "bin, bbl, building_name, wiki_name, address, architect, style, style_secondary, "
    "building_type, use_original, year_built, era, borough_name, historic_district, "
    "landmark, mat_primary, colloquial_names_text, primary_aesthetic, secondary_aesthetic, "
    "storytelling, geocoded_lat, geocoded_lng"
)


# Placeholder values the curated data uses for "we don't know" — embedding these
# pollutes the vector with meaningless tokens, so we drop them from the text.
_JUNK_VALUES = {
    "not determined", "undetermined", "unknown", "n/a", "na", "none",
    "not applicable", "not available", "tbd", "unspecified", "-", "0",
    "nd", "n.d.", "null", "nan", "none determined", "no data",
}

# Sentinel BINs the curated set reuses for non-building sites (cemeteries,
# archaeological sites). They collide on upsert (ON CONFLICT bin), so skip them.
_PLACEHOLDER_BINS = {"0", "5000000", "1000000", "2000000", "3000000", "4000000"}


def _clean(v) -> str:
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in _JUNK_VALUES else s


def _parse_int(v):
    try:
        return int(float(str(v))) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _parse_float(v):
    try:
        return float(str(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


# Vernacular gloss — maps the curated style/type vocabulary onto the everyday
# words people actually search with ("brownstone", "prewar", "loft"). Baked into
# the embedded text so the vector already knows them; no query-time mapping. Each
# entry: substrings that must appear in `style`/`building_type` (lowercased) →
# colloquial terms to emit. This is a linguistic gloss, NOT data — the only
# hardcoding allowed here. Keyed off the actual populated values in the source
# table (style: italianate/neo-grec/renaissance revival…; building_type: row
# house/tenement/store and lofts/church…).
_VERNACULAR_GLOSS = (
    # (style_substrings, type_substrings, glosses)
    (("italianate", "neo-grec", "greek revival", "renaissance revival",
      "queen anne", "second empire", "romanesque"),
     ("row house", "town house", "tenement", "flats", "two-family"),
     "brownstone prewar rowhouse walk-up classic New York townhouse"),
    (("art deco", "deco"), (), "deco jazz-age streamline"),
    (("modern", "international", "brutalist", "minimalist"), (),
     "modernist contemporary"),
    ((), ("store and loft", "loft", "warehouse", "factory", "manufacturing"),
     "loft warehouse industrial cast-iron"),
    ((), ("church", "synagogue", "chapel", "cathedral", "temple", "mosque"),
     "place of worship religious sacred"),
    ((), ("apartment", "flats", "tenement", "model tenement"),
     "apartment residential"),
    ((), ("school", "library", "courthouse", "fire station", "post office",
          "hospital", "police"),
     "civic institutional public"),
)


def _vernacular(style: str, btype: str) -> str:
    """Everyday synonyms derived from the curated style/type, joined to a string."""
    s, t = style.lower(), btype.lower()
    out = []
    for style_subs, type_subs, gloss in _VERNACULAR_GLOSS:
        if (style_subs and any(x in s for x in style_subs)) or \
           (type_subs and any(x in t for x in type_subs)):
            out.append(gloss)
    return " ".join(out)


def _display_name(row: dict) -> str:
    """Proper name, falling back to wiki_name (89% of rows have only wiki_name)."""
    name = _clean(row.get("building_name"))
    if name and name != "0":
        return name
    return _clean(row.get("wiki_name"))


def build_text(row: dict) -> str:
    """Compose the descriptive string we embed as natural prose, by salience.

    Weaves in the ~99%-populated structured fields the old spec-sheet version
    ignored (style, type, neighborhood, era, archetype, materials, colloquial
    names) plus a vernacular gloss, so atmospheric/feature/neighborhood queries
    resolve instead of falling back to a vague style match.
    """
    name = _display_name(row)
    style = _clean(row.get("style"))
    style2 = _clean(row.get("style_secondary"))
    btype = _clean(row.get("building_type")) or _clean(row.get("use_original"))
    borough = _clean(row.get("borough_name"))
    histdist = _clean(row.get("historic_district"))
    architect = _clean(row.get("architect"))
    year = _parse_int(row.get("year_built"))
    era = _clean(row.get("era"))
    material = _clean(row.get("mat_primary"))
    colloquial = _clean(row.get("colloquial_names_text"))
    story = _clean(row.get("storytelling"))

    # Lead clause: "{name}, a {style} {type} in {borough} {historic district}".
    lead = []
    if name:
        lead.append(name)
    descriptor = " ".join(x for x in (style, f"{btype}" if btype else "") if x).strip()
    if descriptor:
        article = "an" if descriptor[:1].lower() in "aeiou" else "a"
        place = ""
        if borough:
            place = f" in {borough}"
            if histdist:
                place += f"'s {histdist}"
        elif histdist:
            place = f" in the {histdist}"
        lead.append(f"{article} {descriptor}{place}")
    parts = [", ".join(lead)] if lead else []

    if style2:
        parts.append(f"with {style2} elements")
    if architect:
        parts.append(f"designed by {architect}")
    if year:
        parts.append(f"built {year}")
    elif era:
        parts.append(f"from the {era} era")
    if material:
        parts.append(f"built of {material}")
    if colloquial:
        parts.append(f"also known as {colloquial}")

    vern = _vernacular(style, btype)
    if vern:
        parts.append(vern)

    for key in ("primary_aesthetic", "secondary_aesthetic"):
        val = _clean(row.get(key))
        if val:
            parts.append(f"{val} character")

    # Keep storytelling LAST so generated lore (Item 4) flows in unchanged.
    if story:
        parts.append(story)

    return ". ".join(p for p in parts if p)


def build_snippet(row: dict) -> str:
    name = _display_name(row) or _clean(row.get("address"))
    style = _clean(row.get("style"))
    return f"{name} — {style}" if style else name


def fetch_source_rows(supa_url: str, limit, rebuild: bool, indexed_bins: set) -> list:
    with psycopg.connect(supa_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        sql = f"SELECT {SOURCE_COLUMNS} FROM buildings_full_merge_scanning WHERE bin IS NOT NULL"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bin_clean = str(r.get("bin") or "").strip().removesuffix(".0")
        if not bin_clean or bin_clean in _PLACEHOLDER_BINS:
            continue
        if not rebuild and bin_clean in indexed_bins:
            continue
        r["_bin"] = bin_clean
        out.append(r)
    return out


def load_indexed_bins(rail_url: str) -> set:
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT bin FROM building_search_index")
        return {row[0] for row in cur.fetchall()}


def upsert(rail_url: str, batch: list):
    """batch: list of (bin, bbl, text, snippet, vec_literal, year, is_landmark, lat, lng)."""
    with psycopg.connect(rail_url) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO building_search_index
                (bin, bbl, text, snippet, embedding, year_built, is_landmark, lat, lng, updated_at)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, now())
            ON CONFLICT (bin) DO UPDATE SET
                bbl = EXCLUDED.bbl, text = EXCLUDED.text, snippet = EXCLUDED.snippet,
                embedding = EXCLUDED.embedding, year_built = EXCLUDED.year_built,
                is_landmark = EXCLUDED.is_landmark, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                updated_at = now()
            """,
            batch,
        )
        conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-embed all rows")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=128)
    args = ap.parse_args()

    supa_url = os.environ.get("DATABASE_URL")
    rail_url = os.environ.get("SEARCH_DB_URL")
    if not supa_url or not rail_url:
        logger.error("DATABASE_URL and SEARCH_DB_URL must both be set")
        sys.exit(1)

    indexed = set() if args.rebuild else load_indexed_bins(rail_url)
    logger.info(f"{len(indexed)} BINs already indexed")

    rows = fetch_source_rows(supa_url, args.limit, args.rebuild, indexed)
    logger.info(f"{len(rows)} buildings to embed")
    if not rows:
        return

    if args.dry_run:
        for r in rows[:5]:
            logger.info(f"  [{r['_bin']}] {build_text(r)[:160]}")
        logger.info("dry-run: no writes")
        return

    total = 0
    for i in range(0, len(rows), args.batch_size):
        chunk = rows[i : i + args.batch_size]
        texts = [build_text(r) for r in chunk]
        vectors = embed_texts(texts)
        batch = []
        for r, txt, vec in zip(chunk, texts, vectors):
            landmark = _clean(r.get("landmark"))
            batch.append((
                r["_bin"],
                _clean(r.get("bbl")).removesuffix(".0") or None,
                txt,
                build_snippet(r),
                "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                _parse_int(r.get("year_built")),
                bool(landmark and landmark != "0"),
                _parse_float(r.get("geocoded_lat")),
                _parse_float(r.get("geocoded_lng")),
            ))
        upsert(rail_url, batch)
        total += len(batch)
        logger.info(f"  upserted {total}/{len(rows)}")

    logger.info(f"✅ done — {total} buildings embedded into building_search_index")


if __name__ == "__main__":
    main()

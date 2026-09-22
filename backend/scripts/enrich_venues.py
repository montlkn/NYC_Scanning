"""
Enrich every venue with WHERE it is and WHETHER it is a place, then re-embed.

Replaces reembed_venues_with_building.py, which only reached the 33,809 venues
whose BIN joins the curated building index. This covers all 292k.

Per venue:
  in_nyc        point-in-polygon against NYC's official NTA 2020 boundaries
                (the same names building_search_index.neighborhood uses), with
                a ~150m snap for piers and waterfront rows that sit just off
                the shoreline-clipped polygons. The ingest bbox was a
                rectangle, and a rectangle around NYC holds Fort Lee, Hoboken
                and Palisades Park: "seagram bar" returned Korean bars in NJ.
  borough,
  neighborhood  from the same polygon. "modernist bars in midtown" could not
                match before, because 10,222 of 292k venue texts named a
                neighborhood.
  category      Overture's raw slugs ("cocktail_bar") normalised to the FSQ
                display form ("Cocktail Bar"). 48,133 rows. The POI category
                boost tokenises on word characters, and "_" is one, so
                "cocktail_bar" never matched "bar".
  searchable    see is_searchable() -- hides non-places without deleting them.
  lex_text      name + category + host building + neighborhood + borough, for
                the trigram pool. It read name||snippet, so "seagram bar"
                could never lexically reach The Bar, which sits in the Seagram
                Building and says so nowhere in its name.
  text/embedding rebuilt with the same fields, only where the text changed.

Resumable: a re-run skips rows whose text already matches.

Run: python -m scripts.enrich_venues [--dry-run] [--limit N] [--no-embed]
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import sys
import time
import urllib.request

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.text_embeddings import MODEL_NAME  # noqa: E402
from scripts.ingest_overture_places import DROP, RESCUE, wanted  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("enrich-venues")

NTA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "geo", "nta2020.geojson")
NTA_URL = "https://data.cityofnewyork.us/api/geospatial/9nt8-h7nd?method=export&format=GeoJSON"
# ~150m. The NTA polygons are clipped to the shoreline, so a pier bar or a
# riverfront park kiosk sits just outside every polygon. The Hudson is >1km
# wide wherever NJ faces Manhattan, so this cannot pull in Hoboken.
SNAP_DEG = 0.0015

BATCH = 2000
EMBED_THREADS = int(os.environ.get("EMBED_THREADS", str(os.cpu_count() or 4)))

# FSQ "Structure" is where FSQ files a building it knows nothing about: 14,105
# rows of offices, car services and apartment buildings named by address
# ("1204 Broadway"). "Neighborhood" rows are the neighborhood itself. Neither
# is somewhere you go, and both are covered by the buildings corpus.
NON_PLACE_CATEGORIES = frozenset({"structure", "neighborhood"})
# A legal-entity suffix means the row came from a business registry, not a
# storefront: "375 Park Food Llc", "Parm Fund Llc", "Major Management Tcz Llc"
# all sit in the Seagram Building as "restaurant".
_LEGAL_ENTITY_RE = re.compile(r"[\s,](llc|l\.l\.c|inc|corp|corporation|ltd|lp|pllc)\.?$", re.I)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# A web domain is not a venue name: "Elove.com" was filed as a Bar inside the
# Graybar Building and led "art deco bar".
_DOMAIN_NAME_RE = re.compile(r"^\S+\.(com|net|org|io|co|nyc|biz|info|us)$", re.I)
_SMALL_WORDS = frozenset({"and", "of", "or", "the", "for", "a", "an", "to", "in"})


def normalize_category(cat: str | None) -> str | None:
    """'cocktail_bar' -> 'Cocktail Bar'. Leaves FSQ's already-cased labels alone."""
    if not cat:
        return cat
    if "_" not in cat and not cat.islower():
        return cat
    words = cat.replace("_", " ").split()
    return " ".join(
        w if (i and w in _SMALL_WORDS) else w[:1].upper() + w[1:]
        for i, w in enumerate(words)
    )


def _slug(cat: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", cat.lower()).strip("_")


def is_searchable(name: str | None, category: str | None, domains: tuple, in_nyc: bool,
                  source: str | None = None) -> bool:
    if not in_nyc:
        return False
    n = (name or "").strip()
    c = (category or "").strip()
    nl, cl = n.lower(), c.lower()
    if not n or not _HAS_LETTER_RE.search(n):
        return False  # "414", "40-10": a number is not a name
    if cl in NON_PLACE_CATEGORIES:
        return False
    if nl == cl or cl.endswith(" " + nl):
        return False  # named for its own category: "Restaurant" (Mexican Restaurant)
    if _LEGAL_ENTITY_RE.search(n) or _DOMAIN_NAME_RE.match(n):
        return False
    slug = _slug(c)
    # One destination rule for both sources: the Overture ingest's errand
    # list (grocery, pharmacy, car dealer, salon, agency...) now applies to
    # FSQ rows too, and Overture rows must pass its keep-list, which was
    # substring-matched when they were ingested (see ingest_overture_places).
    if slug and DROP.search(slug) and not RESCUE.search(slug):
        return False
    if source == "overture" and slug and not wanted(slug):
        return False
    # Pure B2B: law offices, marketing agencies, IT services. Only when that is
    # the row's ONLY domain -- a bar that also books events keeps its place.
    if domains and set(domains) == {"Business and Professional Services"}:
        return False
    return True


def base_parts(text: str) -> list:
    """The leading 'Name. Category. Address' of a venue text, without any
    provenance clauses a previous build appended. Clauses all start with
    'in ' or 'designed by ', and never come first."""
    out = []
    for p in (text or "").split(". "):
        if out and (p.startswith("in ") or p.startswith("designed by ")):
            break
        out.append(p)
    return out


def build_text(parts: list, raw_cat: str | None, cat: str | None, host: str,
               byear, style: str, hood: str, boro: str, arch: str) -> str:
    parts = list(parts)
    if len(parts) > 1 and raw_cat and parts[1] == raw_cat and cat:
        parts[1] = cat
    name = parts[0] if parts else ""
    if host and host.lower() not in name.lower():
        parts.append(f"in the {host}")
    if byear and style:
        parts.append(f"in a {byear} {style} building")
    elif byear:
        parts.append(f"in a {byear} building")
    elif style:
        parts.append(f"in a {style} building")
    place = ", ".join(x for x in (hood, boro) if x)
    if place:
        parts.append(f"in {place}")
    if arch and arch.lower() not in ("not determined", "unknown"):
        parts.append(f"designed by {arch}")
    return ". ".join(parts)


def load_ntas():
    from shapely.geometry import shape
    if not os.path.exists(NTA_PATH):
        os.makedirs(os.path.dirname(NTA_PATH), exist_ok=True)
        log.info("downloading NTA 2020 boundaries")
        urllib.request.urlretrieve(NTA_URL, NTA_PATH)
    with open(NTA_PATH) as f:
        feats = json.load(f)["features"]
    geoms = [shape(ft["geometry"]) for ft in feats]
    props = [(ft["properties"]["ntaname"], ft["properties"]["boroname"]) for ft in feats]
    return geoms, props


def locate(lats: np.ndarray, lngs: np.ndarray):
    """(neighborhood, borough) per point, or (None, None) outside NYC."""
    import shapely
    from shapely.strtree import STRtree
    geoms, props = load_ntas()
    tree = STRtree(geoms)
    pts = shapely.points(lngs, lats)
    hood = [None] * len(pts)
    boro = [None] * len(pts)
    inside = tree.query(pts, predicate="within")
    for pi, gi in zip(*inside):
        if hood[pi] is None:
            hood[pi], boro[pi] = props[gi]
    missing = np.array([i for i, h in enumerate(hood) if h is None], dtype=int)
    if len(missing):
        near = tree.query_nearest(pts[missing], max_distance=SNAP_DEG)
        for mi, gi in zip(*near):
            pi = missing[mi]
            if hood[pi] is None:
                hood[pi], boro[pi] = props[gi]
    return hood, boro


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-embed", action="store_true", help="attributes + lex_text only")
    ap.add_argument("--flags-only", action="store_true",
                    help="rewrite only `searchable`, only where it changed (safe beside a running embed)")
    a = ap.parse_args()
    return run(dry_run=a.dry_run, limit=a.limit, no_embed=a.no_embed, flags_only=a.flags_only)


def run(*, dry_run: bool = False, limit: int | None = None,
        no_embed: bool = False, flags_only: bool = False) -> int:
    """Called by the ingest scripts when they finish, so a new import can't
    reach search un-enriched: NJ rows visible, no neighborhood, raw Overture
    slugs. See docs/SEARCH_RUNBOOK.md."""
    args = argparse.Namespace(dry_run=dry_run, limit=limit, no_embed=no_embed, flags_only=flags_only)
    t0 = time.time()

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT v.fsq_id, v.name, v.category, v.category_domains, v.text,
                   v.lat, v.lng, v.building_year, v.building_style,
                   b.snippet, b.architect, v.source, v.searchable
              FROM venues v
              LEFT JOIN building_search_index b ON b.bin = v.bin
             ORDER BY v.fsq_id
             {'LIMIT ' + str(int(args.limit)) if args.limit else ''}
        """)
        rows = cur.fetchall()
    log.info("%d venues loaded (%.0fs)", len(rows), time.time() - t0)

    # Overture rows carry no FSQ domain. Borrow it from FSQ rows with the same
    # normalised category, by majority, so the B2B rule applies to both.
    votes: dict = collections.defaultdict(collections.Counter)
    for r in rows:
        if r[3]:
            votes[(normalize_category(r[2]) or "").lower()][tuple(sorted(r[3]))] += 1
    domain_of = {k: c.most_common(1)[0][0] for k, c in votes.items()}

    lats = np.array([r[5] if r[5] is not None else np.nan for r in rows])
    lngs = np.array([r[6] if r[6] is not None else np.nan for r in rows])
    hoods, boros = locate(lats, lngs)
    log.info("located (%.0fs)", time.time() - t0)

    out = []
    stats = collections.Counter()
    for r, hood, boro in zip(rows, hoods, boros):
        fsq_id, name, raw_cat, domains, text, _lat, _lng, byear, bstyle, bsnip, arch, source, was_searchable = r
        cat = normalize_category(raw_cat)
        in_nyc = hood is not None
        doms = tuple(domains) if domains else domain_of.get((cat or "").lower(), ())
        searchable = is_searchable(name, cat, doms, in_nyc, source)
        stats["flag_changed"] += (was_searchable is not None and was_searchable != searchable)
        host = (bsnip or "").split("—")[0].strip()
        if host and (host[:1].isdigit() or host.lower() in (name or "").lower()):
            host = ""  # an address-only building "name" adds nothing
        new_text = build_text(base_parts(text), raw_cat, cat, host, byear,
                              bstyle or "", hood or "", boro or "", arch or "")
        lex = " ".join(x for x in (name, cat, host, hood, boro) if x).lower()
        stats["in_nyc"] += in_nyc
        stats["searchable"] += searchable
        stats["cat_changed"] += cat != raw_cat
        stats["text_changed"] += new_text != text
        out.append((fsq_id, in_nyc, boro, hood, searchable, cat, lex,
                    new_text if new_text != text else None, was_searchable))

    log.info("stats: %s of %d", dict(stats), len(out))
    if args.dry_run:
        for o in out[:: max(1, len(out) // 12)][:12]:
            log.info("  %s | nyc=%s search=%s | %s", o[0], o[1], o[4], (o[7] or "(unchanged)")[:120])
        return 0

    if args.flags_only:
        changed = [(o[0], o[4]) for o in out if o[8] != o[4]]
        log.info("%d searchable flags change", len(changed))
        with conn.cursor() as cur:
            for i in range(0, len(changed), 500):
                psycopg2.extras.execute_values(
                    cur,
                    "UPDATE venues v SET searchable = d.s FROM (VALUES %s) AS d(id, s) WHERE v.fsq_id = d.id",
                    changed[i:i + 500], page_size=500,
                )
                conn.commit()
        return 0

    # Pass 1: attributes, no embedding -- fast, and makes the filter live.
    with conn.cursor() as cur:
        for i in range(0, len(out), BATCH):
            psycopg2.extras.execute_values(
                cur,
                "UPDATE venues v SET in_nyc = d.n, borough = d.b, neighborhood = d.h,"
                " searchable = d.s, category = d.c, lex_text = d.l"
                " FROM (VALUES %s) AS d(id, n, b, h, s, c, l) WHERE v.fsq_id = d.id",
                [o[:7] for o in out[i:i + BATCH]],
                page_size=BATCH,
            )
            conn.commit()
    log.info("attributes written (%.0fs)", time.time() - t0)
    if args.no_embed:
        return 0

    # Pass 2: re-embed only what changed, and only what search can return.
    todo = [(o[0], o[7]) for o in out if o[7] is not None and o[4]]
    log.info("%d texts to re-embed with %d threads", len(todo), EMBED_THREADS)
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=MODEL_NAME, threads=EMBED_THREADS)
    done = 0
    with conn.cursor() as cur:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            vecs = [v.tolist() for v in model.embed([c[1] for c in chunk], batch_size=64)]
            psycopg2.extras.execute_values(
                cur,
                "UPDATE venues v SET text = d.t, embedding = d.e::vector, updated_at = now()"
                " FROM (VALUES %s) AS d(id, t, e) WHERE v.fsq_id = d.id",
                [(f, t, str(vec)) for (f, t), vec in zip(chunk, vecs)],
                page_size=500,
            )
            conn.commit()
            done += len(chunk)
            rate = done / max(1e-6, time.time() - t0)
            log.info("  %d/%d (%.0f/s)", done, len(todo), rate)
    conn.close()
    log.info("done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

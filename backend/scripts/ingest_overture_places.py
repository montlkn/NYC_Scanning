"""
Ingest Overture Maps places into `venues`, category-filtered.

WHY NOT ALL OF IT: Overture has 533,473 places in the NYC bbox against our
225,810 Foursquare rows, and the gap is real -- we carry 10 dentists, 9 dry
cleaners and 24 post offices. But its biggest categories are
health_and_medical (14,071), real_estate_agent (3,270) and diagnostic_services
(3,237). Taking the lot would turn "near me" into a business directory.

So the filter is not about data quality, it is about whether a place is a
DESTINATION or an ERRAND. A cocktail bar in a 1926 Art Deco building is
content; a dentist is an appointment. Measured on the 2026-08-19.0 release at
confidence >= 0.9: 66 categories kept (73,235 rows), 172 dropped (171,217).

Two things a naive ingest would get wrong:

  * Overture is partly Foursquare-derived, so a chunk of it is already in
    `venues`. Deduped on normalized name within 50m -- the same rule the iOS
    client uses for Apple POI, so behaviour stays consistent across surfaces.

  * A venue row without a BIN is just a pin. The building join is the moat:
    it is what makes "moody art deco cocktail bar" work at all, because
    build_text folds the host building's year and style into the embedding.

Every row is written with source='overture' so the whole ingest is one
DELETE away from reverted.

Run: python -m scripts.ingest_overture_places [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.text_embeddings import MODEL_NAME  # noqa: E402
from scripts.seed_venues import (  # noqa: E402
    build_text,
    join_buildings_via_footprints,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("overture")

RELEASE = "2026-08-19.0"
SRC = (f"read_parquet('s3://overturemaps-us-west-2/release/{RELEASE}"
       "/theme=places/type=place/*', hive_partitioning=1)")
# NYC + a margin.
BBOX = ("bbox.xmin BETWEEN -74.30 AND -73.68 AND "
        "bbox.ymin BETWEEN  40.48 AND  40.93")
MIN_CONFIDENCE = 0.9
DEDUPE_M = 50.0
BATCH = 2048
EMBED_THREADS = int(os.environ.get("EMBED_THREADS", "4"))

# Destination-shaped. Matched against Overture's `categories.primary`.
KEEP = re.compile(
    r"(restaurant|_bar$|^bar$|pub|brewery|brewpub|winery|distillery|cocktail|"
    r"cafe|coffee|tea_|bakery|patisserie|deli|ice_cream|dessert|juice|"
    r"museum|gallery|theat|cinema|music|concert|nightclub|night_club|dance|"
    r"comedy|performing|art_|arts_|"
    r"landmark|historic|monument|memorial|church|cathedral|synagogue|temple|mosque|"
    r"park|garden|plaza|square|pier|beach|trail|scenic|observation|zoo|aquarium|"
    r"book|record|vintage|antique|thrift|flea|craft|hobby|game|toy|"
    r"hotel|hostel|inn$|bed_and|"
    r"market|farmers|butcher|cheese|chocolate|wine_|liquor|"
    r"library|bookstore|stadium|arena|bowling|arcade|"
    r"tattoo|florist|furniture|design|architect)")
# Errands. Checked second, so it wins a tie ("medical_museum" stays out).
DROP = re.compile(
    r"(health|medical|dentist|diagnostic|physical_therapy|hospital|clinic|"
    r"pharmacy|veterinar|"
    r"real_estate|professional_services|lawyer|corporate|contractor|financial|"
    r"bank|insurance|accounting|"
    r"salon|barber|nail|spa_|massage|"
    r"automotive|gas_station|car_|auto_|parking|"
    r"laundromat|dry_clean|storage|moving|shipping|logistics|"
    r"grocery|supermarket|convenience|mobile_phone|hardware|"
    r"community_services|non_profit|government|school|education|childcare|"
    r"gym|fitness|party_and_event|advertis|recruit|staffing|employment|"
    r"telecom|utility|wholesale)")


def wanted(cat: str | None) -> bool:
    if not cat:
        return False
    return bool(KEEP.search(cat)) and not DROP.search(cat)


def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def fetch_overture(limit: int | None):
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    log.info("scanning Overture %s ...", RELEASE)
    q = f"""
        SELECT names.primary AS name,
               categories.primary AS cat,
               bbox.xmin AS lng, bbox.ymin AS lat,
               confidence
          FROM {SRC}
         WHERE {BBOX} AND confidence >= {MIN_CONFIDENCE}
           AND names.primary IS NOT NULL
           AND categories.primary IS NOT NULL
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    log.info("%d rows at confidence >= %.2f", len(rows), MIN_CONFIDENCE)
    kept = [r for r in rows if wanted(r[1])]
    log.info("%d kept after the destination filter (%.0f%% dropped)",
             len(kept), 100 * (1 - len(kept) / max(1, len(rows))))
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()

    rows = fetch_overture(args.limit)

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SELECT name, lat, lng FROM venues "
                    " WHERE lat IS NOT NULL AND lng IS NOT NULL")
        existing = cur.fetchall()

    # Dedupe on normalized name within ~50m. Bucket by name first so this is a
    # hash lookup per candidate, not 73k x 225k distance checks.
    from collections import defaultdict
    by_name = defaultdict(list)
    for n, la, lo in existing:
        by_name[norm_name(n)].append((float(la), float(lo)))

    M_PER_DEG = 111_320.0
    fresh = []
    dupes = 0
    for name, cat, lng, lat, conf in rows:
        key = norm_name(name)
        hit = False
        for ela, elo in by_name.get(key, ()):
            dy = (lat - ela) * M_PER_DEG
            dx = (lng - elo) * M_PER_DEG * 0.758   # cos(40.7 degrees)
            if (dx * dx + dy * dy) ** 0.5 <= DEDUPE_M:
                hit = True
                break
        if hit:
            dupes += 1
        else:
            fresh.append((name, cat, float(lat), float(lng), float(conf)))

    log.info("%d already in venues (name + %.0fm), %d new",
             dupes, DEDUPE_M, len(fresh))

    if args.dry_run:
        from collections import Counter
        for c, n in Counter(r[1] for r in fresh).most_common(12):
            log.info("  %-38s %6d", c, n)
        log.info("dry run: %d rows not written (%.0fs)", len(fresh), time.time() - t0)
        return 0

    # Building join. Reuses seed_venues' footprint KNN rather than a third
    # implementation -- it goes against the 1.08M DOB footprints, not the 35k
    # curated set, which is the difference between most venues having a year
    # and 79% of them having nothing.
    # Its signature expects rows shaped (_, _, lat, lng), so adapt.
    shaped = [(r[0], r[1], r[2], r[3]) for r in fresh]
    joined = join_buildings_via_footprints(
        os.environ["FOOTPRINTS_DB_URL"], os.environ["SEARCH_DB_URL"], shaped)
    log.info("%d/%d joined to a building (%.0f%%)",
             len(joined), len(fresh), 100 * len(joined) / max(1, len(fresh)))

    # Embed. Own model handle at 4 threads: measured 43 texts/s vs 24.9 at 12
    # -- ONNX oversubscribes (see scripts/embed_building_lore.py).
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=MODEL_NAME, threads=EMBED_THREADS)

    payload = []
    texts = []
    for i, (name, cat, lat, lng, conf) in enumerate(fresh):
        b = joined.get(i) or [None, None, None, ""]
        bin_, bbl, byear, bstyle = b[0], b[1], b[2], (b[3] or "")
        leaf = cat.replace("_", " ")
        texts.append(build_text(name, leaf, None, byear, bstyle))
        snippet = f"{name} — {leaf}"
        payload.append((name, cat, leaf, snippet, lat, lng, bin_, bbl, byear, bstyle))

    log.info("embedding %d texts on %d threads", len(texts), EMBED_THREADS)
    vecs = []
    for i in range(0, len(texts), BATCH):
        vecs.extend(v.tolist() for v in model.embed(texts[i:i + BATCH], batch_size=32))
        log.info("  embedded %d/%d (%.0fs)", len(vecs), len(texts), time.time() - t0)

    with conn.cursor() as cur:
        # source is what makes this whole ingest one DELETE away from reverted.
        #
        # TOOK PRODUCTION SEARCH DOWN ONCE. ADD COLUMN takes an ACCESS
        # EXCLUSIVE lock on `venues`, and the 225k-row backfill that followed
        # ran inside the same transaction for ~8 minutes -- so every search
        # query that reads venues queued behind it and the endpoint hung. The
        # lock is instant on its own; the UPDATE is what holds it.
        #
        # So: a short transaction for the DDL, a lock_timeout so it can never
        # wait on someone else, and the backfill in committed batches that
        # take only row locks. DEFAULT NULL means ADD COLUMN rewrites nothing.
        cur.execute("SET lock_timeout = '3s'")
        cur.execute("ALTER TABLE venues ADD COLUMN IF NOT EXISTS source text")
        conn.commit()

        # Batched so no single statement holds locks for long, and so killing
        # this script mid-run leaves a consistent table.
        stamped = 0
        while True:
            cur.execute("""
                UPDATE venues SET source = 'fsq'
                 WHERE fsq_id IN (
                   SELECT fsq_id FROM venues WHERE source IS NULL LIMIT 5000
                 )
            """)
            n = cur.rowcount
            conn.commit()
            stamped += n
            if n == 0:
                break
            log.info("  stamped %d existing rows as fsq", stamped)
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO venues
                (fsq_id, name, category, text, snippet, embedding,
                 lat, lng, bin, bbl, building_year, building_style, source, updated_at)
            VALUES %s
            ON CONFLICT (fsq_id) DO NOTHING
            """,
            [
                (f"ovt:{abs(hash((p[0], round(p[4], 6), round(p[5], 6)))):016x}",
                 p[0], p[1], t, p[3], str(v), p[4], p[5], p[6], p[7], p[8], p[9],
                 "overture")
                for p, t, v in zip(payload, texts, vecs)
            ],
            template="(%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s, now())",
            page_size=500,
        )
        conn.commit()
        cur.execute("SELECT coalesce(source,'(none)'), count(*) FROM venues GROUP BY 1 ORDER BY 2 DESC")
        for src, n in cur.fetchall():
            log.info("  %-10s %7d", src, n)
    conn.close()
    log.info("done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

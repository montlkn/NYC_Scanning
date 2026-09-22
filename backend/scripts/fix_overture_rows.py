"""
Repair the Overture venues: contact fields, and a better building join.

Two defects in the first ingest (scripts/ingest_overture_places.py):

  1. websites / phones were read out of Overture and then dropped on the
     floor -- 0 of 64,281 rows carry a website or tel, against 97,740 and
     145,287 on the Foursquare rows. They do not feed the embedding, so this
     is a pure column backfill with no re-embed.

  2. The building join used nearest-CENTROID within 60m and reached only
     67.5% (vs 84.9% for FSQ). That radius is the wrong instrument for a POI:
     a storefront sits on the EDGE of its building, and a large footprint's
     centroid can be well over 60m away, so the biggest buildings -- exactly
     the ones with a name and a style -- were the ones that missed.

     Point-in-polygon is the correct test and the footprint GIST index serves
     it directly, with a 25m fallback for coordinates that land in the street.

The building join is the moat: build_text folds the host building's year and
style into the venue embedding, which is what makes "art deco bar" work. A
venue without a BIN is just a pin.

Run: python -m scripts.fix_overture_rows [--dry-run] [--contacts-only]
"""
from __future__ import annotations

import argparse
import logging
import os
import time

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("fix-overture")

RELEASE = "2026-08-19.0"
SRC = (f"read_parquet('s3://overturemaps-us-west-2/release/{RELEASE}"
       "/theme=places/type=place/*', hive_partitioning=1)")
BBOX = ("bbox.xmin BETWEEN -74.30 AND -73.68 AND "
        "bbox.ymin BETWEEN  40.48 AND  40.93")
CHUNK = 4000
FALLBACK_M = 25.0


def backfill_contacts(search, dry: bool) -> None:
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    log.info("re-reading Overture for websites/phones ...")
    rows = con.execute(f"""
        SELECT names.primary AS name,
               bbox.ymin AS lat, bbox.xmin AS lng,
               websites, phones
          FROM {SRC}
         WHERE {BBOX} AND confidence >= 0.9
           AND names.primary IS NOT NULL
           AND (websites IS NOT NULL OR phones IS NOT NULL)
    """).fetchall()
    log.info("%d Overture rows carry a website or phone", len(rows))

    # Key on (name, rounded coords) -- the same identity the ingest used to
    # mint fsq_id, so this re-joins without needing the id round-trip.
    by_key = {}
    for name, lat, lng, webs, phones in rows:
        k = (name, round(float(lat), 6), round(float(lng), 6))
        by_key[k] = (webs[0] if webs else None, phones[0] if phones else None)

    with search.cursor() as cur:
        cur.execute("SELECT fsq_id, name, lat, lng FROM venues WHERE source='overture'")
        venues = cur.fetchall()

    updates = []
    for fsq_id, name, lat, lng in venues:
        hit = by_key.get((name, round(float(lat), 6), round(float(lng), 6)))
        if hit and (hit[0] or hit[1]):
            updates.append((fsq_id, hit[0], hit[1]))
    log.info("matched contact data for %d/%d overture venues", len(updates), len(venues))

    if dry or not updates:
        return
    with search.cursor() as cur:
        for i in range(0, len(updates), CHUNK):
            psycopg2.extras.execute_values(
                cur,
                "UPDATE venues v SET website=d.w, tel=d.t"
                "  FROM (VALUES %s) AS d(id,w,t) WHERE v.fsq_id=d.id",
                updates[i:i + CHUNK], page_size=1000)
            search.commit()
    log.info("contacts written")


def rejoin_buildings(search, dry: bool) -> None:
    with search.cursor() as cur:
        cur.execute("SELECT fsq_id, lat, lng FROM venues "
                    " WHERE source='overture' AND bin IS NULL AND lat IS NOT NULL")
        todo = cur.fetchall()
    log.info("%d overture venues still have no building", len(todo))
    if not todo:
        return

    fp = psycopg2.connect(os.environ["FOOTPRINTS_DB_URL"])
    found: list[tuple] = []
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i + CHUNK]
        vals = ",".join(cur_.mogrify("(%s,%s::float8,%s::float8)", r).decode()
                        for cur_, r in ((fp.cursor(), r) for r in batch))
        with fp.cursor() as c:
            # Containment first: a POI point inside the footprint IS that
            # building, regardless of how far the centroid is.
            c.execute(f"""
                WITH v(id, lat, lng) AS (VALUES {vals})
                SELECT v.id, bf.bin, bf.bbl, bf.construction_year
                  FROM v
                  JOIN LATERAL (
                       SELECT bin, bbl, construction_year
                         FROM building_footprints
                        WHERE ST_Contains(footprint,
                                ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326))
                        LIMIT 1
                  ) bf ON true
            """)
            hit = c.fetchall()
            got = {r[0] for r in hit}
            found += hit
            # Fallback: coordinates that land in the street.
            rest = [r for r in batch if r[0] not in got]
            if rest:
                vals2 = ",".join(c.mogrify("(%s,%s::float8,%s::float8)", r).decode()
                                 for r in rest)
                c.execute(f"""
                    WITH v(id, lat, lng) AS (VALUES {vals2})
                    SELECT v.id, bf.bin, bf.bbl, bf.construction_year
                      FROM v
                      CROSS JOIN LATERAL (
                           SELECT bin, bbl, construction_year, footprint
                             FROM building_footprints
                            ORDER BY footprint <-> ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326)
                            LIMIT 1
                      ) bf
                     WHERE ST_DWithin(bf.footprint::geography,
                             ST_SetSRID(ST_MakePoint(v.lng, v.lat), 4326)::geography, {FALLBACK_M})
                """)
                found += c.fetchall()
        log.info("  joined %d/%d", min(i + CHUNK, len(todo)), len(todo))
    fp.close()

    log.info("recovered a building for %d of %d (%.0f%%)",
             len(found), len(todo), 100 * len(found) / max(1, len(todo)))
    if dry or not found:
        return
    with search.cursor() as cur:
        for i in range(0, len(found), CHUNK):
            psycopg2.extras.execute_values(
                cur,
                "UPDATE venues v SET bin=d.bin, bbl=d.bbl,"
                "  building_year=coalesce(v.building_year, d.yr::integer)"
                "  FROM (VALUES %s) AS d(id,bin,bbl,yr) WHERE v.fsq_id=d.id",
                [(a, b, c2, y) for a, b, c2, y in found], page_size=1000)
            search.commit()
    log.info("buildings written")


def purge_outside_nyc(search, dry: bool) -> None:
    """Delete Overture rows that are not in New York City.

    The ingest bbox (-74.30..-73.68, 40.48..40.93) was drawn as "NYC plus a
    margin" and the margin turned out to be New Jersey. 15,772 of the rows
    with no building sit west of -74.03 (Englewood Cliffs, Hoboken, Jersey
    City) and 3,988 north of 40.88 (Westchester) -- 15,332 of the 20,921
    unjoined rows are simply outside the city.

    That, not the join, is why Overture's BIN rate looked bad at 67.5%
    against Foursquare's 84.9%: a NYC footprints table cannot match a New
    Jersey coffee shop. Removing them puts Overture at 43,360/48,949 = 88.6%,
    ahead of FSQ.

    A BOUNDING BOX CANNOT DO THIS. New York City and New Jersey interleave in
    longitude -- Staten Island sits at -74.2, west of Jersey City at -74.04 --
    so the footprint envelope (-74.255..-73.700) contains most of NJ too. A
    first pass using that box removed only 4,323 rows and left 15,037 New
    Jersey venues in place, which is what dragged the map out to Hoboken.

    Point-in-polygon against the NTA 2020 neighborhood boundaries is the
    definitive test, and those polygons are already on disk from
    backfill_index_neighborhood.py.
    """
    from shapely.geometry import Point, shape
    from shapely.strtree import STRtree
    import json as _json

    cache = os.path.join(os.path.dirname(__file__), ".nta_2020.geojson")
    if not os.path.exists(cache):
        log.warning("NTA polygons missing (%s); skipping purge", cache)
        return
    with open(cache) as fh:
        gj = _json.load(fh)
    polys = [shape(f["geometry"]) for f in gj["features"] if f.get("geometry")]
    tree = STRtree(polys)
    log.info("%d NYC boundary polygons", len(polys))

    with search.cursor() as cur:
        cur.execute("SELECT fsq_id, lat, lng FROM venues "
                    " WHERE source='overture' AND lat IS NOT NULL")
        rows = cur.fetchall()

    outside = []
    for fsq_id, lat, lng in rows:
        pt = Point(float(lng), float(lat))
        if not any(polys[i].contains(pt) for i in tree.query(pt)):
            outside.append(fsq_id)

    log.info("%d of %d overture rows are outside NYC", len(outside), len(rows))
    if dry or not outside:
        return
    with search.cursor() as cur:
        for i in range(0, len(outside), CHUNK):
            cur.execute("DELETE FROM venues WHERE fsq_id = ANY(%s)",
                        (outside[i:i + CHUNK],))
            search.commit()
    log.info("removed %d out-of-city rows", len(outside))


def dedupe_residual(search, dry: bool) -> None:
    """Remove Overture rows that duplicate an FSQ row 50-130m away.

    The ingest deduped on name within 50m, matching the 40m rule the iOS
    client uses for Apple POI. That is right for dense blocks but too tight
    for cross-source coordinate disagreement: the two sources put Nelson A.
    Rockefeller Park 53m apart, Winter Garden Atrium 54m, Milk & Pull 52m --
    one venue each, two opinions about where it is.

    Widening the radius alone would merge real neighbours, so the rule is
    CHAIN-AWARE and the chain test is measured, not listed: a normalized name
    appearing 3+ times across the corpus is a chain (Pret A Manger, Popeyes),
    and two of its stores 119m apart are two stores. A name that occurs once
    or twice is a single venue, and a second copy within 130m is a duplicate.
    """
    with search.cursor() as cur:
        cur.execute("""
            WITH norm AS (
                SELECT fsq_id, source, lat, lng,
                       lower(regexp_replace(name, '[^a-zA-Z0-9]', '', 'g')) AS n
                  FROM venues WHERE lat IS NOT NULL AND lng IS NOT NULL
            ),
            chains AS (
                SELECT n FROM norm WHERE n <> '' GROUP BY n HAVING count(*) >= 3
            )
            SELECT o.fsq_id
              FROM norm o
             WHERE o.source = 'overture'
               AND o.n <> ''
               AND o.n NOT IN (SELECT n FROM chains)
               AND EXISTS (
                   SELECT 1 FROM norm f
                    WHERE f.source = 'fsq' AND f.n = o.n
                      AND abs(f.lat - o.lat) < 0.0012
                      AND abs(f.lng - o.lng) < 0.0016
               )
        """)
        ids = [r[0] for r in cur.fetchall()]
    log.info("%d overture rows duplicate an FSQ row (non-chain, <=130m)", len(ids))
    if dry or not ids:
        return
    with search.cursor() as cur:
        for i in range(0, len(ids), CHUNK):
            cur.execute("DELETE FROM venues WHERE fsq_id = ANY(%s)", (ids[i:i + CHUNK],))
            search.commit()
    log.info("removed %d duplicates", len(ids))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--contacts-only", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    search = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    backfill_contacts(search, args.dry_run)
    if not args.contacts_only:
        purge_outside_nyc(search, args.dry_run)
        rejoin_buildings(search, args.dry_run)
        dedupe_residual(search, args.dry_run)
    with search.cursor() as cur:
        cur.execute("""SELECT source, count(*),
                              count(*) FILTER (WHERE bin IS NOT NULL),
                              count(*) FILTER (WHERE website IS NOT NULL),
                              count(*) FILTER (WHERE tel IS NOT NULL)
                         FROM venues GROUP BY 1 ORDER BY 2 DESC""")
        for src, n, b, w, t in cur.fetchall():
            log.info("  %-9s rows=%-7d bin=%-7d web=%-7d tel=%d", src, n, b, w, t)
    search.close()
    log.info("done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

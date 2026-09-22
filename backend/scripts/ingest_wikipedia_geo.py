"""
Index NYC's geotagged Wikipedia articles into layer_search_index (layer='wiki').

The app's Wikipedia map layer is fetched live, per viewport, through the
Cloudflare Worker, so search never saw a single article: "where did famous
writers live" or "demolished theaters" could only reach the 1,922 hand-built
lore rows, while Wikipedia holds thousands of NYC articles about exactly
these things.

Pipeline:
  1. Tile the city (only grid points near an NTA polygon) and page through
     list=geosearch at each point. A tile that returns the API's 500-result
     cap is split into four smaller ones, so dense Midtown is not truncated.
  2. Fetch intro extracts, 20 titles per call. Disambiguation pages and
     stubs with no extract are skipped.
  3. Keep articles within ~800m of NYC (river and harbor articles count,
     Hoboken does not; same rule as scripts/flag_layers_in_nyc.py).
  4. Embed "title. extract" with the search model and upsert, keyed
     'wiki:{pageid}'. Re-runs update in place; only changed text re-embeds.

Run: python -m scripts.ingest_wikipedia_geo [--dry-run] [--max-tiles N]
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time

import httpx
import numpy as np
import psycopg2
import psycopg2.extras
import shapely
from shapely.strtree import STRtree

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.enrich_venues import load_ntas  # noqa: E402
from services.text_embeddings import MODEL_NAME  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("wiki-geo")

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "JinkSearchIndexer/1.0 (https://github.com/montlkn/NYC_Scanning)"}
STEP_DEG = 0.012          # ~1.3km tiles
RADIUS_M = 1000           # covers a tile's corners with overlap
NEAR_NYC_DEG = 0.009      # ~800m
API_CAP = 500
MIN_EXTRACT = 60


def _get(client: httpx.Client, params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": 2}
    for attempt in range(5):
        try:
            r = client.get(API, params=params)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            time.sleep(1 + attempt)
    return {}


def geosearch(client, lat, lng, radius) -> list:
    d = _get(client, {"action": "query", "list": "geosearch", "gscoord": f"{lat}|{lng}",
                      "gsradius": radius, "gslimit": API_CAP, "gsnamespace": 0})
    return d.get("query", {}).get("geosearch", [])


def harvest(client, geoms, max_tiles: int | None) -> dict:
    tree = STRtree(geoms)
    minx, miny, maxx, maxy = shapely.total_bounds(np.array(geoms))
    pts = [(la, lo) for la in np.arange(miny, maxy + STEP_DEG, STEP_DEG)
                     for lo in np.arange(minx, maxx + STEP_DEG, STEP_DEG)]
    near = set(tree.query(shapely.points([p[1] for p in pts], [p[0] for p in pts]),
                          predicate="dwithin", distance=NEAR_NYC_DEG)[0].tolist())
    queue = [(pts[i][0], pts[i][1], RADIUS_M, STEP_DEG) for i in sorted(near)]
    if max_tiles:
        queue = queue[:max_tiles]
    log.info("%d tiles", len(queue))
    pages: dict = {}
    done = 0
    while queue:
        lat, lng, radius, step = queue.pop()
        rows = geosearch(client, lat, lng, radius)
        for r in rows:
            pages[r["pageid"]] = (r["title"], r["lat"], r["lon"])
        if len(rows) >= API_CAP and radius > 150:
            h = step / 4
            for dla in (-h, h):
                for dlo in (-h, h):
                    queue.append((lat + dla, lng + dlo, radius // 2, step / 2))
        done += 1
        if done % 100 == 0:
            log.info("  %d tiles, %d articles, %d queued", done, len(pages), len(queue))
    return pages


def extracts(client, titles: list) -> dict:
    out = {}
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        d = _get(client, {"action": "query", "prop": "extracts|pageprops", "exintro": 1,
                          "explaintext": 1, "exlimit": 20, "titles": "|".join(chunk),
                          "redirects": 1})
        for p in d.get("query", {}).get("pages", []):
            if "disambiguation" in (p.get("pageprops") or {}):
                continue
            ext = (p.get("extract") or "").strip()
            if len(ext) >= MIN_EXTRACT:
                out[p["title"]] = ext
        if (i // 20) % 50 == 0:
            log.info("  extracts %d/%d", i + len(chunk), len(titles))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-tiles", type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()
    geoms, _ = load_ntas()
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        pages = harvest(client, geoms, args.max_tiles)
        log.info("%d geotagged articles (%.0fs)", len(pages), time.time() - t0)

        tree = STRtree(geoms)
        ids = list(pages)
        pts = shapely.points([pages[i][2] for i in ids], [pages[i][1] for i in ids])
        near = set(tree.query(pts, predicate="dwithin", distance=NEAR_NYC_DEG)[0].tolist())
        ids = [ids[i] for i in sorted(near)]
        log.info("%d within NYC", len(ids))

        ext = extracts(client, [pages[i][0] for i in ids])
    # Strictly inside (with a small shoreline snap) vs merely near: "near"
    # keeps bridge and harbor articles whose coordinates sit mid-river, but
    # across the narrow Arthur Kill it also admits Perth Amboy. An article
    # that is only NEAR and whose opening says New Jersey is New Jersey's.
    strict = set(tree.query(shapely.points([pages[i][2] for i in ids], [pages[i][1] for i in ids]),
                            predicate="dwithin", distance=0.0015)[0].tolist())
    rows = []
    for n, pid in enumerate(ids):
        title, lat, lng = pages[pid]
        e = ext.get(title)
        if not e:
            continue
        if n not in strict and "new jersey" in e[:250].lower():
            continue
        body = e[:1500]
        rows.append((f"wiki:{pid}", title, e[:160], f"{title}. {body}", lat, lng))
    log.info("%d articles with an extract (%.0fs)", len(rows), time.time() - t0)
    if args.dry_run:
        for r in rows[:: max(1, len(rows) // 10)][:10]:
            log.info("  %s | %s", r[1], r[2][:100])
        return 0

    conn = psycopg2.connect(os.environ["SEARCH_DB_URL"])
    with conn.cursor() as cur:
        cur.execute("SELECT id, text FROM layer_search_index WHERE layer = 'wiki'")
        have = {i: hashlib.md5(t.encode()).hexdigest() for i, t in cur.fetchall()}
    todo = [r for r in rows if have.get(r[0]) != hashlib.md5(r[3].encode()).hexdigest()]
    log.info("%d new or changed", len(todo))
    if todo:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=MODEL_NAME, threads=os.cpu_count() or 4)
        with conn.cursor() as cur:
            for i in range(0, len(todo), 1000):
                chunk = todo[i:i + 1000]
                vecs = [v.tolist() for v in model.embed([c[3] for c in chunk], batch_size=64)]
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO layer_search_index (id, layer, title, snippet, text, embedding, lat, lng,"
                    " category, in_nyc, updated_at) VALUES %s"
                    " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, snippet = EXCLUDED.snippet,"
                    " text = EXCLUDED.text, embedding = EXCLUDED.embedding, lat = EXCLUDED.lat,"
                    " lng = EXCLUDED.lng, in_nyc = true, updated_at = now()",
                    [(c[0], "wiki", c[1], c[2], c[3], str(v), c[4], c[5], "wikipedia", True)
                     for c, v in zip(chunk, vecs)],
                    template="(%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s, now())",
                    page_size=500,
                )
                conn.commit()
                log.info("  upserted %d/%d", i + len(chunk), len(todo))
    conn.close()
    log.info("done in %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Score /api/search/unified against tests/search_eval_cases.json.

Every case is a query that was once wrong and the property that makes it
right. Run before and after any ranking change; a change that fixes one query
and breaks two shows up here instead of in a screenshot.

    python -m scripts.search_eval                       # production
    python -m scripts.search_eval --base http://localhost:8011
    python -m scripts.search_eval --only "tin"          # cases whose query contains "tin"

Exit code is the number of failures, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx

DEFAULT_BASE = "https://nycscanning-production.up.railway.app"
CASES = os.path.join(os.path.dirname(__file__), "..", "tests", "search_eval_cases.json")


def _names(hits):
    return [h.get("name") or "" for h in hits]


def check(case: dict, hits: list) -> list:
    """Return a list of failure messages (empty = pass)."""
    fails = []
    k = case.get("topk", 5)
    top = hits[:k]
    names = _names(top)
    if "top1_name" in case:
        got = hits[0]["name"] if hits else None
        if got != case["top1_name"]:
            fails.append(f"top1 {got!r} != {case['top1_name']!r}")
    if "top1_contains" in case:
        got = hits[0]["name"] if hits else ""
        if case["top1_contains"].lower() not in got.lower():
            fails.append(f"top1 {got!r} lacks {case['top1_contains']!r}")
    for n in case.get("all_of", []):
        if not any(n.lower() in x.lower() for x in names):
            fails.append(f"missing {n!r} in top{k}")
    if case.get("any_of") and not any(a.lower() in x.lower() for a in case["any_of"] for x in names):
        fails.append(f"none of {case['any_of']} in top{k}")
    for n in case.get("none_of", []):
        if any(n.lower() == x.lower() for x in names):
            fails.append(f"unwanted {n!r} in top{k}")
    for n in case.get("none_of_top3", []):
        if any(n.lower() == x.lower() for x in _names(hits[:3])):
            fails.append(f"unwanted {n!r} in top3")
    if "category_matches" in case:
        rx = re.compile(case["category_matches"], re.I)
        bad = [h["name"] for h in top if not rx.search((h.get("category") or "") + " " + (h.get("name") or ""))]
        if bad:
            fails.append(f"category not /{case['category_matches']}/: {bad}")
    if "types" in case:
        bad = [h["name"] for h in top if h.get("type") not in case["types"]]
        if bad:
            fails.append(f"type not in {case['types']}: {bad}")
    if case.get("nearest_first"):
        d = [h.get("dist_m") for h in top if h.get("dist_m") is not None]
        if d != sorted(d):
            fails.append(f"not nearest-first: {[round(x) for x in d]}")
    if "first_within_m" in case:
        d = hits[0].get("dist_m") if hits else None
        if d is None or d > case["first_within_m"]:
            fails.append(f"first hit {d}m > {case['first_within_m']}m")
    if "all_within_m" in case:
        far = [(h["name"], round(h["dist_m"] or 0)) for h in top
               if h.get("dist_m") is None or h["dist_m"] > case["all_within_m"]]
        if far:
            fails.append(f"outside area: {far[:3]}")
    if "min_in_years" in case:
        n, lo, hi = case["min_in_years"]
        ok = sum(1 for h in top if isinstance(h.get("year"), int) and lo <= h["year"] <= hi)
        if ok < n:
            fails.append(f"only {ok}/{n} in {lo}-{hi}: {[h.get('year') for h in top]}")
    if "min_name_contains" in case:
        n, word = case["min_name_contains"]
        ok = sum(1 for x in names if word.lower() in x.lower())
        if ok < n:
            fails.append(f"only {ok}/{n} names contain {word!r}: {names}")
    if case.get("no_new_jersey"):
        # West of the Hudson's Manhattan shore, north of Bayonne: Jersey City,
        # Hoboken, Fort Lee. Coarse on purpose; a hit here is a real leak.
        nj = [h["name"] for h in hits if h.get("lng") is not None and h.get("lat") is not None
              and h["lng"] < -74.02 and h["lat"] > 40.70]
        if nj:
            fails.append(f"New Jersey results: {nj[:3]}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SEARCH_EVAL_BASE", DEFAULT_BASE))
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    spec = json.load(open(CASES))
    locs = spec["locations"]
    failures = 0
    t_all = []
    with httpx.Client(timeout=30.0) as client:
        for case in spec["cases"]:
            if args.only and args.only.lower() not in case["q"].lower():
                continue
            lat, lng = locs[case["at"]]
            params = {"q": case["q"], "lat": lat, "lng": lng, "limit": 18,
                      "radius_m": case.get("area_radius_m", 4000), "debug": "true"}
            if "area_radius_m" in case:
                params["area"] = "true"
            t = time.time()
            try:
                d = client.get(f"{args.base}/api/search/unified", params=params).json()
            except Exception as e:
                print(f"ERROR {case['q']!r}: {e}")
                failures += 1
                continue
            el = time.time() - t
            t_all.append(el)
            fails = check(case, d.get("hits", []))
            mark = "ok  " if not fails else "FAIL"
            print(f"{mark} {el:4.1f}s  {case['q']!r}")
            for f in fails:
                print(f"         {f}")
            failures += bool(fails)
    n = len(t_all)
    if n:
        t_all.sort()
        print(f"\n{n - failures}/{n} passed · p50 {t_all[n // 2]:.1f}s · max {t_all[-1]:.1f}s")
    return failures


if __name__ == "__main__":
    sys.exit(main())

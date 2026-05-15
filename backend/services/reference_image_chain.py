"""
Reference image fallback chain — F1b + free F2.

Free-first chain for building reference imagery:

    Mapillary (free, pano-aware selection)  →  Google Street View ($0.007)

NYC tax photos were considered but excluded: the archival imagery is from the
1940s and 1980s, which would teach CLIP a building's *historical* facade —
actively misleading for matching a phone snap of the current building.

Mapillary coverage in NYC is good on major streets, patchier on side streets.
Google is the always-works backstop.

F2 (free version): instead of picking the first pano in a bbox, we score panos
by whether their camera was *actually pointed at the target building* at capture
time. This catches the "Street View of the wrong building" problem at zero cost
since we're already calling Mapillary.

Performance: tight timeouts. The chain bails to the next source on
timeout/error/empty-result rather than blocking the request.
"""

import math
import os
import logging
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


MAPILLARY_TOKEN = os.environ.get("MAPILLARY_ACCESS_TOKEN") or ""


async def fetch_reference_image(
    *,
    lat: float,
    lng: float,
    bbl: Optional[str],  # accepted but currently unused; kept for forward compat
    google_fallback,     # callable: (lat, lng) -> Optional[bytes]
) -> Tuple[Optional[bytes], str]:
    """
    Try free sources first, fall through to the paid Google fetcher.

    Returns (image_bytes, source_label). `source_label` is one of:
      'mapillary', 'google_streetview', 'none'.
    """
    # 1. Mapillary — pano-aware: prefer images whose camera was aimed at this building
    if MAPILLARY_TOKEN:
        img = await _fetch_mapillary(lat, lng)
        if img:
            return img, "mapillary"
    else:
        logger.warning("MAPILLARY_ACCESS_TOKEN not set; skipping Mapillary step")

    # 2. Google Street View (paid backstop)
    img = await google_fallback(lat, lng)
    if img:
        return img, "google_streetview"

    return None, "none"


# ─── Mapillary ────────────────────────────────────────────────────────────────

# Search a ~80m bbox around the building centroid. Mapillary panos are placed
# every 5-15m on streets, so this typically yields several candidates per query.
_MAPILLARY_BBOX_DEG = 0.0007  # ~80m at NYC latitudes
# A pano scores well when its compass_angle (the direction the camera was facing
# when the photo was taken) is within this many degrees of the bearing toward
# the target building. 45° is generous — it catches panos that walked past the
# building at an angle, not just dead-on shots.
_MAPILLARY_ALIGNMENT_TOL_DEG = 45.0


async def _fetch_mapillary(lat: float, lng: float) -> Optional[bytes]:
    """
    Find the Mapillary pano whose camera was actually pointed at the target
    building, not just "any pano in the area."

    Scoring: a pano is good when (a) its physical location is close to the
    building AND (b) its compass_angle aims at the building. We minimise the
    sum `angular_diff + 0.5 × distance_normalised`. Closer + better-aimed wins.
    """
    if not MAPILLARY_TOKEN:
        return None

    bbox = (
        f"{lng - _MAPILLARY_BBOX_DEG},{lat - _MAPILLARY_BBOX_DEG},"
        f"{lng + _MAPILLARY_BBOX_DEG},{lat + _MAPILLARY_BBOX_DEG}"
    )
    # Request geometry so we know where each pano was taken from.
    list_url = (
        "https://graph.mapillary.com/images"
        "?fields=id,thumb_2048_url,compass_angle,geometry"
        f"&bbox={bbox}"
        "&limit=25"
    )
    headers = {"Authorization": f"OAuth {MAPILLARY_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(list_url, headers=headers)
            if resp.status_code != 200:
                logger.info(f"Mapillary list returned {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            images = data.get("data") or []
            if not images:
                logger.info(f"Mapillary returned no images near ({lat}, {lng})")
                return None

            best = _pick_best_pano(images, building_lat=lat, building_lng=lng)
            if not best:
                return None

            thumb_url = best.get("thumb_2048_url")
            if not thumb_url:
                return None

            img_resp = await client.get(thumb_url)
            if img_resp.status_code == 200 and len(img_resp.content) > 5000:
                return img_resp.content
    except Exception as e:
        logger.info(f"Mapillary fetch failed: {e}")
    return None


def _pick_best_pano(
    images: list,
    *,
    building_lat: float,
    building_lng: float,
) -> Optional[dict]:
    """
    Among Mapillary candidates, pick the pano whose camera was aimed at the
    target building. Falls back to the first thumb if no pano metadata is
    usable (so we degrade gracefully to the old behaviour).
    """
    scored = []
    for im in images:
        if not im.get("thumb_2048_url"):
            continue
        geom = im.get("geometry") or {}
        coords = geom.get("coordinates") or []
        compass = im.get("compass_angle")
        if not coords or compass is None:
            # Without pose info we can't score it; keep as last-resort.
            scored.append((float("inf"), im))
            continue
        pano_lng, pano_lat = float(coords[0]), float(coords[1])
        bearing = _bearing_deg(pano_lat, pano_lng, building_lat, building_lng)
        angular_diff = _angular_diff_deg(compass, bearing)
        if angular_diff > _MAPILLARY_ALIGNMENT_TOL_DEG:
            # Camera was pointing the wrong way — likely captured the back of
            # the building or a different building entirely.
            scored.append((angular_diff * 2, im))
            continue
        dist_m = _haversine_m(pano_lat, pano_lng, building_lat, building_lng)
        # Distance normalised so 30m ≈ 30° equivalence — both penalised equally.
        score = angular_diff + 0.5 * dist_m
        scored.append((score, im))

    if not scored:
        return None
    scored.sort(key=lambda kv: kv[0])
    return scored[0][1]


# ─── Geo helpers (local to this module — small + standalone) ─────────────────

def _bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angular_diff_deg(a: float, b: float) -> float:
    d = abs((a - b + 540) % 360 - 180)
    return d


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

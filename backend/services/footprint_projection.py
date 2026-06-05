"""
Footprint projection + IoU scoring for tap-to-pick (Phase 13).

Given the camera pose (lat/lng/bearing/pitch) and a candidate's PLUTO
footprint polygon, this module projects the footprint into the image plane
and computes an IoU-style overlap with the tap region (mask or bbox).

The key insight: the user's tap gives us an image-space silhouette of the
building they mean; the footprint back-projection gives us where each
candidate's plan-view polygon *would* appear in that same image. The
candidate whose projected footprint best overlaps the tapped region wins.

All geometry is done in a local ENU (East-North-Up) frame centred on the
camera, then perspective-projected onto a normalised image plane. No
external APIs, no ML — pure trigonometry.
"""

import json
import math
import base64
import logging
from typing import Optional
import numpy as np

logger = logging.getLogger(__name__)

# iPhone wide-angle lens: 35mm-equivalent ~26mm → HFOV ~73°.
# Ultrawide: 35mm-equiv ~13mm → HFOV ~120°. We receive lens_type and
# use these defaults unless intrinsics are provided.
_HFOV_DEG = {"standard": 73.0, "ultrawide": 120.0, "wide": 73.0}
_DEFAULT_HFOV = 73.0

# Pixel dimensions of the normalised image plane we project into (matches
# the 1024px JPEG the iOS client sends).
_IMG_W = 1024
_IMG_H = 768  # 4:3 aspect; ARKit captures at 1920×1440 → scaled to 1024×768


def _hfov_to_focal(hfov_deg: float, img_w: int = _IMG_W) -> float:
    """Focal length in pixels from horizontal field of view."""
    return img_w / (2.0 * math.tan(math.radians(hfov_deg / 2.0)))


def _latlng_to_enu(lat: float, lng: float, origin_lat: float, origin_lng: float) -> tuple[float, float, float]:
    """Convert lat/lng to ENU (metres) relative to origin."""
    R = 6371000.0
    dlat = math.radians(lat - origin_lat)
    dlng = math.radians(lng - origin_lng)
    lat_r = math.radians(origin_lat)
    north = R * dlat
    east = R * math.cos(lat_r) * dlng
    return east, north, 0.0  # (E, N, U)


def _enu_to_camera(
    east: float, north: float, up: float,
    yaw_deg: float, pitch_deg: float,
) -> tuple[float, float, float]:
    """
    Rotate ENU vector into camera frame.
    Camera convention: +X right, +Y down, +Z forward (into scene).
    Yaw = compass bearing (0=North, 90=East).
    Pitch = phone pitch (-90=pointing up, 0=horizontal, 90=pointing down).
    """
    # Compass bearing → rotation around vertical axis.
    # In ENU: forward (bearing=0) = North = (0, 1, 0).
    yaw_r = math.radians(yaw_deg)
    # World forward direction in ENU for this bearing.
    fwd_e = math.sin(yaw_r)
    fwd_n = math.cos(yaw_r)
    # World right direction (perpendicular, clockwise 90°).
    right_e = math.cos(yaw_r)
    right_n = -math.sin(yaw_r)

    # Project ENU vector onto camera axes before pitch.
    cam_x = east * right_e + north * right_n   # right axis
    cam_z = east * fwd_e + north * fwd_n        # forward axis
    cam_y_enu = up                               # up in ENU

    # Apply pitch: rotate around X (right) axis.
    # Positive pitch = phone tilted up (sky) → forward tips upward.
    pitch_r = math.radians(pitch_deg)
    cp, sp = math.cos(pitch_r), math.sin(pitch_r)
    new_y = -cam_y_enu * cp + cam_z * sp   # camera Y = down
    new_z = cam_y_enu * sp + cam_z * cp

    return cam_x, new_y, new_z


def _project_to_image(
    cam_x: float, cam_y: float, cam_z: float,
    focal: float, img_w: int = _IMG_W, img_h: int = _IMG_H,
) -> Optional[tuple[float, float]]:
    """
    Perspective-project a camera-space point to pixel coords.
    Returns None if the point is behind the camera (z <= 0).
    """
    if cam_z <= 0.1:
        return None
    px = focal * cam_x / cam_z + img_w / 2.0
    py = focal * cam_y / cam_z + img_h / 2.0
    return px / img_w, py / img_h  # normalised 0..1


def project_footprint(
    footprint_geojson: str,
    cam_lat: float, cam_lng: float,
    bearing_deg: float, pitch_deg: float,
    lens_type: str = "standard",
    building_height_m: float = 15.0,
) -> Optional[list[tuple[float, float]]]:
    """
    Project a building's PLUTO footprint polygon into the camera image plane.

    Returns a list of normalised (x, y) points (0..1) representing the
    projected footprint outline, or None if the footprint is entirely behind
    the camera or outside the image frame.

    We project both the ground-level ring AND a raised ring at
    `building_height_m` to get an approximation of the visible facade quad.
    The convex hull of all projected points forms the facade silhouette.
    """
    try:
        geom = json.loads(footprint_geojson)
    except (json.JSONDecodeError, TypeError):
        return None

    # Extract coordinate rings. Support Polygon and MultiPolygon.
    rings: list[list[tuple[float, float]]] = []
    gtype = geom.get("type", "")
    if gtype == "Polygon":
        rings = geom.get("coordinates", [])
    elif gtype == "MultiPolygon":
        for poly in geom.get("coordinates", []):
            rings.extend(poly)
    else:
        return None

    if not rings:
        return None

    focal = _hfov_to_focal(_HFOV_DEG.get(lens_type, _DEFAULT_HFOV))
    projected: list[tuple[float, float]] = []

    for ring in rings:
        for coord in ring:
            lng, lat = coord[0], coord[1]
            for height in (0.0, building_height_m):
                e, n, _ = _latlng_to_enu(lat, lng, cam_lat, cam_lng)
                cx, cy, cz = _enu_to_camera(e, n, height, bearing_deg, pitch_deg)
                pt = _project_to_image(cx, cy, cz, focal)
                if pt is not None:
                    projected.append(pt)

    if len(projected) < 3:
        return None

    # Return the convex hull for a clean silhouette.
    return _convex_hull(projected)


def _pixel_to_ground_latlng(
    px_norm: float, py_norm: float,
    cam_lat: float, cam_lng: float,
    bearing_deg: float, pitch_deg: float,
    focal: float,
    eye_height_m: float = 1.6,
    depth_m: Optional[float] = None,
) -> Optional[tuple[float, float]]:
    """
    Back-project a single normalised image pixel to a (lat, lng) point.

    When `depth_m` is provided (from ARKit `sceneDepth` or `raycast`), the
    ray extends to exactly that distance in 3D — no flat-ground assumption.
    This correctly places taps on buildings across the street, buildings
    above eye-level, etc. Returns None if `depth_m` is given but absurd
    (≤0 or >150m).

    Without `depth_m`, falls back to flat-ground intersection at
    `eye_height_m`. Returns None when the ray points at/above the horizon
    or further than 150m (cone-error territory).
    """
    if not (0.0 <= px_norm <= 1.0 and 0.0 <= py_norm <= 1.0):
        return None

    px = px_norm * _IMG_W
    py = py_norm * _IMG_H
    ray_cam = (px - _IMG_W / 2.0, py - _IMG_H / 2.0, focal)

    # Reverse pitch (camera frame → ENU-like, before yaw).
    pitch_r = math.radians(pitch_deg)
    cp, sp = math.cos(pitch_r), math.sin(pitch_r)
    ray_right = ray_cam[0]
    ray_y_enu = -ray_cam[1] * cp + ray_cam[2] * sp
    ray_fwd = ray_cam[1] * sp + ray_cam[2] * cp

    # Reverse yaw.
    yaw_r = math.radians(bearing_deg)
    fwd_e, fwd_n = math.sin(yaw_r), math.cos(yaw_r)
    right_e, right_n = math.cos(yaw_r), -math.sin(yaw_r)

    ray_e = ray_right * right_e + ray_fwd * fwd_e
    ray_n = ray_right * right_n + ray_fwd * fwd_n
    ray_u = ray_y_enu

    # Path A — ARKit depth is the gold standard.
    if depth_m is not None and depth_m > 0:
        if depth_m > 150:
            return None
        ray_len = math.sqrt(ray_e * ray_e + ray_n * ray_n + ray_u * ray_u)
        if ray_len < 1e-6:
            return None
        scale = depth_m / ray_len
        east = scale * ray_e
        north = scale * ray_n
    else:
        # Path B — flat-ground fallback (no depth available).
        if ray_u >= -1e-3:
            return None
        t = -eye_height_m / ray_u
        if t <= 0 or t > 150:
            return None
        east = t * ray_e
        north = t * ray_n

    R = 6371000.0
    lat_r = math.radians(cam_lat)
    return cam_lat + math.degrees(north / R), cam_lng + math.degrees(east / (R * math.cos(lat_r)))


def tap_facade_anchors(
    mask_bbox: tuple[float, float, float, float],
    cam_lat: float, cam_lng: float,
    bearing_deg: float, pitch_deg: float,
    lens_type: str = "standard",
    eye_height_m: float = 1.6,
    depth_m: Optional[float] = None,
) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    """
    Return the two (lat, lng) ground anchors at the bottom corners of the
    tap mask's silhouette. Together they define the facade *line segment*
    in plan view; we match against candidate footprint edges below.

    When `depth_m` is supplied (ARKit `sceneDepth` at the tap pixel), it's
    used as the true range — handles across-the-street buildings, raised
    facades, and arbitrary occlusion correctly. Without depth, we fall back
    to flat-ground intersection at eye height (current behavior).
    """
    x0, _y0, x1, y1 = mask_bbox
    focal = _hfov_to_focal(_HFOV_DEG.get(lens_type, _DEFAULT_HFOV))
    left = _pixel_to_ground_latlng(
        x0, y1, cam_lat, cam_lng, bearing_deg, pitch_deg, focal,
        eye_height_m, depth_m,
    )
    right = _pixel_to_ground_latlng(
        x1, y1, cam_lat, cam_lng, bearing_deg, pitch_deg, focal,
        eye_height_m, depth_m,
    )
    if left is None or right is None:
        return None
    return left, right


def _polygon_edges(ring: list) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Return list of (start, end) lng/lat pairs for each edge of a ring."""
    edges = []
    n = len(ring)
    if n < 2:
        return edges
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        edges.append(((a[0], a[1]), (b[0], b[1])))
    return edges


def _segment_midpoint_distance_m(
    seg: tuple[tuple[float, float], tuple[float, float]],
    cam_lat: float, cam_lng: float,
) -> float:
    """Distance in metres from camera to the midpoint of a (lng, lat) edge."""
    a, b = seg
    mid_lng, mid_lat = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
    R = 6371000.0
    dlat = math.radians(mid_lat - cam_lat)
    dlng = math.radians(mid_lng - cam_lng)
    lat_r = math.radians(cam_lat)
    return math.hypot(R * dlat, R * math.cos(lat_r) * dlng)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance between two (lat, lng) tuples."""
    R = 6371000.0
    lat1, lng1 = a
    lat2, lng2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _nearest_facade_edge(
    rings: list,
    cam_lat: float, cam_lng: float,
) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    """
    Pick the polygon edge most likely to be the building's facade from the
    user's vantage: the edge whose midpoint is closest to the camera. The
    edge is returned in (lat, lng) order — note PLUTO geojson is (lng, lat),
    we convert at the boundary.
    """
    best: Optional[tuple[float, tuple[tuple[float, float], tuple[float, float]]]] = None
    for ring in rings:
        for seg in _polygon_edges(ring):
            d = _segment_midpoint_distance_m(seg, cam_lat, cam_lng)
            if best is None or d < best[0]:
                # Convert to (lat, lng) for the caller.
                (alng, alat), (blng, blat) = seg
                best = (d, ((alat, alng), (blat, blng)))
    return best[1] if best is not None else None


def score_facade_match(
    candidates: list,
    tap_anchors: tuple[tuple[float, float], tuple[float, float]],
    cam_lat: float, cam_lng: float,
) -> list[dict]:
    """
    Score every candidate by how well its near-facade edge matches the two
    tap-derived ground anchors.

    Score = average of (distance from tap-left to candidate-facade-left,
                        distance from tap-right to candidate-facade-right).
    Lower is better. We also test the swapped pairing in case the polygon
    winds the other way, and keep whichever assignment is tighter. Returns
    candidates annotated with `tap_facade_score_m`. Candidates without a
    footprint get a +Infinity score so they sort last but aren't dropped.
    """
    tap_left, tap_right = tap_anchors
    annotated: list[dict] = []
    for c in candidates:
        gj = c.get("footprint_geojson")
        score = float("inf")
        if gj:
            try:
                geom = json.loads(gj) if isinstance(gj, str) else gj
            except (json.JSONDecodeError, TypeError):
                geom = None
            rings: list = []
            if isinstance(geom, dict):
                gtype = geom.get("type", "")
                if gtype == "Polygon":
                    rings = geom.get("coordinates", [])
                elif gtype == "MultiPolygon":
                    for poly in geom.get("coordinates", []):
                        rings.extend(poly)
            if rings:
                edge = _nearest_facade_edge(rings, cam_lat, cam_lng)
                if edge is not None:
                    e_left, e_right = edge
                    # Test both pairings (polygon winding can go either way).
                    same = (_haversine_m(tap_left, e_left) + _haversine_m(tap_right, e_right)) / 2.0
                    flip = (_haversine_m(tap_left, e_right) + _haversine_m(tap_right, e_left)) / 2.0
                    score = min(same, flip)
        c2 = dict(c)
        c2["tap_facade_score_m"] = score
        annotated.append(c2)
    return annotated


def _cross(o: tuple, a: tuple, b: tuple) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts
    lower: list = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _poly_bbox(poly: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_iou(
    ax0: float, ay0: float, ax1: float, ay1: float,
    bx0: float, by0: float, bx1: float, by1: float,
) -> float:
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a_area = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    b_area = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _mask_poly_iou(
    mask_bytes: bytes, mask_w: int, mask_h: int,
    poly: list[tuple[float, float]],
) -> float:
    """
    Compute IoU between a binary mask (flat bytes, 0 or 255, row-major) and
    a convex polygon (normalised coords). Uses a rasterised approximation:
    for each mask pixel, check if its normalised centre is inside the polygon.
    """
    if not mask_bytes or mask_w <= 0 or mask_h <= 0 or not poly:
        return 0.0

    # Rasterise polygon at mask resolution using ray-casting.
    mask_set = sum(1 for b in mask_bytes if b >= 128)
    if mask_set == 0:
        return 0.0

    hit = 0
    for py in range(mask_h):
        for px in range(mask_w):
            norm_x = (px + 0.5) / mask_w
            norm_y = (py + 0.5) / mask_h
            if _point_in_poly(norm_x, norm_y, poly):
                if mask_bytes[py * mask_w + px] >= 128:
                    hit += 1

    poly_area = _polygon_area(poly) * mask_w * mask_h  # approx pixels
    union = mask_set + max(0, poly_area - hit)
    return hit / union if union > 0 else 0.0


def _point_in_poly(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_area(poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    area = 0.0
    j = n - 1
    for i in range(n):
        area += (poly[j][0] + poly[i][0]) * (poly[j][1] - poly[i][1])
        j = i
    return abs(area) / 2.0


def compute_tap_overlap_score(
    candidate: dict,
    tap_x: float,
    tap_y: float,
    mask_b64: Optional[str],
    mask_w: int,
    mask_h: int,
    cam_lat: float,
    cam_lng: float,
    bearing_deg: float,
    pitch_deg: float,
    lens_type: str = "standard",
) -> float:
    """
    Compute a 0..1 overlap score between the tap region and this candidate's
    projected footprint. Returns 0.0 if the footprint can't be projected.

    Score is IoU when a mask is provided, or bbox-point containment (0 or 1)
    as a fallback when only the tap point is available.
    """
    footprint_geojson = candidate.get("footprint_geojson")
    if not footprint_geojson:
        return 0.0

    poly = project_footprint(
        footprint_geojson, cam_lat, cam_lng, bearing_deg, pitch_deg, lens_type
    )
    if not poly:
        return 0.0

    if mask_b64:
        try:
            mask_bytes = base64.b64decode(mask_b64)
            if mask_w > 0 and mask_h > 0 and len(mask_bytes) == mask_w * mask_h:
                return _mask_poly_iou(mask_bytes, mask_w, mask_h, poly)
        except Exception:
            pass

    # Empty-mask path: user tapped on background (no foreground instance
    # under the tap). Without segmentation we can't tell the user's intent
    # from pixels — we only know which projected footprints CONTAIN the
    # tap point. Multiple candidates can; previously we returned 1.0 for
    # all containers and lost to whatever came first in the cone-sorted
    # list (= the closest parcel, which is often a car-occupied tax lot
    # between user and the actual building they meant).
    #
    # Fix: weight by footprint area (buildings are >500 m², cars/awnings
    # are <50 m²) AND by distance (when ambiguous, prefer the FAR wall —
    # that's what the user can actually see and meant to tap). Both
    # signals point at "real building" vs "parked car parcel in front".
    if _point_in_poly(tap_x, tap_y, poly):
        area_m2 = candidate.get("shape_area") or 0
        try:
            area_m2 = float(area_m2)
        except (TypeError, ValueError):
            area_m2 = 0.0
        # Area component saturates at 500 m² — anything bigger gets the
        # full 1.0. Tiny things (cars ~10 m², awnings ~30 m²) get scored
        # proportionally low; a real building scoring ≥500 m² gets 1.0.
        area_factor = min(1.0, area_m2 / 500.0)

        # Distance component: prefer further. Within 0–150m cone (cone-cap
        # in routers/scan.py) we map distance to [0.5..1.0] so the far
        # wall along the ray beats the near one but doesn't drown out
        # area completely.
        dist_m = candidate.get("distance_meters") or 0
        try:
            dist_m = float(dist_m)
        except (TypeError, ValueError):
            dist_m = 0.0
        dist_factor = 0.5 + min(0.5, dist_m / 300.0)

        # Combine: average so both signals contribute, never zero unless
        # area_factor is zero (no shape_area on candidate — bail to 0.5).
        if area_factor <= 0:
            return dist_factor
        return (area_factor + dist_factor) / 2.0

    return 0.0


async def rank_by_tap_overlap(
    candidates: list[dict],
    tap_x: float,
    tap_y: float,
    mask_b64: Optional[str],
    mask_w: int,
    mask_h: int,
    cam_lat: float,
    cam_lng: float,
    bearing_deg: float,
    pitch_deg: float,
    lens_type: str = "standard",
) -> list[dict]:
    """
    Score each candidate by footprint×mask overlap and attach
    `tap_overlap_score`. Filter out zero-overlap candidates (hard pre-filter).
    Preserves original order among tied candidates.

    Returns the filtered+annotated list (may be empty if no candidate
    overlaps — in that case caller should skip the pre-filter).
    """
    scored = []
    for c in candidates:
        score = compute_tap_overlap_score(
            c, tap_x, tap_y, mask_b64, mask_w, mask_h,
            cam_lat, cam_lng, bearing_deg, pitch_deg, lens_type,
        )
        c = dict(c)
        c["tap_overlap_score"] = score
        scored.append(c)

    nonzero = [c for c in scored if c["tap_overlap_score"] > 0]
    if not nonzero:
        # No footprint projected into tap region — skip pre-filter, return
        # all with scores attached so telemetry can log the miss.
        logger.info(
            f"tap_overlap: no candidates overlapped tap ({tap_x:.3f},{tap_y:.3f}); "
            "skipping pre-filter"
        )
        return scored

    nonzero.sort(key=lambda c: c["tap_overlap_score"], reverse=True)
    logger.info(
        f"tap_overlap: {len(nonzero)}/{len(candidates)} candidates overlap tap; "
        f"top score {nonzero[0]['tap_overlap_score']:.3f} (BIN {nonzero[0].get('bin')})"
    )
    return nonzero

"""
Pipeline orchestrator: image + GPS → ranked candidates → calibrated response.

Flow:
  1. retrieval.get_candidates    — adaptive cone (+ ring fallback only if sparse/low-CLIP)
  2. enrich_candidates            — metadata from buildings DB + PLUTO
  3. embedding lookup             — CLIP image-image similarity
  4. scoring.blend_scores         — two-signal weighted blend (footprint + clip_image)
  5. scoring.calibrate            — temperature softmax
  6. scoring.sort_and_decide      — sort + picker trigger
  7. thumbnails                   — resolve per-candidate thumbnail URL (R2-only, no live fetch)
  8. telemetry

Input/output are plain dicts — no FastAPI types leak in here.
The router in routers/scan_v2.py is the only caller.
"""

import asyncio
import logging
import re
import time
from typing import Optional, Tuple, List, Dict, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models.session import AsyncSessionLocal
from pipeline import retrieval, scoring, telemetry
from pipeline.config import get_pipeline_config
from services.geospatial import enrich_candidates_with_metadata

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()


_GROK_TEXT_EVIDENCE_KEYWORDS = (
    "number", "address", "flag", "sign", "signage", "plaque",
    "awning", "lettering", "letters", "inscribed", "written",
    "consulate", "logo", "banner", "stencil", "name plate",
    "nameplate", "engraved", "house number", "street number",
)
_GROK_DIGIT_RE = re.compile(r"\b\d{2,5}\b")

# Rowhouse-cluster threshold: when 2+ candidates fall within this radius of
# each other (centroid-to-centroid), geometry alone cannot tell them apart and
# we should fire the VLM even if bail/picker hasn't tripped. Tuned to the NYC
# rowhouse footprint (~6m wide x 18m deep → adjacent centroids ≈ 8-12m apart).
_GROK_CLUSTER_RADIUS_M = 15.0


def _candidates_clustered_within(
    candidates: List[Dict[str, Any]],
    radius_m: float,
) -> bool:
    """True if any two candidates' centroids are within `radius_m` of each other."""
    import math

    def latlng(c: Dict[str, Any]) -> Optional[tuple[float, float]]:
        la = c.get("geocoded_lat") or c.get("latitude")
        ln = c.get("geocoded_lng") or c.get("longitude")
        if la is None or ln is None:
            return None
        try:
            return float(la), float(ln)
        except (TypeError, ValueError):
            return None

    points = [p for p in (latlng(c) for c in candidates) if p is not None]
    if len(points) < 2:
        return False

    R = 6371000.0
    for i in range(len(points)):
        la1, ln1 = points[i]
        p1 = math.radians(la1)
        for j in range(i + 1, len(points)):
            la2, ln2 = points[j]
            p2 = math.radians(la2)
            dp = math.radians(la2 - la1)
            dl = math.radians(ln2 - ln1)
            a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
            d = 2 * R * math.asin(math.sqrt(a))
            if d <= radius_m:
                return True
    return False


def _has_textual_evidence(reason: Optional[str]) -> bool:
    """Grok's pick is trustworthy only when its reason cites readable marks
    (numbers, flags, signage, plaques). Pure facade-similarity reasons are
    the regression mode that loses 555 Park to a visually-similar neighbour."""
    if not reason:
        return False
    r = reason.lower()
    if any(k in r for k in _GROK_TEXT_EVIDENCE_KEYWORDS):
        return True
    # A standalone digit string in the reason ("121", "555") is strong evidence.
    return bool(_GROK_DIGIT_RE.search(r))


async def run(
    session: AsyncSession,
    photo_bytes: bytes,
    user_photo_url: str,
    lat: float,
    lng: float,
    bearing: float,
    pitch: float,
    gps_accuracy_m: Optional[float],
    heading_accuracy_deg: Optional[float],
    lens_type: str,
    scan_id: str,
    tap_x: Optional[float] = None,
    tap_y: Optional[float] = None,
    tap_mask_b64: Optional[str] = None,
    tap_mask_w: int = 0,
    tap_mask_h: int = 0,
    tap_depth_m: Optional[float] = None,
    nearest_poi: Optional[str] = None,
    gps_source: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute the full matching pipeline.

    Returns a dict with:
      matches, show_picker, verification_method,
      retrieval_meta, processing_time_ms, clip_cost_usd
    """
    t_start = time.time()

    # ── 1. Retrieval (cone, optional ring fallback) ─────────────────────────────
    # First pass without a clip score — ring fallback handled after CLIP below
    raw_candidates, retrieval_meta = await retrieval.get_candidates(
        session, lat, lng, bearing, pitch,
        gps_accuracy_m, heading_accuracy_deg, lens_type,
        top_clip_score=None,
    )

    if not raw_candidates:
        return _empty_response(retrieval_meta, t_start)

    # ── 2. Enrich metadata (CLIP fully removed) ────────────────────────────────
    enriched_cands = await enrich_candidates_with_metadata(session, raw_candidates)
    clip_cost = 0.0
    clip_method = "bypassed"

    # When tap is present, fetch footprints up front — both the IoU
    # pre-filter and the facade-edge matcher below need them, and the
    # original fetch at step 4 runs too late (after scoring) to feed them.
    if tap_x is not None and tap_y is not None:
        try:
            from services.geospatial import get_footprints_for_bins
            geom_by_bin = await get_footprints_for_bins(
                [str(c.get("bin") or "") for c in enriched_cands[:20]]
            )
            for c in enriched_cands[:20]:
                geo = geom_by_bin.get(str(c.get("bin") or ""))
                if geo:
                    c["footprint_geojson"] = geo
        except Exception as e:
            logger.warning(f"[{scan_id}] early footprint fetch failed (tap will degrade): {e}")

    # ── 2b. Tap-to-pick pre-filter (Phase 13) ────────────────────────────────
    # When the user tapped a building in the AR preview, project each
    # candidate's PLUTO footprint into the camera image plane and drop
    # candidates whose footprint has zero overlap with the tap region.
    # This happens BEFORE scoring so the signal is used as a hard gate, not
    # a soft weight. If no candidates survive (e.g. all footprints missing)
    # we fall through with the full set — never fail silently.
    if tap_x is not None and tap_y is not None:
        try:
            from services.footprint_projection import rank_by_tap_overlap
            filtered = await rank_by_tap_overlap(
                candidates=enriched_cands,
                tap_x=tap_x,
                tap_y=tap_y,
                mask_b64=tap_mask_b64,
                mask_w=tap_mask_w,
                mask_h=tap_mask_h,
                cam_lat=lat,
                cam_lng=lng,
                bearing_deg=bearing,
                pitch_deg=pitch,
                lens_type=lens_type,
            )
            # Only apply pre-filter if at least one candidate overlapped.
            nonzero = [c for c in filtered if c.get("tap_overlap_score", 0) > 0]
            if nonzero:
                enriched_cands = nonzero
                retrieval_meta["tap_prefilter"] = {
                    "kept": len(nonzero),
                    "dropped": len(filtered) - len(nonzero),
                    "top_score": nonzero[0].get("tap_overlap_score"),
                    "top_bin": nonzero[0].get("bin"),
                }
                logger.info(
                    f"[{scan_id}] tap pre-filter: kept {len(nonzero)}, "
                    f"top BIN {nonzero[0].get('bin')} score {nonzero[0].get('tap_overlap_score'):.3f}"
                )
            else:
                retrieval_meta["tap_prefilter"] = "no_overlap_fallthrough"
        except Exception as e:
            logger.warning(f"[{scan_id}] tap pre-filter failed (continuing): {e}")

        # ── 2c. Facade-edge match (the orthographic / plan-view step) ──────
        # The IoU pre-filter above asks "which projected footprint best fills
        # the tap silhouette?" — that's a visual-region test. The stronger
        # signal lives in plan view: take the tap mask's two ground vertices,
        # back-project them to (lat, lng) anchors on the sidewalk, and find
        # the candidate whose *near facade edge* in plan best matches those
        # two anchors. This survives oblique angles and partial occlusion
        # that confuse the IoU approach, and gives us a hard discriminator
        # for adjacent rowhouses (their facade edges are 6-8m apart in plan
        # even when they're visually identical in the camera frame).
        try:
            from services.footprint_projection import (
                tap_facade_anchors, score_facade_match,
            )
            # Compute the mask bbox in normalised image coords. Prefer the
            # client-provided mask geometry when present (down-sampled binary
            # mask), else fall back to a small square around the tap point.
            if tap_mask_b64 and tap_mask_w > 0 and tap_mask_h > 0:
                # The mask bbox is the tightest rect around lit pixels —
                # rank_by_tap_overlap already needs this; we reuse the same
                # decoder.
                import base64
                raw = base64.b64decode(tap_mask_b64)
                if len(raw) == tap_mask_w * tap_mask_h:
                    minx, miny, maxx, maxy = tap_mask_w, tap_mask_h, -1, -1
                    for yy in range(tap_mask_h):
                        row = raw[yy * tap_mask_w : (yy + 1) * tap_mask_w]
                        if any(b >= 128 for b in row):
                            for xx in range(tap_mask_w):
                                if row[xx] >= 128:
                                    if xx < minx: minx = xx
                                    if xx > maxx: maxx = xx
                                    if yy < miny: miny = yy
                                    if yy > maxy: maxy = yy
                    if maxx >= 0:
                        bbox = (
                            minx / tap_mask_w, miny / tap_mask_h,
                            (maxx + 1) / tap_mask_w, (maxy + 1) / tap_mask_h,
                        )
                    else:
                        bbox = (max(0.0, tap_x - 0.04), max(0.0, tap_y - 0.04),
                                min(1.0, tap_x + 0.04), min(1.0, tap_y + 0.04))
                else:
                    bbox = (max(0.0, tap_x - 0.04), max(0.0, tap_y - 0.04),
                            min(1.0, tap_x + 0.04), min(1.0, tap_y + 0.04))
            else:
                # No mask — back-project a small square around the tap point
                # as a degenerate "facade segment." Less accurate but still
                # tells us roughly where on the sidewalk the user pointed.
                bbox = (max(0.0, tap_x - 0.04), max(0.0, tap_y - 0.04),
                        min(1.0, tap_x + 0.04), min(1.0, tap_y + 0.04))

            anchors = tap_facade_anchors(
                mask_bbox=bbox,
                cam_lat=lat, cam_lng=lng,
                bearing_deg=bearing, pitch_deg=pitch,
                lens_type=lens_type,
                depth_m=tap_depth_m,
            )
            if anchors is not None:
                scored = score_facade_match(enriched_cands, anchors, lat, lng)
                scored.sort(key=lambda c: c.get("tap_facade_score_m", float("inf")))
                top_score = scored[0].get("tap_facade_score_m", float("inf"))
                second_score = scored[1].get("tap_facade_score_m", float("inf")) if len(scored) > 1 else float("inf")
                # Tight match + clear gap → trust the tap. 6m threshold
                # accommodates ARKit pose error + GPS error + facade edge
                # measurement error. 2x ratio rejects ties where adjacent
                # rowhouses are all roughly equidistant.
                if top_score < 6.0 and (second_score == float("inf") or second_score > top_score * 1.5):
                    enriched_cands = scored
                    retrieval_meta["tap_facade_match"] = {
                        "bin": scored[0].get("bin"),
                        "score_m": round(top_score, 2),
                        "second_score_m": (round(second_score, 2) if second_score != float("inf") else None),
                    }
                    logger.info(
                        f"[{scan_id}] tap-facade winner BIN {scored[0].get('bin')} "
                        f"score {top_score:.2f}m (next: {second_score:.2f}m)"
                    )
                else:
                    retrieval_meta["tap_facade_match"] = {
                        "rejected": True,
                        "top_score_m": (round(top_score, 2) if top_score != float("inf") else None),
                        "second_score_m": (round(second_score, 2) if second_score != float("inf") else None),
                    }
        except Exception as e:
            logger.warning(f"[{scan_id}] tap facade match failed (continuing): {e}")

    retrieval_meta["clip_bypassed"] = True

    # The tap is the new high-signal source — same role CLIP used to play,
    # except it's user-driven and grounded in PLUTO's authoritative city
    # plan instead of image similarity. A clean facade match is treated as
    # an *auto-confirm*, not a bias: bypass Grok, bypass picker, the user
    # already told us the answer.
    _tap_winner_bin: Optional[str] = None
    _tap_winner_via: Optional[str] = None
    facade = retrieval_meta.get("tap_facade_match")
    if isinstance(facade, dict) and facade.get("bin") and not facade.get("rejected"):
        _tap_winner_bin = facade["bin"]
        _tap_winner_via = "facade_match"
    elif tap_x is not None and retrieval_meta.get("tap_prefilter", {}) != "no_overlap_fallthrough":
        # Fallback: the coarser IoU pre-filter narrowed to exactly one. Still
        # a strong signal, just less precise than the facade-edge match.
        pf = retrieval_meta.get("tap_prefilter", {})
        if isinstance(pf, dict) and pf.get("kept", 0) == 1:
            _tap_winner_bin = enriched_cands[0].get("bin") if enriched_cands else None
            _tap_winner_via = "iou_singleton"

    # ── 3. Two-signal scoring ──────────────────────────────────────────────────
    scored = scoring.blend_scores(enriched_cands)
    calibrated = scoring.calibrate(scored)
    candidates, show_picker, bail = scoring.sort_and_decide_picker(calibrated)

    # Tap auto-confirm: find the winner in the (possibly multi-candidate)
    # post-scoring list and float it to top-1 with auto-confirm confidence.
    # The tap is a higher-signal disambiguator than facade-similarity ranking
    # so it should *override* score ordering when present, not just bias it.
    if _tap_winner_bin:
        winner_idx = next(
            (i for i, c in enumerate(candidates) if c.get("bin") == _tap_winner_bin),
            None,
        )
        if winner_idx is not None:
            w = candidates[winner_idx]
            # Auto-confirm threshold (settings.confidence_threshold default 0.7).
            # We bump above the picker_abs_threshold so the picker doesn't
            # re-appear, and above the bail threshold so we never bail on a
            # tap-confirmed scan.
            w["confidence"] = max(
                w.get("confidence", 0.0),
                _cfg.picker_abs_threshold + 0.05,  # comfortably above picker line
                0.75,                              # standard auto-confirm floor
            )
            w["verification_method_override"] = f"tap_{_tap_winner_via}"
            # Move to top.
            candidates = [w] + [c for i, c in enumerate(candidates) if i != winner_idx]
            bail = False
            show_picker = False
            retrieval_meta["tap_winner"] = _tap_winner_bin
            retrieval_meta["tap_winner_via"] = _tap_winner_via

    # ── 3a. MapKit POI boost: landmark prior from client ─────────────────────
    # The iOS client runs a MapKit POI search before each scan. If a named
    # landmark is within 30m, its name is sent as `nearest_poi`. This is a
    # near-certain identity signal — boost that candidate above the auto-confirm
    # threshold so geometry ambiguity between same-block neighbors can't override.
    if nearest_poi and candidates and not _tap_winner_bin:
        poi_lower = nearest_poi.lower().strip()
        for i, c in enumerate(candidates):
            cname = (c.get("building_name") or c.get("name") or c.get("address") or "").lower()
            caddr = (c.get("address") or "").lower()
            if poi_lower and (poi_lower in cname or poi_lower in caddr
                              or cname in poi_lower):
                # Float to top with auto-confirm confidence.
                c["confidence"] = max(c.get("confidence", 0.0), 0.85)
                c["verification_method_override"] = "nearest_poi"
                candidates = [c] + [x for j, x in enumerate(candidates) if j != i]
                bail = False
                show_picker = False
                retrieval_meta["poi_winner"] = c.get("bin")
                retrieval_meta["poi_name"] = nearest_poi
                break

    # ── 3b. P4: Grok Vision disambig on close calls ──────────────────────────
    # When CLIP can't separate the top candidates (which is the consulate /
    # row-house / 555-corner failure mode), ask a VLM that can read flags
    # and address numbers. Triggers only on ambiguous scans (~10-30% of total).
    grok_decision: Optional[str] = None
    grok_reason: Optional[str] = None
    # Trigger expanded 2026-05-15: bail/picker OR clustered candidates within
    # ~15m. The rowhouse failure mode is geometrically indistinguishable
    # centroids — we should fire the VLM there even when the top-1 confidence
    # looks fine, because "looks fine" with twin neighbours is exactly when
    # we get a confidently-wrong answer.
    _grok_cluster_trigger = _candidates_clustered_within(
        candidates[:5], _GROK_CLUSTER_RADIUS_M
    )
    # The user's tap is a higher-signal disambiguator than Grok's facade
    # similarity — once the tap has named a winner we trust it and don't
    # ask Grok to second-guess. Grok still fires when there's no tap and
    # the geometry alone can't separate candidates.
    if (
        _cfg.grok_disambig_enabled
        and _tap_winner_bin is None
        and (bail or show_picker or _grok_cluster_trigger)
        and len(candidates) >= 2
    ):
        if _grok_cluster_trigger and not (bail or show_picker):
            retrieval_meta["grok_trigger"] = "cluster_15m"
        try:
            grok_decision, grok_reason = await _try_grok_disambig(
                photo_bytes=photo_bytes,
                top_candidates=candidates[:3],
            )
            if grok_decision is not None and grok_decision != "UNSURE":
                # Gate: only trust the pick if Grok's reason cites readable
                # evidence (numbers, flags, signage). Without that, Grok is
                # matching facade similarity — same failure mode as CLIP, and
                # exactly the 555 Park regression case.
                if (
                    _cfg.grok_require_textual_evidence
                    and not _has_textual_evidence(grok_reason)
                ):
                    retrieval_meta["grok_pick"] = "GATED_GENERIC"
                    retrieval_meta["grok_reason"] = grok_reason
                    logger.info(
                        f"Grok pick {grok_decision} gated (no textual evidence): {grok_reason!r}"
                    )
                else:
                    idx = {"A": 0, "B": 1, "C": 2}.get(grok_decision)
                    if idx is not None and idx < len(candidates):
                        chosen = candidates[idx]
                        # Bump bounded by config — 0.65 default. Lower than the
                        # original 0.90 so a soft Grok pick doesn't drown out
                        # the picker UX for ambiguous cases.
                        chosen["confidence"] = max(
                            chosen.get("confidence", 0.0), _cfg.grok_confidence_bump
                        )
                        chosen["grok_reason"] = grok_reason
                        candidates = [chosen] + [
                            c for i, c in enumerate(candidates) if i != idx
                        ]
                        # Recompute bail/picker against the bumped confidence.
                        new_conf = chosen["confidence"]
                        bail = new_conf < _cfg.no_confident_match_threshold
                        show_picker = new_conf < _cfg.picker_abs_threshold
                        retrieval_meta["grok_pick"] = grok_decision
                        retrieval_meta["grok_reason"] = grok_reason
                        logger.info(
                            f"Grok Vision picked {grok_decision} ({grok_reason!r}) conf={new_conf:.2f}"
                        )
            elif grok_decision == "UNSURE":
                retrieval_meta["grok_pick"] = "UNSURE"
                retrieval_meta["grok_reason"] = grok_reason
                logger.info(f"Grok Vision returned UNSURE ({grok_reason!r})")
        except Exception as e:
            logger.warning(f"Grok disambig failed (continuing with CLIP rank): {e}")
    elif not _cfg.grok_disambig_enabled:
        retrieval_meta["grok_pick"] = "DISABLED"

    # ── 4. Resolve thumbnails ──────────────────────────────────────────────────
    # On bail or any picker situation return top-5 so the map picker has more
    # candidates to render. (We used to return 3 unless bailing, which left the
    # P5 map picker under-populated whenever margin-bail tripped.)
    n_out = 5 if (bail or show_picker) else 3
    out = candidates[:n_out]
    for c in out:
        c["bearing_offset_deg"] = c.get("bearing_difference")
        c["evidence"] = []
        # Name fallback: never leave a candidate with no display string — this is what
        # surfaced as "Unknown Building" in the picker. Order: name → address → BIN.
        if not c.get("name"):
            c["name"] = c.get("address") or (f"BIN {c.get('bin')}" if c.get("bin") else None)

    # Fetch footprint GeoJSON whenever a picker will be shown so the iOS map
    # picker can render polygons. Cheap query (~5 BINs by primary key).
    fetch_geom_task = None
    if bail or show_picker:
        from services.geospatial import get_footprints_for_bins
        fetch_geom_task = asyncio.create_task(
            get_footprints_for_bins([str(c.get("bin") or "") for c in out])
        )

    await _resolve_thumbnails(out)

    if fetch_geom_task is not None:
        try:
            geom_by_bin = await fetch_geom_task
            for c in out:
                geo = geom_by_bin.get(str(c.get("bin") or ""))
                if geo:
                    c["footprint_geojson"] = geo
        except Exception as e:
            logger.warning(f"footprint geojson fetch failed: {e}")

    # ── 5. Telemetry ──────────────────────────────────────────────────────────
    # Tap auto-confirm wins the verification_method label so the flywheel can
    # separate tap-driven scans from geometry/CLIP-driven ones — they're
    # different data products (user-labeled vs algorithm-labeled).
    if out and out[0].get("verification_method_override"):
        verification_method = out[0]["verification_method_override"]
    elif bail:
        verification_method = "no_confident_match"
    else:
        verification_method = f"pipeline_v3_{clip_method}"
    total_ms = int((time.time() - t_start) * 1000)
    telemetry.log_scan(
        scan_id=scan_id,
        top3_bins=[c["bin"] for c in out[:3]],
        score_breakdowns=[c.get("score_breakdown", {}) for c in out[:3]],
        cone_deg=retrieval_meta.get("cone_deg", _cfg.base_cone_deg),
        used_ring_fallback=retrieval_meta.get("used_ring_fallback", False),
        clip_method=clip_method,
        processing_time_ms=total_ms,
        verification_method=verification_method,
        top_confidence=out[0].get("confidence", 0.0) if out else 0.0,
        show_picker=show_picker or bail,
    )

    return {
        "matches": out,
        "show_picker": show_picker or bail,
        "verification_method": verification_method,
        "retrieval_meta": retrieval_meta,
        "processing_time_ms": total_ms,
        "clip_cost_usd": clip_cost,
        "bail": bail,
    }


def _r2_aerial_url(bin_val: str) -> Optional[str]:
    if not bin_val:
        return None
    clean = bin_val.strip().replace(".0", "")
    return _cfg.r2_aerial_template.format(bin=clean)


async def _resolve_thumbnails(candidates: List[Dict]) -> None:
    """
    Resolve thumbnail_url for each candidate from R2 only.

    Priority (HEAD checks, ~1.5s timeout each, all candidates in parallel):
      1. Cached Street View at thumbs/{bin}/streetview.jpg
      2. R2 aerial at {bin}/0deg_40pitch.jpg
      3. null — client renders a placeholder tile instead of a spinner

    We no longer fetch Street View live inside the request. That was the
    "picker spinner" failure mode. Missing thumbnails get backfilled offline.
    """
    await asyncio.gather(*(_resolve_one_thumbnail(c) for c in candidates))


async def _resolve_one_thumbnail(c: Dict) -> None:
    bin_val = c.get("bin", "")
    if not bin_val:
        c["thumbnail_url"] = None
        return

    clean_bin = str(bin_val).strip().replace(".0", "")
    sv_url = f"https://pub-234fc67c039149b2b46b864a1357763d.r2.dev/thumbs/{clean_bin}/streetview.jpg"
    aerial_url = _r2_aerial_url(bin_val)

    async with httpx.AsyncClient(timeout=1.5) as client:
        for url in (sv_url, aerial_url):
            if not url:
                continue
            try:
                head = await client.head(url)
                if head.status_code == 200:
                    c["thumbnail_url"] = url
                    return
            except Exception:
                continue

    c["thumbnail_url"] = None


async def _try_grok_disambig(
    *,
    photo_bytes: bytes,
    top_candidates: List[Dict],
) -> tuple[Optional[str], Optional[str]]:
    """
    Call Grok Vision with the user's photo + top candidate references.
    Returns (choice, reason). Choice is "A"|"B"|"C"|"UNSURE" or None on failure.
    On a confident pick, also attaches Grok-generated lore to the chosen
    candidate's `storytelling` field — so the v2 router skips its separate
    lore call (one Grok request, both jobs).
    """
    from services.grok import grok_vision_pick
    from services.reference_image_chain import fetch_reference_image

    # Backend-only Street View fetch. Used by Grok's vision pick so it has
    # something visual to compare against the user photo when no tap mask is
    # present (Grok read "121" on the Consulate awning this way). This is
    # the last live path that talks to maps.googleapis.com — iOS purged GSV
    # in Phase 8. Fires only on tap-less ambiguous scans, which should be
    # a shrinking slice now that tap-to-pick autoconfirms cleanly.
    # Pull LPC-sourced landmark text for each candidate up front so Grok has
    # corroborating facts (year, architect, designation, history) and can't
    # invent stuff. The lore_generator already has this helper.
    from services.lore_generator import _get_raw_chunks

    async def _cand_bytes(c: Dict) -> Optional[Dict]:
        lat = c.get("geocoded_lat") or c.get("latitude")
        lng = c.get("geocoded_lng") or c.get("longitude")
        if lat is None or lng is None:
            return None

        from services.clip_disambiguation import fetch_street_view_image

        async def _google(la: float, ln: float) -> Optional[bytes]:
            return await fetch_street_view_image(la, ln, 0)

        # Fetch the reference image and the LPC chunks in parallel.
        img_and_chunks = await asyncio.gather(
            fetch_reference_image(
                lat=float(lat), lng=float(lng), bbl=c.get("bbl"),
                google_fallback=_google,
            ),
            _get_raw_chunks(str(c.get("bin") or ""), c.get("name") or c.get("address")),
        )
        (img, _src), chunks = img_and_chunks
        if not img:
            return None

        # Compact fact sheet so Grok grounds lore in real metadata, not vibes.
        facts = []
        if c.get("year_built"): facts.append(f"built {c['year_built']}")
        if c.get("style"): facts.append(str(c['style']))
        if c.get("architect"): facts.append(f"architect {c['architect']}")
        if c.get("use"): facts.append(str(c['use']))
        if c.get("materials"): facts.append(str(c['materials']))
        if c.get("is_landmark"): facts.append("NYC Landmark")
        # Trim the LPC chunk to a reasonable size — Grok doesn't need 3000 chars.
        chunk_excerpt = (chunks[:800] + "…") if chunks and len(chunks) > 800 else (chunks or "")

        context_parts = []
        if facts:
            context_parts.append("; ".join(facts))
        # If our buildings DB already has curated storytelling for this BIN,
        # hand it to Grok as authoritative ground truth. Lore should be a
        # refinement of this with visible-detail grounding, not a rewrite.
        existing_story = c.get("storytelling")
        if existing_story and isinstance(existing_story, str) and len(existing_story) > 20:
            existing_excerpt = existing_story[:600] + ("…" if len(existing_story) > 600 else "")
            context_parts.append(f"Supabase storytelling: {existing_excerpt}")
        if chunk_excerpt:
            context_parts.append(f"LPC notes: {chunk_excerpt}")
        # Coordinate is the disambiguating ID — include it verbatim so Grok
        # can web-search against the exact address.
        context_parts.append(f"coords: ({float(lat):.5f}, {float(lng):.5f})")

        return {
            "address": c.get("name") or c.get("address"),
            "image_bytes": img,
            "building_context": " | ".join(context_parts),
        }

    cand_payloads = await asyncio.gather(
        *(_cand_bytes(c) for c in top_candidates), return_exceptions=False
    )
    cand_payloads = [c for c in cand_payloads if c]
    if len(cand_payloads) < 2:
        return None, None

    result = await grok_vision_pick(
        user_photo_bytes=photo_bytes,
        candidates=cand_payloads,
    )
    if not result:
        return None, None

    choice = result.get("choice")
    reason = result.get("reason")
    lore = result.get("lore") or ""

    # If Grok picked a winner, stash its lore on the candidate so the v2 router
    # uses it directly and skips a separate generic lore call.
    if choice in ("A", "B", "C") and lore:
        idx = {"A": 0, "B": 1, "C": 2}[choice]
        if idx < len(top_candidates):
            top_candidates[idx]["storytelling"] = lore
            top_candidates[idx]["lore_source"] = "grok_vision_disambig"

    return choice, reason


def _empty_response(retrieval_meta: dict, t_start: float) -> Dict[str, Any]:
    return {
        "matches": [],
        "show_picker": True,
        "verification_method": "pipeline_v3_no_candidates",
        "retrieval_meta": retrieval_meta,
        "processing_time_ms": int((time.time() - t_start) * 1000),
        "clip_cost_usd": 0.0,
        "error": "no_candidates",
    }

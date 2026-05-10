"""
Pipeline orchestrator: image + GPS → ranked candidates → calibrated response.

Flow:
  1. retrieval.get_candidates    — adaptive cone + ring fallback
  2. enrich_candidates            — metadata from buildings DB + PLUTO
  3. embedding lookup             — CLIP image-image similarity (existing logic)
  4. perception.extract           — CLIP zero-shot visual attributes
  5. scoring.blend_scores         — multi-signal weighted blend
  6. scoring.calibrate            — temperature softmax
  7. scoring.sort_and_decide      — sort + picker trigger
  8. thumbnails                   — resolve per-candidate thumbnail URL
  9. evidence chips               — perception ↔ metadata alignment
  10. telemetry

Input/output are plain dicts — no FastAPI types leak in here.
The router in routers/scan_v2.py is the only caller.
"""

import asyncio
import logging
import time
from typing import Optional, Tuple, List, Dict, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models.session import AsyncSessionLocal
from pipeline import retrieval, perception as perc_module, scoring, telemetry
from pipeline.config import get_pipeline_config
from services.geospatial_v2 import enrich_candidates_with_metadata
from services import clip_disambiguation

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()


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
) -> Dict[str, Any]:
    """
    Execute the full matching pipeline.

    Returns a dict with:
      matches, show_picker, verification_method, perception,
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

    # ── 2. Metadata enrichment + CLIP image similarity (parallel) ──────────────
    top3 = raw_candidates[:3]

    async def _clip():
        async with AsyncSessionLocal() as clip_sess:
            return await clip_disambiguation.disambiguate_candidates(
                session=clip_sess,
                user_photo_url=user_photo_url,
                candidates=top3,
                user_lat=lat,
                user_lng=lng,
                user_bearing=bearing
            )

    enriched_cands, clip_result, perception = await asyncio.gather(
        enrich_candidates_with_metadata(session, raw_candidates),
        _clip(),
        perc_module.extract_perception(photo_bytes),
    )

    clip_cost = clip_result.get("cost_usd", 0.0)
    clip_method = clip_result.get("method", "unknown")

    # Merge CLIP scores back onto enriched candidates
    clip_by_bin = {c["bin"]: c for c in clip_result.get("matches", [])}
    for c in enriched_cands:
        if c["bin"] in clip_by_bin:
            c["clip_similarity"] = clip_by_bin[c["bin"]].get("clip_similarity", 0.0)

    # ── 2b. Preliminary score to decide if ring fallback is needed ─────────────
    # Score cone candidates first so we can check the margin, not just raw CLIP.
    # The broken pattern was: wrong buildings CLIP at 65-75 each, threshold=55,
    # so fallback never fired even though all 3 candidates were wrong.
    pre_scored = scoring.blend_scores(list(enriched_cands), perception, pitch, bearing)
    pre_calibrated = scoring.calibrate(pre_scored)
    _, pre_show_picker = scoring.sort_and_decide_picker(pre_calibrated)

    top_clip = max((c.get("clip_similarity", 0.0) for c in enriched_cands[:3]), default=0.0)

    # Ring fallback triggers when:
    #   (a) CLIP top score is genuinely low — none of the cone candidates match well
    #   (b) picker would be shown anyway — margin is thin, worth a wider search
    #   (c) too few cone candidates — cone may have missed the building entirely
    should_ring = (
        top_clip < _cfg.ring_fallback_clip_threshold * 100  # e.g. < 35 (tuned down from 55)
        or pre_show_picker                                    # ambiguous result → search wider
        or len(raw_candidates) < _cfg.ring_fallback_min_candidates
    )

    if should_ring:
        reason = (
            f"low_clip={top_clip:.1f}" if top_clip < _cfg.ring_fallback_clip_threshold * 100
            else ("picker_ambiguous" if pre_show_picker else "too_few")
        )
        logger.info(f"Ring fallback triggered ({reason})")
        extra_raw, _ = await retrieval.ring_query_direct(
            session, lat, lng, bearing, _cfg.max_distance_m, _cfg.max_candidates
        )
        existing_bins = {c["bin"] for c in enriched_cands}
        new_raws = [c for c in extra_raw if c["bin"] not in existing_bins]
        if new_raws:
            new_enriched = await enrich_candidates_with_metadata(session, new_raws)
            async with AsyncSessionLocal() as ring_sess:
                ring_clip = await clip_disambiguation.disambiguate_candidates(
                    session=ring_sess,
                    user_photo_url=user_photo_url,
                    candidates=new_raws[:5],  # wider batch for ring
                    user_lat=lat, user_lng=lng, user_bearing=bearing,
                )
            ring_clip_by_bin = {c["bin"]: c for c in ring_clip.get("matches", [])}
            for c in new_enriched:
                if c["bin"] in ring_clip_by_bin:
                    c["clip_similarity"] = ring_clip_by_bin[c["bin"]].get("clip_similarity", 0.0)
            enriched_cands.extend(new_enriched)
            clip_cost += ring_clip.get("cost_usd", 0.0)
            retrieval_meta["used_ring_fallback"] = True
            retrieval_meta["ring_reason"] = reason
            retrieval_meta["ring_added"] = len(new_raws)

    # ── 3. Multi-signal scoring ────────────────────────────────────────────────
    scored = scoring.blend_scores(enriched_cands, perception, pitch, bearing)
    calibrated = scoring.calibrate(scored)
    candidates, show_picker = scoring.sort_and_decide_picker(calibrated)

    # ── 4. Resolve thumbnails + evidence chips ─────────────────────────────────
    top3_out = candidates[:3]
    for c in top3_out:
        c["evidence"] = perception.evidence_for_candidate(c)
        c["bearing_offset_deg"] = c.get("bearing_difference")

    # Resolve thumbnails synchronously — picker is shown immediately, spinner is useless.
    # For each candidate: try R2 aerial (HEAD check). If missing, fetch Street View,
    # upload to R2, and return that URL. Falls back to R2 aerial URL if all else fails.
    await _resolve_thumbnails(top3_out, bearing)

    # ── 5. Telemetry ──────────────────────────────────────────────────────────
    total_ms = int((time.time() - t_start) * 1000)
    telemetry.log_scan(
        scan_id=scan_id,
        top3_bins=[c["bin"] for c in top3_out],
        score_breakdowns=[c.get("score_breakdown", {}) for c in top3_out],
        cone_deg=retrieval_meta.get("cone_deg", _cfg.base_cone_deg),
        used_ring_fallback=retrieval_meta.get("used_ring_fallback", False),
        clip_method=clip_method,
        perception_summary=perception.summary_line(),
        processing_time_ms=total_ms,
        verification_method=f"pipeline_v3_{clip_method}",
        top_confidence=top3_out[0].get("confidence", 0.0) if top3_out else 0.0,
        show_picker=show_picker,
    )

    return {
        "matches": top3_out,
        "show_picker": show_picker,
        "verification_method": f"pipeline_v3_{clip_method}",
        "perception": perception.to_dict(),
        "retrieval_meta": retrieval_meta,
        "processing_time_ms": total_ms,
        "clip_cost_usd": clip_cost,
    }


def _r2_aerial_url(bin_val: str) -> Optional[str]:
    if not bin_val:
        return None
    clean = bin_val.strip().replace(".0", "")
    return _cfg.r2_aerial_template.format(bin=clean)


async def _resolve_thumbnails(candidates: List[Dict], bearing: float) -> None:
    """
    Resolve thumbnail_url for each candidate synchronously before returning
    the response — the picker needs real images immediately, not on retry.

    Priority:
      1. R2 aerial (instant if exists — just a HEAD check)
      2. Street View fetched live → uploaded to R2 → URL returned
      3. R2 aerial URL as last resort (client shows spinner, but this rarely happens)
    """
    try:
        from services.reference_images import fetch_street_view
        from utils.storage import upload_image
        sv_available = True
    except ImportError:
        sv_available = False
        logger.warning("Street View unavailable — falling back to R2 aerial URLs only")

    tasks = [_resolve_one_thumbnail(c, bearing, sv_available) for c in candidates]
    await asyncio.gather(*tasks)


async def _resolve_one_thumbnail(c: Dict, bearing: float, sv_available: bool) -> None:
    from services.reference_images import fetch_street_view
    from utils.storage import upload_image

    bin_val = c.get("bin", "")
    lat = c.get("geocoded_lat") or c.get("latitude")
    lng = c.get("geocoded_lng") or c.get("longitude")
    r2_aerial = _r2_aerial_url(bin_val)

    # Check cached Street View R2 key first (fastest path for repeat scans)
    if bin_val:
        clean_bin = bin_val.strip().replace(".0", "")
        sv_r2_url = f"https://pub-234fc67c039149b2b46b864a1357763d.r2.dev/thumbs/{clean_bin}/streetview.jpg"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                sv_head = await client.head(sv_r2_url)
            if sv_head.status_code == 200:
                c["thumbnail_url"] = sv_r2_url
                return
        except Exception:
            pass

    # Check R2 aerial
    if r2_aerial:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                head = await client.head(r2_aerial)
            if head.status_code == 200:
                c["thumbnail_url"] = r2_aerial
                return
        except Exception:
            pass

    # Neither cached — fetch Street View live, upload, return URL
    if sv_available and lat and lng:
        try:
            sv_bytes = await fetch_street_view(
                lat=float(lat), lng=float(lng), bearing=bearing,
                size=_cfg.street_view_size,
                pitch=_cfg.street_view_pitch,
                fov=_cfg.street_view_fov,
            )
            if sv_bytes and bin_val:
                clean_bin = bin_val.strip().replace(".0", "")
                sv_r2_key = f"thumbs/{clean_bin}/streetview.jpg"
                sv_url = await upload_image(sv_bytes, sv_r2_key)
                if sv_url:
                    c["thumbnail_url"] = sv_url
                    logger.info(f"Fetched live Street View thumbnail for BIN {clean_bin}")
                    return
        except Exception as e:
            logger.debug(f"Street View fetch failed for BIN {bin_val}: {e}")

    # Last resort — R2 aerial URL (client shows spinner if 404)
    c["thumbnail_url"] = r2_aerial


def _empty_response(retrieval_meta: dict, t_start: float) -> Dict[str, Any]:
    return {
        "matches": [],
        "show_picker": True,
        "verification_method": "pipeline_v3_no_candidates",
        "perception": None,
        "retrieval_meta": retrieval_meta,
        "processing_time_ms": int((time.time() - t_start) * 1000),
        "clip_cost_usd": 0.0,
        "error": "no_candidates",
    }

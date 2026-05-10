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

    # ── 2b. Ring fallback if top CLIP score is still low ───────────────────────
    top_clip = max((c.get("clip_similarity", 0.0) for c in enriched_cands[:3]), default=0.0)
    if top_clip < _cfg.ring_fallback_clip_threshold * 100:
        logger.info(f"Top CLIP={top_clip:.1f} < threshold — triggering ring fallback")
        extra_raw, ring_meta = await retrieval.get_candidates(
            session, lat, lng, bearing, pitch,
            gps_accuracy_m, heading_accuracy_deg, lens_type,
            top_clip_score=top_clip / 100.0,
        )
        existing_bins = {c["bin"] for c in enriched_cands}
        new_raws = [c for c in extra_raw if c["bin"] not in existing_bins]
        if new_raws:
            new_enriched = await enrich_candidates_with_metadata(session, new_raws)
            # Run CLIP on the new batch
            async with AsyncSessionLocal() as ring_sess:
                ring_clip = await clip_disambiguation.disambiguate_candidates(
                    session=ring_sess,
                    user_photo_url=user_photo_url,
                    candidates=new_raws[:3],
                    user_lat=lat, user_lng=lng, user_bearing=bearing,
                )
            ring_clip_by_bin = {c["bin"]: c for c in ring_clip.get("matches", [])}
            for c in new_enriched:
                if c["bin"] in ring_clip_by_bin:
                    c["clip_similarity"] = ring_clip_by_bin[c["bin"]].get("clip_similarity", 0.0)
            enriched_cands.extend(new_enriched)
            clip_cost += ring_clip.get("cost_usd", 0.0)
            retrieval_meta["used_ring_fallback"] = True

    # ── 3. Multi-signal scoring ────────────────────────────────────────────────
    scored = scoring.blend_scores(enriched_cands, perception, pitch, bearing)
    calibrated = scoring.calibrate(scored)
    candidates, show_picker = scoring.sort_and_decide_picker(calibrated)

    # ── 4. Resolve thumbnails + evidence chips ─────────────────────────────────
    top3_out = candidates[:3]
    for c in top3_out:
        c["thumbnail_url"] = _r2_aerial_url(c["bin"])
        c["evidence"] = perception.evidence_for_candidate(c)
        c["bearing_offset_deg"] = c.get("bearing_difference")

    # Fire-and-forget: backfill Street View thumbnails for any BINs missing R2 aerial.
    # Next scan for those buildings will serve the Street View image from R2.
    asyncio.ensure_future(_backfill_street_view_thumbnails(top3_out, bearing))

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
    """Construct the R2 aerial thumbnail URL for a BIN."""
    if not bin_val:
        return None
    clean = bin_val.strip().replace(".0", "")
    return _cfg.r2_aerial_template.format(bin=clean)


async def _backfill_street_view_thumbnails(candidates: List[Dict], bearing: float) -> None:
    """
    For each candidate whose R2 aerial thumbnail doesn't exist (HTTP 404),
    fetch a Street View image at the candidate's centroid and upload it to R2
    under thumbs/{bin}/streetview.jpg. Non-blocking — runs after the response
    is returned to the client. The API key stays server-side.
    """
    try:
        from services.reference_images import fetch_street_view
        from utils.storage import upload_image
    except ImportError:
        logger.warning("Street View backfill skipped — imports unavailable")
        return

    for c in candidates:
        bin_val = c.get("bin", "")
        lat = c.get("geocoded_lat") or c.get("latitude")
        lng = c.get("geocoded_lng") or c.get("longitude")
        if not (bin_val and lat and lng):
            continue

        r2_url = _r2_aerial_url(bin_val)
        if not r2_url:
            continue

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                head = await client.head(r2_url)
            if head.status_code == 200:
                continue  # R2 aerial exists — no Street View needed
        except Exception:
            pass  # Network error checking R2 — skip this candidate

        # R2 aerial missing — fetch Street View and cache it
        try:
            sv_bytes = await fetch_street_view(
                lat=float(lat), lng=float(lng), bearing=bearing,
                size=_cfg.street_view_size,
                pitch=_cfg.street_view_pitch,
                fov=_cfg.street_view_fov,
            )
            if sv_bytes:
                clean_bin = bin_val.strip().replace(".0", "")
                sv_r2_key = f"thumbs/{clean_bin}/streetview.jpg"
                sv_url = await upload_image(sv_bytes, sv_r2_key)
                if sv_url:
                    # Update in-place so future scans return the Street View URL
                    c["thumbnail_url"] = sv_url
                    logger.info(f"Backfilled Street View thumbnail for BIN {clean_bin}")
        except Exception as e:
            logger.debug(f"Street View backfill failed for BIN {bin_val}: {e}")


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

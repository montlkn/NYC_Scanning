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
import time
from typing import Optional, Tuple, List, Dict, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models.session import AsyncSessionLocal
from pipeline import retrieval, scoring, telemetry
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

    # ── 2. Enrich metadata + fast-path CLIP on top-3 (parallel) ────────────────
    # Fast path: if the top-3 by visibility-score already has a clear visual
    # winner, we skip ranking the rest of the cone. Saves latency on easy scans.
    top3 = raw_candidates[:3]

    async def _clip_top3():
        async with AsyncSessionLocal() as clip_sess:
            return await clip_disambiguation.disambiguate_candidates(
                session=clip_sess,
                user_photo_url=user_photo_url,
                candidates=top3,
                user_lat=lat,
                user_lng=lng,
                user_bearing=bearing,
            )

    enriched_cands, clip_result = await asyncio.gather(
        enrich_candidates_with_metadata(session, raw_candidates),
        _clip_top3(),
    )

    clip_cost = clip_result.get("cost_usd", 0.0)
    clip_method = clip_result.get("method", "unknown")

    # Merge top-3 CLIP scores back onto the enriched candidates.
    clip_by_bin = {c["bin"]: c for c in clip_result.get("matches", [])}
    for c in enriched_cands:
        if c["bin"] in clip_by_bin:
            c["clip_similarity"] = clip_by_bin[c["bin"]].get("clip_similarity", 0.0)

    # ── 2a. Fast-path check ───────────────────────────────────────────────────
    # Inspect just the top-3 CLIP scores. If the leader is clearly winning,
    # don't bother CLIP-ranking the rest of the cone.
    top3_clip = sorted(
        [c.get("clip_similarity", 0.0) for c in enriched_cands[:3]],
        reverse=True,
    )
    fast_path = (
        len(top3_clip) >= 2
        and top3_clip[0] / 100.0 >= _cfg.fast_path_clip_threshold
        and (top3_clip[0] - top3_clip[1]) / 100.0 >= _cfg.fast_path_clip_margin
    )

    retrieval_meta["fast_path"] = fast_path
    if not fast_path:
        # CLIP-rank the rest of the cone (the actual 555 Park fix). Most of
        # these will hit cached embeddings from F1 so the cost is near-zero.
        already_ranked = set(clip_by_bin.keys())
        rest = [
            c for c in enriched_cands
            if c.get("bin") and c["bin"] not in already_ranked
        ][: max(0, _cfg.full_cone_clip_pool - len(already_ranked))]
        if rest:
            logger.info(
                f"Fast path skipped; CLIP-ranking {len(rest)} more cone candidates"
            )
            async with AsyncSessionLocal() as wide_sess:
                wider = await clip_disambiguation.disambiguate_candidates(
                    session=wide_sess,
                    user_photo_url=user_photo_url,
                    candidates=rest,
                    user_lat=lat,
                    user_lng=lng,
                    user_bearing=bearing,
                )
            wider_by_bin = {c["bin"]: c for c in wider.get("matches", [])}
            for c in enriched_cands:
                if c["bin"] in wider_by_bin:
                    c["clip_similarity"] = wider_by_bin[c["bin"]].get(
                        "clip_similarity", 0.0
                    )
            clip_cost += wider.get("cost_usd", 0.0)
            retrieval_meta["wide_clip_ranked"] = len(rest)

    # ── 3. Two-signal scoring ──────────────────────────────────────────────────
    scored = scoring.blend_scores(enriched_cands)
    calibrated = scoring.calibrate(scored)
    candidates, show_picker, bail = scoring.sort_and_decide_picker(calibrated)

    # ── 3a. P4: Grok Vision disambig on close calls ──────────────────────────
    # When CLIP can't separate the top candidates (which is the consulate /
    # row-house / 555-corner failure mode), ask a VLM that can read flags
    # and address numbers. Triggers only on ambiguous scans (~10-30% of total).
    grok_decision: Optional[str] = None
    grok_reason: Optional[str] = None
    if (bail or show_picker) and len(candidates) >= 2:
        try:
            grok_decision, grok_reason = await _try_grok_disambig(
                photo_bytes=photo_bytes,
                top_candidates=candidates[:3],
            )
            if grok_decision is not None and grok_decision != "UNSURE":
                # Grok picked a winner. Reorder so the chosen candidate is #1
                # and bump its confidence so the client doesn't bail.
                idx = {"A": 0, "B": 1, "C": 2}.get(grok_decision)
                if idx is not None and idx < len(candidates):
                    chosen = candidates[idx]
                    chosen["confidence"] = max(chosen.get("confidence", 0.0), 0.90)
                    chosen["grok_reason"] = grok_reason
                    # Move chosen to front, keep the rest in their current order.
                    candidates = [chosen] + [
                        c for i, c in enumerate(candidates) if i != idx
                    ]
                    bail = False
                    show_picker = False
                    retrieval_meta["grok_pick"] = grok_decision
                    retrieval_meta["grok_reason"] = grok_reason
                    logger.info(f"Grok Vision picked {grok_decision} ({grok_reason!r})")
            elif grok_decision == "UNSURE":
                retrieval_meta["grok_pick"] = "UNSURE"
                retrieval_meta["grok_reason"] = grok_reason
                logger.info(f"Grok Vision returned UNSURE ({grok_reason!r})")
        except Exception as e:
            logger.warning(f"Grok disambig failed (continuing with CLIP rank): {e}")

    # ── 4. Resolve thumbnails ──────────────────────────────────────────────────
    # On bail return top-5 so the map picker has more options to render.
    n_out = 5 if bail else 3
    out = candidates[:n_out]
    for c in out:
        c["bearing_offset_deg"] = c.get("bearing_difference")
        c["evidence"] = []
        # Name fallback: never leave a candidate with no display string — this is what
        # surfaced as "Unknown Building" in the picker. Order: name → address → BIN.
        if not c.get("name"):
            c["name"] = c.get("address") or (f"BIN {c.get('bin')}" if c.get("bin") else None)

    await _resolve_thumbnails(out)

    # ── 5. Telemetry ──────────────────────────────────────────────────────────
    verification_method = (
        "no_confident_match" if bail else f"pipeline_v3_{clip_method}"
    )
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

    # Fetch reference image bytes for each candidate. Reuses the chain so we
    # benefit from Mapillary first, Google only as fallback. Most BINs already
    # have a cached embedding *image* in R2 — but we need raw bytes here, not
    # the embedding. The chain re-fetches; on hot blocks this is a few hundred
    # ms total across all candidates (Mapillary is fast for repeats).
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

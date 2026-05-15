"""
Structured per-scan telemetry.

Logs to:
 1. The Python logger (always, at DEBUG level — visible in Modal logs)
 2. The analytics service (PostHog, if configured)

Key metrics:
 - was_top1_correct  ← the success metric for WS-2
 - was_in_top3       ← retrieval diagnostic
 - retrieval_failure ← right answer not in top-3 at all (cone/GPS problem)
 - ranking_failure   ← right answer in top-3 but not #1 (scoring problem)
"""

import logging
from typing import List, Optional, Dict, Any

from services.analytics import track_scan, track_confirmation

logger = logging.getLogger(__name__)


def log_scan(
    scan_id: str,
    top3_bins: List[str],
    score_breakdowns: List[Dict[str, Any]],
    cone_deg: float,
    used_ring_fallback: bool,
    clip_method: str,
    processing_time_ms: int,
    verification_method: str,
    top_confidence: float,
    show_picker: bool,
):
    logger.debug(
        f"[telemetry:{scan_id}] "
        f"top3={top3_bins} "
        f"cone={cone_deg:.0f}° ring={used_ring_fallback} "
        f"clip={clip_method} "
        f"conf={top_confidence:.3f} picker={show_picker} "
        f"{processing_time_ms}ms"
    )

    track_scan(scan_id, {
        "top3_bins": top3_bins,
        "top_confidence": top_confidence,
        "cone_deg": cone_deg,
        "used_ring_fallback": used_ring_fallback,
        "clip_method": clip_method,
        "show_picker": show_picker,
        "processing_time_ms": processing_time_ms,
        "verification_method": verification_method,
        "score_breakdowns": score_breakdowns,
    })


def log_confirmation(
    scan_id: str,
    top3_bins: List[str],
    confirmed_bin: str,
):
    was_top1_correct = bool(top3_bins) and top3_bins[0] == confirmed_bin
    was_in_top3 = confirmed_bin in top3_bins
    retrieval_failure = not was_in_top3
    ranking_failure = was_in_top3 and not was_top1_correct

    logger.debug(
        f"[telemetry:{scan_id}] confirm BIN={confirmed_bin} "
        f"top1_correct={was_top1_correct} in_top3={was_in_top3} "
        f"retrieval_fail={retrieval_failure} ranking_fail={ranking_failure}"
    )

    track_confirmation(scan_id, confirmed_bin, was_top_match=was_top1_correct)

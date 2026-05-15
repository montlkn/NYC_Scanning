"""
Two-signal scoring, softmax calibration, and picker-trigger decision.

Score formula:
    raw = w_footprint·footprint + w_clip_image·clip_image
"""

import numpy as np
import logging
from typing import List, Dict, Any

from pipeline.config import get_pipeline_config

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()


def blend_scores(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compute raw blended score for each candidate.
    Adds 'raw_score' and 'score_breakdown' keys.
    """
    for c in candidates:
        fp = (c.get("footprint_score") or 0.0) / 100.0       # normalise 0-100 → 0-1
        clip_img = (c.get("clip_similarity") or 0.0) / 100.0

        raw = _cfg.w_footprint * fp + _cfg.w_clip_image * clip_img

        c["raw_score"] = round(raw, 4)
        c["score_breakdown"] = {
            "footprint": round(_cfg.w_footprint * fp, 4),
            "clip_image": round(_cfg.w_clip_image * clip_img, 4),
        }

    return candidates


def calibrate(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Temperature-scaled softmax over raw_score.
    Adds 'confidence' key in [0, 1] — these should now spread meaningfully.
    """
    if not candidates:
        return candidates

    scores = np.array([c.get("raw_score", 0.0) for c in candidates])
    scaled = scores / _cfg.softmax_temperature
    # Stable softmax
    scaled -= scaled.max()
    exp_scores = np.exp(scaled)
    probs = exp_scores / exp_scores.sum()

    for c, p in zip(candidates, probs):
        c["confidence"] = round(float(p), 4)

    return candidates


def sort_and_decide_picker(
    candidates: List[Dict[str, Any]]
) -> tuple[List[Dict[str, Any]], bool, bool]:
    """
    Sort by confidence descending. Decide:
      - whether to show the percentage-style picker (close call between candidates), and
      - whether to bail to the map-picker UX (no candidate is confidently right).

    Returns (sorted_candidates, show_picker, bail).
    `bail=True` means top-1 confidence is below the no_confident_match threshold
    and the client should route to the map picker (P5) instead of trusting any
    of the listed candidates.
    """
    candidates = sorted(candidates, key=lambda x: x.get("confidence", 0), reverse=True)

    if not candidates:
        return candidates, True, True

    top = candidates[0]["confidence"]
    bail = top < _cfg.no_confident_match_threshold

    if len(candidates) == 1:
        return candidates, top < _cfg.picker_abs_threshold, bail

    margin = top - candidates[1]["confidence"]
    show_picker = (
        margin < _cfg.picker_margin_threshold
        or top < _cfg.picker_abs_threshold
    )

    return candidates, show_picker, bail

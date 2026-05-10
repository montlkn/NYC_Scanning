"""
Multi-signal scoring, softmax calibration, and picker-trigger decision.

Score formula (all weights in pipeline/config.py):
    raw = w_f·footprint + w_v·clip_image + w_p·clip_perception
        + w_g·ground_plane - w_o·occlusion_penalty

Then temperature-scaled softmax so outputs sum to 1.
Picker is triggered by margin (top1 - top2) or absolute threshold — not by
a raw percentage — so the user only sees a choice when it actually matters.
"""

import numpy as np
import logging
from typing import List, Dict, Any, Optional

from pipeline.config import get_pipeline_config
from pipeline.perception import PerceptionAttributes, perception_match_score

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()


def blend_scores(
    candidates: List[Dict[str, Any]],
    perception: Optional[PerceptionAttributes],
    phone_pitch: float,
    user_bearing: float,
) -> List[Dict[str, Any]]:
    """
    Compute raw blended score for each candidate.
    Adds 'raw_score' and 'score_breakdown' keys.
    """
    from pipeline.retrieval import ground_plane_score  # avoid circular at module level

    for c in candidates:
        fp = (c.get("footprint_score") or 0.0) / 100.0  # normalise 0-100 → 0-1
        clip_img = (c.get("clip_similarity") or 0.0) / 100.0
        gp = ground_plane_score(c, phone_pitch, user_bearing)

        perc_score = 0.0
        if perception is not None:
            perc_score = perception_match_score(perception, c)

        raw = (
            _cfg.w_footprint * fp
            + _cfg.w_clip_image * clip_img
            + _cfg.w_clip_perception * perc_score
            + _cfg.w_ground_plane * gp
        )

        c["raw_score"] = round(raw, 4)
        c["score_breakdown"] = {
            "footprint": round(_cfg.w_footprint * fp, 4),
            "clip_image": round(_cfg.w_clip_image * clip_img, 4),
            "clip_perception": round(_cfg.w_clip_perception * perc_score, 4),
            "ground_plane": round(_cfg.w_ground_plane * gp, 4),
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
) -> tuple[List[Dict[str, Any]], bool]:
    """
    Sort by confidence descending. Decide whether to show the picker.
    Returns (sorted_candidates, show_picker).
    """
    candidates = sorted(candidates, key=lambda x: x.get("confidence", 0), reverse=True)

    if not candidates:
        return candidates, True

    top = candidates[0]["confidence"]

    if len(candidates) == 1:
        return candidates, top < _cfg.picker_abs_threshold

    margin = top - candidates[1]["confidence"]
    show_picker = (
        margin < _cfg.picker_margin_threshold
        or top < _cfg.picker_abs_threshold
    )

    return candidates, show_picker

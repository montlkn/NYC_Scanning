"""
Pipeline configuration — single source of truth for all weights and thresholds.
All values are overridable via environment variables (via pydantic Settings).
No magic numbers live in scoring/retrieval code.
"""

from dataclasses import dataclass, field
from typing import List
import os


@dataclass
class PipelineConfig:
    # ─── Retrieval ─────────────────────────────────────────────────────────────
    max_distance_m: float = float(os.environ.get("PIPELINE_MAX_DIST", 150))
    base_cone_deg: float = float(os.environ.get("PIPELINE_CONE_DEG", 75))
    cone_floor_deg: float = float(os.environ.get("PIPELINE_CONE_FLOOR", 60))
    cone_ceiling_deg: float = float(os.environ.get("PIPELINE_CONE_CEIL", 175))
    max_candidates: int = int(os.environ.get("PIPELINE_MAX_CANDS", 20))

    # Adaptive cone — how much to widen per metre of GPS uncertainty
    cone_gps_scale: float = float(os.environ.get("PIPELINE_CONE_GPS_SCALE", 1.5))
    cone_gps_threshold_m: float = 15.0          # below this: no widening
    cone_heading_scale: float = 1.0              # deg of cone per deg of heading accuracy
    cone_ultrawide_bonus: float = 20.0
    # iPhone GPS in urban canyons can be off by 30–80m while reporting a tiny
    # `gps_accuracy`. We floor the value before the cone calculation so the
    # cone widens enough to recover. Applied globally — slightly wider cones
    # outside dense areas are an acceptable cost.
    gps_accuracy_floor_m: float = float(os.environ.get("PIPELINE_GPS_FLOOR_M", 50))

    # Ring fallback — only fires for genuinely sparse/low-signal cone results.
    # The previous "fire when picker is ambiguous" trigger lived in match.py and
    # was responsible for ~3 live Street View fetches per scan (the picker spinner).
    ring_fallback_clip_threshold: float = float(os.environ.get("PIPELINE_RING_THRESH", 0.30))
    ring_fallback_min_candidates: int = 2        # also trigger if fewer than N returned

    # ─── Scoring weights ───────────────────────────────────────────────────────
    # Two-signal blend. After P3 widened the CLIP pool to the full cone, we
    # found that CLIP-on-brownstones can rank a cross-block building higher
    # than the actual target. Footprint geometry (distance + alignment) is the
    # right tiebreaker for those cases — buildings on the wrong block should
    # not win on CLIP alone. So footprint gets nontrivial weight.
    w_footprint: float = float(os.environ.get("PIPELINE_W_FOOTPRINT", 0.45))
    w_clip_image: float = float(os.environ.get("PIPELINE_W_CLIP_IMAGE", 0.55))

    # ─── Calibration ──────────────────────────────────────────────────────────
    softmax_temperature: float = float(os.environ.get("PIPELINE_TEMP", 0.25))

    # ─── Picker trigger ───────────────────────────────────────────────────────
    # Show picker when top1 - top2 margin (after softmax) is below this
    picker_margin_threshold: float = float(os.environ.get("PIPELINE_PICKER_MARGIN", 0.20))
    # Also show picker if top-1 absolute calibrated confidence is below this
    picker_abs_threshold: float = float(os.environ.get("PIPELINE_PICKER_ABS", 0.55))

    # ─── CLIP-primary retrieval (P3) ──────────────────────────────────────────
    # Fast path: skip CLIP-ranking the rest of the cone if the top-3 already
    # has a clear winner. "Clear" = top-1 CLIP >= threshold AND gap to #2 >= margin.
    fast_path_clip_threshold: float = float(os.environ.get("PIPELINE_FASTPATH_CLIP", 0.75))
    fast_path_clip_margin: float = float(os.environ.get("PIPELINE_FASTPATH_MARGIN", 0.15))
    # When fast path doesn't fire, expand CLIP comparison to this many candidates.
    # Most will hit cached embeddings (F1) so the cost is minimal.
    full_cone_clip_pool: int = int(os.environ.get("PIPELINE_FULL_CONE_POOL", 10))
    # Bail to map picker when top-1 calibrated confidence is below this even
    # after the full cone has been CLIP-ranked. Tuned to "no answer is better
    # than a confidently wrong one."
    no_confident_match_threshold: float = float(os.environ.get("PIPELINE_BAIL_CONF", 0.50))

    # ─── P4: Grok Vision disambig ─────────────────────────────────────────────
    # Kill-switch + tunable confidence bump. Grok is great on photos with
    # readable identifying marks (numbers, flags, plaques) but fails on
    # generic facades — it confidently picks a wrong-but-similar neighbour.
    # The textual-evidence gate downgrades picks whose reason is just
    # "facade similarity / materials / period" to UNSURE so we bail honestly.
    grok_disambig_enabled: bool = os.environ.get("PIPELINE_GROK_DISAMBIG", "true").lower() == "true"
    grok_confidence_bump: float = float(os.environ.get("PIPELINE_GROK_BUMP", 0.65))
    grok_require_textual_evidence: bool = os.environ.get("PIPELINE_GROK_REQUIRE_TEXT", "true").lower() == "true"

    # ─── Thumbnails ───────────────────────────────────────────────────────────
    r2_aerial_template: str = (
        "https://pub-234fc67c039149b2b46b864a1357763d.r2.dev/{bin}/0deg_40pitch.jpg"
    )
    street_view_size: str = "400x400"
    street_view_pitch: int = 10
    street_view_fov: int = 60

    # ─── Telemetry ────────────────────────────────────────────────────────────
    telemetry_enabled: bool = os.environ.get("PIPELINE_TELEMETRY", "true").lower() == "true"


_config: PipelineConfig | None = None


def get_pipeline_config() -> PipelineConfig:
    global _config
    if _config is None:
        _config = PipelineConfig()
    return _config

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

    # Ring fallback — now also triggered when picker margin is thin (see match.py).
    # Threshold lowered: wrong NYC brownstones CLIP at 0.60-0.75 each, so 0.55 never fired.
    # 0.35 catches genuinely low-confidence cone results; picker_ambiguous catches the rest.
    ring_fallback_clip_threshold: float = float(os.environ.get("PIPELINE_RING_THRESH", 0.35))
    ring_fallback_min_candidates: int = 2        # also trigger if fewer than N returned

    # ─── Scoring weights ───────────────────────────────────────────────────────
    w_footprint: float = float(os.environ.get("PIPELINE_W_FOOTPRINT", 0.25))
    w_clip_image: float = float(os.environ.get("PIPELINE_W_CLIP_IMAGE", 0.35))
    w_clip_perception: float = float(os.environ.get("PIPELINE_W_CLIP_PERC", 0.25))
    w_ground_plane: float = float(os.environ.get("PIPELINE_W_GROUND", 0.10))
    w_occlusion: float = float(os.environ.get("PIPELINE_W_OCCL", 0.05))  # subtracted

    # ─── Calibration ──────────────────────────────────────────────────────────
    softmax_temperature: float = float(os.environ.get("PIPELINE_TEMP", 0.08))

    # ─── Picker trigger ───────────────────────────────────────────────────────
    # Show picker when top1 - top2 margin (after softmax) is below this
    picker_margin_threshold: float = float(os.environ.get("PIPELINE_PICKER_MARGIN", 0.20))
    # Also show picker if top-1 absolute calibrated confidence is below this
    picker_abs_threshold: float = float(os.environ.get("PIPELINE_PICKER_ABS", 0.55))

    # ─── Perception (CLIP zero-shot) ──────────────────────────────────────────
    perception_top_k: int = int(os.environ.get("PIPELINE_PERC_K", 3))
    perception_vocab_path: str = os.environ.get(
        "PIPELINE_VOCAB_PATH",
        "/root/backend/data/perception_vocab.yaml"
    )

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

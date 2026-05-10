"""
CLIP-as-perception: zero-shot attribute extraction from the user's scan photo.

Scores the photo against vocabulary buckets (material, feature, era, form, context)
loaded from data/perception_vocab.yaml — a config file, not code.

Output is used in two places:
  1. pipeline/scoring.py — clip_perception_match term in the blend
  2. routers/scan_v2.py — returned to client as evidence chips and Grok context
"""

import yaml
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from pipeline.embedding import encode_image, encode_texts, cosine
from pipeline.config import get_pipeline_config

logger = logging.getLogger(__name__)
_cfg = get_pipeline_config()

# ─── Vocab cache ──────────────────────────────────────────────────────────────

_vocab: Optional[Dict[str, List[str]]] = None
_vocab_embeddings: Optional[Dict[str, np.ndarray]] = None  # bucket → (N, D) array


def _load_vocab() -> Dict[str, List[str]]:
    global _vocab
    if _vocab is None:
        try:
            with open(_cfg.perception_vocab_path) as f:
                _vocab = yaml.safe_load(f)
            logger.info(f"Loaded perception vocab from {_cfg.perception_vocab_path}")
        except Exception as e:
            logger.error(f"Failed to load perception vocab: {e}")
            _vocab = {}
    return _vocab


async def _get_vocab_embeddings() -> Dict[str, Tuple[List[str], np.ndarray]]:
    """Lazily encode all vocab strings once per process lifetime."""
    global _vocab_embeddings
    vocab = _load_vocab()
    if _vocab_embeddings is None:
        _vocab_embeddings = {}
        for bucket, labels in vocab.items():
            if not labels:
                continue
            try:
                embs = await encode_texts(labels)
                _vocab_embeddings[bucket] = (labels, embs)
                logger.info(f"Encoded vocab bucket '{bucket}' ({len(labels)} labels)")
            except Exception as e:
                logger.error(f"Failed to encode vocab bucket '{bucket}': {e}")
    return _vocab_embeddings


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class PercAttr:
    label: str
    score: float   # cosine similarity in [0, 1]


@dataclass
class PerceptionAttributes:
    material: List[PercAttr] = field(default_factory=list)
    feature: List[PercAttr] = field(default_factory=list)
    era: List[PercAttr] = field(default_factory=list)
    form: List[PercAttr] = field(default_factory=list)
    context: List[PercAttr] = field(default_factory=list)

    def summary_line(self) -> str:
        parts = []
        if self.material:
            parts.extend(a.label for a in self.material[:2])
        if self.feature:
            parts.extend(a.label for a in self.feature[:3])
        if self.era:
            parts.append(self.era[0].label)
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "material": [{"label": a.label, "score": round(a.score, 3)} for a in self.material],
            "feature": [{"label": a.label, "score": round(a.score, 3)} for a in self.feature],
            "era": [{"label": a.label, "score": round(a.score, 3)} for a in self.era],
            "form": [{"label": a.label, "score": round(a.score, 3)} for a in self.form],
            "context": [{"label": a.label, "score": round(a.score, 3)} for a in self.context],
            "summary": self.summary_line(),
        }

    def evidence_for_candidate(self, metadata: dict) -> List[str]:
        """
        Produce 1-3 human-readable evidence strings comparing photo perception
        to a candidate's DB metadata (style, materials, year_built).
        Used for picker evidence chips.
        """
        evidence = []
        style = (metadata.get("style") or "").lower()
        material = (metadata.get("material") or metadata.get("materials") or "").lower()
        year_built = metadata.get("year_built")

        # Material match
        if self.material and material:
            top_mat = self.material[0].label.lower()
            if any(word in material for word in top_mat.split()):
                evidence.append(f"✓ {self.material[0].label.split()[0]}")

        # Style/era match
        if self.era and style:
            top_era = self.era[0].label.lower()
            if any(word in style for word in top_era.split() if len(word) > 4):
                evidence.append(f"✓ {self.era[0].label.split()[0]}")

        # Feature (always include top feature as a soft cue if score is high)
        if self.feature and self.feature[0].score > 0.22:
            evidence.append(f"~ {self.feature[0].label}")

        return evidence[:3]


# ─── Main extraction function ─────────────────────────────────────────────────

async def extract_perception(photo_bytes: bytes) -> PerceptionAttributes:
    """
    Score the user's photo against all vocab buckets.
    Returns top-K attributes per bucket.
    """
    k = _cfg.perception_top_k
    try:
        photo_emb = await encode_image(photo_bytes)
        vocab_embs = await _get_vocab_embeddings()

        result = PerceptionAttributes()
        for bucket, (labels, embs) in vocab_embs.items():
            scores = [cosine(photo_emb, embs[i]) for i in range(len(labels))]
            ranked = sorted(zip(labels, scores), key=lambda x: x[1], reverse=True)[:k]
            attrs = [PercAttr(label=lbl, score=float(sc)) for lbl, sc in ranked]
            setattr(result, bucket, attrs)

        logger.info(
            f"Perception: material={result.material[0].label if result.material else '?'}, "
            f"era={result.era[0].label if result.era else '?'}, "
            f"feature={result.feature[0].label if result.feature else '?'}"
        )
        return result

    except Exception as e:
        logger.error(f"Perception extraction failed: {e}")
        return PerceptionAttributes()


def perception_match_score(
    perception: PerceptionAttributes,
    candidate_metadata: dict
) -> float:
    """
    Soft alignment score [0, 1] between perception and a candidate's DB metadata.

    Checks: material match, era/style match.
    Returns 0 if no metadata is available (neutral, not penalized).
    """
    if not perception.material and not perception.era:
        return 0.0

    scores = []
    style = (candidate_metadata.get("style") or "").lower()
    material = (candidate_metadata.get("material") or candidate_metadata.get("materials") or "").lower()

    # Material alignment: top-1 photo material ↔ candidate's material field
    if perception.material and material:
        top_label = perception.material[0].label.lower()
        word_overlap = any(w in material for w in top_label.split() if len(w) > 3)
        scores.append(0.7 if word_overlap else 0.0)
    elif perception.material:
        # No material metadata — neutral (don't penalise)
        scores.append(0.3)

    # Era/style alignment: top-1 era ↔ candidate's style field
    if perception.era and style:
        top_era = perception.era[0].label.lower()
        word_overlap = any(w in style for w in top_era.split() if len(w) > 4)
        scores.append(0.7 if word_overlap else 0.0)
    elif perception.era:
        scores.append(0.3)

    return float(np.mean(scores)) if scores else 0.0

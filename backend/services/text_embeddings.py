"""
Text embedding service for semantic building/venue search.

Uses fastembed (ONNX, CPU) with BAAI/bge-small-en-v1.5 (384-dim). No torch, no
paid API. The model is LAZY-loaded on first use (see _get_model) so scan-only
traffic never pays the model download / ~150MB memory cost — only the /search
path triggers it.

Query and corpus MUST use the same model so vectors are comparable: the batch
embedder (scripts/embed_buildings.py) imports embed_texts from here, and the
/search router imports embed_query. bge-v1.5 vectors come back L2-normalized,
so cosine distance (pgvector `<=>`) is the right operator.
"""

import logging
from functools import lru_cache
from typing import List

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

# bge-v1.5 retrieval guidance: prefix the QUERY (not the passages) with this
# instruction. Passages/corpus are embedded raw.
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _get_model():
    """Load the embedding model once per process, on first use."""
    from fastembed import TextEmbedding  # lazy import keeps app boot light

    logger.info(f"Loading embedding model {MODEL_NAME} (first use)...")
    model = TextEmbedding(model_name=MODEL_NAME)
    logger.info("✅ Embedding model loaded")
    return model


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of corpus texts → list of 384-dim vectors (raw, no prefix)."""
    if not texts:
        return []
    model = _get_model()
    return [vec.tolist() for vec in model.embed(texts)]


def embed_query(text: str) -> List[float]:
    """Embed a single search query → 384-dim vector (with bge query instruction)."""
    if not text or not text.strip():
        return [0.0] * EMBED_DIM
    model = _get_model()
    prefixed = _QUERY_INSTRUCTION + text.strip()
    return next(iter(model.embed([prefixed]))).tolist()

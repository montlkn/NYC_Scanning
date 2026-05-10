"""
CLIP model lifecycle — thin wrapper around open_clip.
Single load point; both image and text encoding go through here.
"""

import torch
import open_clip
import numpy as np
from PIL import Image
from io import BytesIO
import logging
import asyncio
from typing import List

logger = logging.getLogger(__name__)

_model = None
_preprocess = None
_tokenizer = None


def _device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def _load() -> tuple:
    global _model, _preprocess, _tokenizer
    if _model is None:
        device = _device()
        logger.info(f"Loading CLIP model on {device}…")
        _model, _, _preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="openai"
        )
        _model = _model.to(device).eval()
        _tokenizer = open_clip.get_tokenizer("ViT-B-32")
        logger.info("✅ CLIP model ready")
    return _model, _preprocess, _tokenizer


def _encode_image_sync(image_bytes: bytes) -> np.ndarray:
    model, preprocess, _ = _load()
    device = _device()
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    tensor = preprocess(image).unsqueeze(0).to(device)
    with torch.no_grad():
        emb = model.encode_image(tensor)
    emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.cpu().numpy()[0]


def _encode_texts_sync(texts: List[str]) -> np.ndarray:
    """Encode a batch of text prompts. Returns (N, D) normalised array."""
    model, _, tokenizer = _load()
    device = _device()
    tokens = tokenizer(texts).to(device)
    with torch.no_grad():
        emb = model.encode_text(tokens)
    emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.cpu().numpy()


async def encode_image(image_bytes: bytes) -> np.ndarray:
    return await asyncio.to_thread(_encode_image_sync, image_bytes)


async def encode_texts(texts: List[str]) -> np.ndarray:
    return await asyncio.to_thread(_encode_texts_sync, texts)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))

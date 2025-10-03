"""
CLIP-based image matching service
Uses OpenCLIP for building facade similarity comparison
"""

import open_clip
import torch
from PIL import Image
from io import BytesIO
import httpx
from typing import List, Dict, Optional, Tuple
import logging
import numpy as np

from models.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Global model and preprocessor (loaded once on startup)
_clip_model = None
_clip_preprocess = None
_clip_device = None


def init_clip_model():
    """
    Initialize CLIP model on startup
    This should be called during FastAPI lifespan startup
    """
    global _clip_model, _clip_preprocess, _clip_device

    if _clip_model is not None:
        logger.warning("CLIP model already initialized")
        return

    logger.info(f"Loading CLIP model: {settings.clip_model_name}")

    try:
        # Load model and preprocessor
        model, preprocess_train, preprocess_val = open_clip.create_model_and_transforms(
            settings.clip_model_name,
            pretrained=settings.clip_pretrained
        )

        # Use validation transform for inference
        _clip_preprocess = preprocess_val

        # Set device
        if settings.clip_device == "cuda" and torch.cuda.is_available():
            _clip_device = torch.device("cuda")
            model = model.cuda()
            logger.info("Using CUDA for CLIP inference")
        else:
            _clip_device = torch.device("cpu")
            logger.info("Using CPU for CLIP inference")

        model.eval()  # Set to evaluation mode
        _clip_model = model

        logger.info("✅ CLIP model loaded successfully")

    except Exception as e:
        logger.error(f"Failed to load CLIP model: {e}")
        raise


def get_clip_model() -> Tuple:
    """
    Get the initialized CLIP model and preprocessor
    Lazy-loads the model if not already initialized

    Returns:
        Tuple of (model, preprocess, device)
    """
    global _clip_model

    if _clip_model is None:
        logger.info("Lazy-loading CLIP model on first request...")
        init_clip_model()

    return _clip_model, _clip_preprocess, _clip_device


async def download_image(url: str) -> Optional[Image.Image]:
    """
    Download image from URL and convert to PIL Image

    Args:
        url: Image URL

    Returns:
        PIL Image or None if failed
    """
    try:
        logger.debug(f"Downloading image from: {url}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()

            img = Image.open(BytesIO(response.content)).convert('RGB')
            logger.debug(f"Successfully downloaded image from {url}")
            return img

    except httpx.TimeoutException as e:
        logger.error(f"Timeout downloading image from {url}: {e}")
        return None
    except httpx.HTTPError as e:
        logger.error(f"HTTP error downloading image from {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to download image from {url}: {type(e).__name__}: {e}", exc_info=True)
        return None


def encode_image(image: Image.Image) -> Optional[torch.Tensor]:
    """
    Encode image to CLIP embedding

    Args:
        image: PIL Image

    Returns:
        Normalized embedding tensor or None if failed
    """
    try:
        model, preprocess, device = get_clip_model()

        # Preprocess image
        image_tensor = preprocess(image).unsqueeze(0).to(device)

        # Generate embedding
        with torch.no_grad():
            embedding = model.encode_image(image_tensor)
            # Normalize
            embedding = embedding / embedding.norm(dim=-1, keepdim=True)

        return embedding

    except Exception as e:
        logger.error(f"Failed to encode image: {e}")
        return None


async def compare_images(
    user_photo_url: str,
    candidates: List[Dict],
    reference_images: Dict[str, str]
) -> List[Dict]:
    """
    Compare user photo to reference images using CLIP

    Args:
        user_photo_url: URL of user's photo
        candidates: List of candidate building dicts
        reference_images: Dict mapping BBL to reference image URL

    Returns:
        List of matches sorted by confidence (highest first)
    """
    logger.info(f"Comparing user photo against {len(reference_images)} reference images")

    # Download and encode user photo
    user_img = await download_image(user_photo_url)
    if user_img is None:
        logger.error("Failed to download user photo")
        return []

    user_embedding = encode_image(user_img)
    if user_embedding is None:
        logger.error("Failed to encode user photo")
        return []

    # Compare to each reference image
    matches = []

    for candidate in candidates:
        bbl = candidate['bbl']

        if bbl not in reference_images:
            logger.debug(f"No reference image for BBL {bbl}")
            continue

        # Download and encode reference image
        ref_img = await download_image(reference_images[bbl])
        if ref_img is None:
            logger.warning(f"Failed to download reference image for BBL {bbl}")
            continue

        ref_embedding = encode_image(ref_img)
        if ref_embedding is None:
            logger.warning(f"Failed to encode reference image for BBL {bbl}")
            continue

        # Calculate cosine similarity
        similarity = (user_embedding @ ref_embedding.T).item()

        # Apply boosters based on building characteristics
        boosted_score = similarity

        # Landmark boost
        if candidate.get('is_landmark'):
            boosted_score *= settings.landmark_boost_factor

        # Proximity boost
        distance = candidate.get('distance_meters', 100)
        if distance < settings.proximity_boost_threshold:
            boosted_score *= settings.proximity_boost_factor

        # Bearing alignment boost (more aligned = better)
        bearing_diff = candidate.get('bearing_difference', 90)
        if bearing_diff < 15:
            boosted_score *= 1.05

        # Normalize to 0-1 confidence range
        # CLIP similarity is typically between -1 and 1, but usually 0.2 to 0.4 for similar images
        # We'll map this to 0-1 confidence
        confidence = min(max((boosted_score + 1) / 2, 0), 1)

        matches.append({
            'bbl': bbl,
            'address': candidate['address'],
            'latitude': candidate['latitude'],
            'longitude': candidate['longitude'],
            'distance_meters': candidate.get('distance_meters'),
            'confidence': round(confidence, 4),
            'raw_similarity': round(similarity, 4),
            'boosted_score': round(boosted_score, 4),
            'thumbnail_url': reference_images[bbl],
            'is_landmark': candidate.get('is_landmark', False),
            'landmark_name': candidate.get('landmark_name'),
            'architect': candidate.get('architect'),
            'architectural_style': candidate.get('architectural_style'),
            'year_built': candidate.get('year_built'),
        })

    # Sort by confidence (highest first)
    matches.sort(key=lambda x: x['confidence'], reverse=True)

    logger.info(f"Generated {len(matches)} matches")
    if matches:
        logger.info(f"Top match: {matches[0]['address']} ({matches[0]['confidence']:.2%} confidence)")

    return matches


async def batch_encode_images(image_urls: List[str]) -> Dict[str, Optional[torch.Tensor]]:
    """
    Encode multiple images in batch for efficiency

    Args:
        image_urls: List of image URLs

    Returns:
        Dict mapping URL to embedding tensor
    """
    embeddings = {}

    for url in image_urls:
        img = await download_image(url)
        if img:
            embedding = encode_image(img)
            embeddings[url] = embedding
        else:
            embeddings[url] = None

    return embeddings


def calculate_similarity_matrix(
    embeddings: List[torch.Tensor]
) -> np.ndarray:
    """
    Calculate pairwise similarity matrix for a list of embeddings

    Args:
        embeddings: List of embedding tensors

    Returns:
        NxN similarity matrix
    """
    embeddings_tensor = torch.stack(embeddings)
    similarity_matrix = embeddings_tensor @ embeddings_tensor.T
    return similarity_matrix.cpu().numpy()


async def find_best_match(
    user_photo_url: str,
    reference_urls: Dict[str, str]
) -> Optional[Tuple[str, float]]:
    """
    Find best matching building from reference images

    Args:
        user_photo_url: User's photo URL
        reference_urls: Dict mapping BBL to reference image URL

    Returns:
        Tuple of (best_bbl, confidence) or None if no match
    """
    # Download and encode user photo
    user_img = await download_image(user_photo_url)
    if not user_img:
        return None

    user_embedding = encode_image(user_img)
    if user_embedding is None:
        return None

    best_bbl = None
    best_score = -1.0

    for bbl, ref_url in reference_urls.items():
        ref_img = await download_image(ref_url)
        if not ref_img:
            continue

        ref_embedding = encode_image(ref_img)
        if ref_embedding is None:
            continue

        similarity = (user_embedding @ ref_embedding.T).item()

        if similarity > best_score:
            best_score = similarity
            best_bbl = bbl

    if best_bbl:
        confidence = min(max((best_score + 1) / 2, 0), 1)
        return best_bbl, confidence

    return None

"""
Grok client for the backend. Used by lore_generator for text-only narrative
generation. The vision-pick disambig path was removed 2026-06-04 — the
five rescue layers (pose gate, cone, OCR widening, tap-ray, tenant POI)
handle every case it used to rescue, without a paid VLM round-trip.

Single Bearer token from GROK_API_KEY in the Render env. Same x.ai endpoint
the Swift app uses, so prompts and voice stay consistent across the system.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GROK_API_KEY = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY") or ""
GROK_URL = "https://api.x.ai/v1/chat/completions"

# grok-4-1-fast-non-reasoning is the cheapest tier ($0.20/$0.50 per 1M tokens).
# A typical lore call totals ~2K tokens in / ~250 tokens out, costing
# ~$0.0005 per call. The Swift app uses the same model for lore so voice
# and tone stay consistent across the system.
GROK_TEXT_MODEL = os.environ.get("GROK_TEXT_MODEL", "grok-4-1-fast-non-reasoning")


async def grok_text(
    *,
    system: str,
    user: str,
    max_tokens: int = 250,
    temperature: float = 0.3,
    search_enabled: bool = True,
    timeout_s: float = 30.0,
) -> Optional[str]:
    """Plain text-in/text-out call. Used by lore_generator."""
    if not GROK_API_KEY:
        logger.warning("GROK_API_KEY not configured; grok_text skipped")
        return None

    body = {
        "model": GROK_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "search_enabled": search_enabled,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                GROK_URL,
                headers={
                    "Authorization": f"Bearer {GROK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code != 200:
                logger.warning(f"Grok text {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception as e:
        logger.warning(f"Grok text call failed: {e}")
        return None

"""
Grok client for the backend. Shared by:
  - P4 disambiguation (multimodal: user photo + candidate refs → which one)
  - lore_generator (text only, with web search)

Single Bearer token from GROK_API_KEY in the Modal Secret. Same x.ai endpoint
the Swift app uses, so prompts and voice stay consistent across the system.
"""

import base64
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GROK_API_KEY = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY") or ""
GROK_URL = "https://api.x.ai/v1/chat/completions"

# Models. We default to grok-4-1-fast-non-reasoning for both text and vision
# because it's multimodal AND the cheapest tier ($0.20/$0.50 per 1M tokens).
# A typical disambig + lore call totals ~6.5K tokens in / ~250 tokens out,
# costing ~$0.0014 per call. The Swift app uses the same model for lore so
# voice and tone stay consistent across the system.
GROK_TEXT_MODEL = os.environ.get("GROK_TEXT_MODEL", "grok-4-1-fast-non-reasoning")
GROK_VISION_MODEL = os.environ.get("GROK_VISION_MODEL", "grok-4-1-fast-non-reasoning")


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


async def grok_vision_pick(
    *,
    user_photo_bytes: bytes,
    candidates: list,  # list of {address: str, image_bytes: bytes, building_context: str|None}
    timeout_s: float = 25.0,
) -> Optional[dict]:
    """
    Multimodal disambiguation + lore in one call.

    Hands Grok the user's scan photo + N labeled candidate references and
    asks which is the same building. The response also includes a richly-
    observed narrative grounded in what's actually visible in the user's
    photo (signage, flags, materials, ornament). This replaces a separate
    generic lore call: one Grok request, two outputs.

    Returns:
        {"choice": "A"|"B"|"C"|"unsure", "reason": "...", "lore": "..."}
    or None on hard failure.
    """
    if not GROK_API_KEY:
        logger.warning("GROK_API_KEY not configured; grok_vision_pick skipped")
        return None
    if not candidates:
        return None

    letters = ["A", "B", "C", "D"][: len(candidates)]

    intro = (
        "I took a photo of a New York City building (USER PHOTO below). "
        "Below that are CANDIDATE buildings near my GPS. Each candidate has a "
        "reference photo, an address, and a context block containing:\n"
        "  • a fact sheet (year, style, architect, materials, use, landmark status),\n"
        "  • Supabase storytelling if we already have curated text for this building,\n"
        "  • Landmarks Preservation Commission (LPC) notes if available,\n"
        "  • exact coordinates.\n\n"
        "Task 1 — IDENTIFY: which candidate is the building I photographed?\n"
        "  Look for distinguishing details visible in BOTH images: address "
        "numbers above doors, signs, flags, door colors, window patterns, "
        "cornice or pediment details, fire-escape positions, mullion divisions. "
        "IGNORE the surrounding streetscape (trees, cars, sidewalk, sky) — those "
        "vary between photos.\n"
        "  If you cannot tell with high confidence, return choice='unsure'.\n\n"
        "Task 2 — DESCRIBE: write a 3-4 sentence lore paragraph about the "
        "chosen building. STRICT GROUNDING RULES:\n"
        "  (a) Every factual claim (year, architect, style, designation, tenant, "
        "history) MUST come from the candidate's context block, the LPC notes, "
        "the Supabase storytelling, or a verifiable web search of the EXACT "
        "address. Do NOT invent names, dates, events, or details.\n"
        "  (b) If Supabase storytelling already exists, treat it as authoritative "
        "and weave a refinement that adds visible-detail grounding from the user's "
        "photo. Do not contradict it.\n"
        "  (c) Anchor 1-2 sentences in specific features VISIBLE in the user's "
        "photo (materials, ornament, signage, tenant clues like flags or plaques).\n"
        "  (d) Plain prose. No markdown, no bullets. No clichés ('rose amid', "
        "'quiet sentinel', 'bustling streets', 'whisper of jazz', 'sentinel on a "
        "street').\n"
        "  (e) If choice='unsure', return lore=''.\n\n"
        "Reply ONLY with strict JSON: "
        '{"choice":"A"|"B"|"C"|"D"|"unsure","reason":"<one sentence>","lore":"<3-4 sentences or empty>"}\n\n'
        "USER PHOTO:"
    )

    content_parts: list[dict] = [
        {"type": "text", "text": intro},
        _image_part(user_photo_bytes),
    ]
    for letter, cand in zip(letters, candidates):
        addr = cand.get("address") or "(unknown address)"
        ctx = cand.get("building_context") or ""
        header = f"\nCANDIDATE {letter}: {addr}"
        if ctx:
            header += f"\n  Facts: {ctx}"
        content_parts.append({"type": "text", "text": header})
        content_parts.append(_image_part(cand["image_bytes"]))

    body = {
        "model": GROK_VISION_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise visual matcher for New York City buildings and "
                    "a knowledgeable architectural writer. Reply with strict JSON only — "
                    "no markdown, no preface, no trailing text. Lore must be grounded "
                    "in visible details and verifiable facts from web search, not "
                    "generic filler."
                ),
            },
            {"role": "user", "content": content_parts},
        ],
        "max_tokens": 500,
        "temperature": 0.3,
        "stream": False,
        # Let Grok consult the web for landmark designation, tenants, history.
        # Tool fee is ~$0.005/1k calls — only fires on the 20% of scans that
        # actually need disambig, so cost impact is negligible.
        "search_enabled": True,
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
                logger.warning(f"Grok vision {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not text:
                return None
            text = text.strip()
            if text.startswith("```"):
                text = text.strip("`").lstrip("json").strip()
            try:
                parsed = json.loads(text)
            except Exception:
                logger.warning(f"Grok vision returned non-JSON: {text[:200]}")
                return None
            choice = parsed.get("choice", "").upper()
            if choice not in ("A", "B", "C", "D", "UNSURE"):
                return None
            return {
                "choice": choice,
                "reason": (parsed.get("reason") or "")[:240],
                "lore": (parsed.get("lore") or "").strip(),
            }
    except Exception as e:
        logger.warning(f"Grok vision call failed: {e}")
        return None


# ─── helpers ──────────────────────────────────────────────────────────────────

def _image_part(image_bytes: bytes) -> dict:
    """Format image bytes as a data URL for the Grok multimodal content array."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
    }

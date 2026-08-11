"""
OpenAI text client for lore synthesis.

Why synthesis moved here
────────────────────────
Synthesis is the ideal cheap-model job: the facts are already supplied as
grounded source text (an LPC designation report or a Wikipedia extract), so the
model is rewriting, not researching. gpt-5.6-luna is $0.20/$1.20 per 1M tokens,
and a lore call is ~2K in / ~300 out — a fraction of a cent.

It also closes a real cost leak. `services.grok.grok_text` defaults to
`search_enabled=True`, so every tier-1 synthesis over already-free LPC text was
asking a model to run live web search anyway. The entire point of tier 1 is
that it costs nothing beyond tokens. This client sends no tools at all, so the
cheap tier is actually cheap.

Requires OPENAI_API_KEY in the environment. Callers must handle None and fall
back — the key is not yet set in every deployment.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL = "https://api.openai.com/v1/responses"

# gpt-5.6-luna: $0.20 in / $1.20 out per 1M tokens.
OPENAI_TEXT_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-5.6-luna")


def is_configured() -> bool:
    return bool(OPENAI_API_KEY)


async def openai_text(
    *,
    system: str,
    user: str,
    max_tokens: int = 1200,
    timeout_s: float = 30.0,
) -> Optional[str]:
    """Text in, text out. No tools, so no search is ever billed.

    `temperature` is deliberately absent: GPT-5.x reasoning models reject it.

    `max_tokens` maps to `max_output_tokens`, which on a reasoning model covers
    REASONING TOKENS TOO — they are spent first. At 300 the reasoning pass ate
    the budget and the prose was cut mid-sentence ("At 1,454 feet tall including
    its"), which then cached. The default is sized so a ~300-token answer still
    fits after reasoning; at $1.20/1M output the headroom costs a fraction of a
    cent and buys a complete sentence.
    """
    if not OPENAI_API_KEY:
        return None

    body = {
        "model": OPENAI_TEXT_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_output_tokens": max_tokens,
        # Stable prefix key so the large, static system prompt is billed at the
        # cached-input rate across calls.
        "prompt_cache_key": "jink-lore-synth",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if resp.status_code != 200:
            logger.warning(
                f"openai_text {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"openai_text failed: {e}")
        return None

    # Responses API: output[] carries message items whose content[] holds text.
    out = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            t = c.get("text")
            if t:
                out.append(t)
    text = "".join(out).strip()
    return text or None

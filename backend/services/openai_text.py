"""
OpenAI text client. The backend's ONLY LLM client as of 2026-08-11.

Why everything moved here
─────────────────────────
Synthesis is the ideal cheap-model job: the facts are already supplied as
grounded source text (an LPC designation report, a Wikipedia extract, Brave
results), so the model is rewriting, not researching. gpt-5.6-luna is
$0.20/$1.20 per 1M tokens, and a lore call is ~2K in / ~300 out — a fraction of
a cent.

It also closes a real cost leak. The Grok client this replaced defaulted to
`search_enabled=True`, so every tier-1 synthesis over already-free LPC text was
asking a model to run live web search anyway. The entire point of tier 1 is
that it costs nothing beyond tokens. This client sends NO TOOLS AT ALL, which is
the property that makes the cheap tiers actually cheap — and it is deliberate,
not an oversight. Search belongs to `brave_search`, where the query count is a
constant we set (MAX_QUERIES) rather than a number a model chooses. Agentic
search is what reached 13–24 queries and $0.55 on a single building.

If you are tempted to add a `web_search` tool here, you are re-opening that
leak. Add a capped tier to the chain instead.

Requires OPENAI_API_KEY. Callers must handle None — but note `assert_configured`
below: a missing key is now logged loudly at startup rather than degrading in
silence, because three separate env-gated features in this chain each failed
closed without a word in the logs, and that cost a full debugging session.
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


def assert_configured() -> None:
    """Say plainly, at startup, whether the LLM is wired up.

    Every failure mode in this chain is a silent one: no key means
    `is_configured()` returns False and callers skip their work without
    erroring. Requests still return 200 with a null field, which reads as "this
    building has no lore" rather than "the service is misconfigured". Called
    from main.py so the answer is in the deploy log, not inferred from latency.
    """
    if OPENAI_API_KEY:
        logger.info(f"[LLM] openai configured, model={OPENAI_TEXT_MODEL}")
    else:
        logger.error(
            "[LLM] OPENAI_API_KEY is NOT set — all lore synthesis will be "
            "skipped and /api/lore will return null. Set it in the Railway "
            "service variables."
        )


async def openai_text(
    *,
    system: str,
    user: str,
    max_tokens: int = 1200,
    timeout_s: float = 30.0,
    cache_key: str = "jink-lore-synth",
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
        # No reasoning. This is rewriting supplied source text, not solving
        # anything — there is nothing to reason about. Reasoning tokens are
        # billed at the output rate AND drawn from max_output_tokens before the
        # prose is, which is what truncated lore mid-sentence. Turning it off
        # both cuts cost and removes the truncation pressure.
        "reasoning": {"effort": "none"},
        # Stable prefix key so the large, static system prompt is billed at the
        # cached-input rate across calls. Per-caller, because the cache keys on
        # a shared PREFIX: pooling unrelated system prompts under one key means
        # none of them match and the discount silently never applies.
        "prompt_cache_key": cache_key,
    }

    async def _post(payload: dict) -> Optional[httpx.Response]:
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                return await client.post(
                    OPENAI_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as e:
            logger.warning(f"openai_text failed: {e}")
            return None

    resp = await _post(body)
    if resp is None:
        return None

    # Accepted reasoning-effort values differ across model generations, and a
    # rejected one 400s the whole call. Rather than pin a value we cannot verify
    # from here, retry once without the field — losing the saving, never the
    # lore. The log line says which happened.
    if resp.status_code == 400 and "reasoning" in resp.text.lower():
        logger.warning(
            f"openai_text: model rejected reasoning effort, retrying without "
            f"({resp.text[:160]})"
        )
        body.pop("reasoning", None)
        resp = await _post(body)
        if resp is None:
            return None

    if resp.status_code != 200:
        logger.warning(f"openai_text {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        data = resp.json()
    except Exception as e:
        logger.warning(f"openai_text bad json: {e}")
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

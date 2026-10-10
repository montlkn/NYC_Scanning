"""
A strict second pass over what Kit writes.

The cheap checks in kit_voice catch new numbers, new names and copyedits. They
cannot catch a changed meaning: "after the Titanic sank" becoming "while", a
"nationalization" becoming a "forced merger", or an aside that quietly invents
a motive ("a forgotten collaborator tucked in the basement"). Only a reader
can. This is that reader: a separate model call, told to be hostile, that
compares the candidate to its source and lists every claim the source does not
support.

It fails CLOSED. Anything other than a clean OK, including a failed call,
rejects the candidate and leaves the old story or no hook.
"""
from __future__ import annotations

from typing import Optional

from services.openai_text import openai_text

JUDGE_SYSTEM = """You are a strict fact checker. You get a SOURCE and a CANDIDATE
written from it. List every claim in the CANDIDATE that the SOURCE does not state
or directly imply. Count all of these:
- changed timing or order (before, after, while, then)
- changed cause, motive or agent
- a word that changes the meaning (merger for nationalization, owned for rented)
- invented imagery or detail presented as fact
- an insinuation about how a person or group was treated or regarded
- a number, name or place not in the SOURCE

Do NOT count pure tone, rhythm or a rhetorical flourish that asserts nothing
checkable (for example "People had opinions." or "Hats downstairs, cards
upstairs" when the facts are in the SOURCE). A claim stated differently but
meaning the same thing is fine.

Answer with exactly OK if there is nothing to list. Otherwise answer
UNSUPPORTED: followed by each claim, separated by " | ". No other text."""


def parse_verdict(out: Optional[str]) -> Optional[str]:
    """None if the candidate is faithful, else the reason it was rejected."""
    if out is None:
        return "judge unavailable"
    t = out.strip()
    if t.upper().rstrip(".!") == "OK":
        return None
    if t.upper().startswith("UNSUPPORTED"):
        return "judge: " + t.split(":", 1)[-1].strip()[:220]
    return "judge unclear: " + t[:80]


async def judge(source: str, candidate: str, *, label: str = "story") -> Optional[str]:
    """None if faithful, else why not."""
    out = await openai_text(
        system=JUDGE_SYSTEM,
        user=f"SOURCE:\n{source}\n\nCANDIDATE ({label}):\n{candidate}",
        max_tokens=300,
        timeout_s=45.0,
        cache_key="jink-voice-judge",
    )
    return parse_verdict(out)

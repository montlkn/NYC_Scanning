"""
A strict second pass over what Kit writes.

The cheap checks in kit_voice catch new numbers, new names and copyedits. They
cannot catch a changed meaning: "after the Titanic sank" becoming "while", a
"nationalization" becoming a "forced merger", or an aside that quietly invents
a motive. Only a reader can. This is that reader: a separate model call, told
to be hostile, that compares the candidate to its source.

A hostile small model is also a noisy one. In the first run it listed facts the
CANDIDATE never stated, and rejected "built between 1978 and 1984" as implying
construction throughout. So three guards keep it honest:

  1. Every objection must quote the exact CANDIDATE words it objects to. An
     objection whose quote is not actually in the candidate is discarded.
  2. Objections are graded. Only MAJOR (a changed or invented fact) rejects;
     MINOR (nuance, rewording a reader would take as the same) does not.
  3. A rejection is fed back to the writer for one retry (see the scripts), so
     a story with one bad aside is fixed, not thrown away.

It still fails CLOSED: an unparseable answer or a failed call rejects.
Set JUDGE_MODEL to use a stronger model than the writer.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from services.openai_text import openai_text

JUDGE_MODEL = os.environ.get("JUDGE_MODEL") or None

JUDGE_SYSTEM = """You are a strict fact checker. You get a SOURCE and a CANDIDATE
written from it. Find claims in the CANDIDATE that the SOURCE does not support.

MAJOR (reject):
- a changed or invented fact: timing or order (before/after/while), cause,
  motive, agent, place, who did what
- a word that changes the meaning (merger for nationalization)
- a number, name or place not in the SOURCE
- an insinuation about how a person or group was treated or regarded
- an intensifier that overstates the source (decades, enormous, never) when the
  source gives no basis

MINOR (do not reject): rewording a reasonable reader would take to mean the same
thing; a different order of the same facts; a flourish that asserts nothing
checkable ("People had opinions.", "apparently one mansion was not enough").
"Built between 1978 and 1984" and "went up between 1978 and 1984" are the same.

For EACH issue give:
- "candidate_quote": the exact words from the CANDIDATE (never the SOURCE).
- "kind": "absent" if the SOURCE never says it, or "distorted" if the SOURCE says
  something different.
- "evidence": for "absent", the 1 to 3 distinctive words you searched for in the
  SOURCE and did not find (a name, number or key noun). For "distorted", the
  exact words from the SOURCE that the candidate distorts.
Before you answer "absent", search the whole SOURCE for those words. If they are
there, it is not an issue. If the CANDIDATE only repeats what the SOURCE says, it
has no issues.

Answer with JSON only, nothing else:
{"issues":[{"severity":"major"|"minor","candidate_quote":"...","kind":"absent"|"distorted","evidence":"...","problem":"one short sentence"}]}
Use {"issues":[]} when there is nothing to list."""


def _norm(t: str) -> str:
    t = re.sub(r"[*_]{1,2}", "", t).lower()
    t = t.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"[^a-z0-9$%]+", " ", t).strip()


def _evidence_holds(issue: dict, src: str) -> bool:
    """Check the judge's objection against the source in code, not on trust.

    absent    -> the words it says are missing must really be missing. If most
                 of them are in the source, the claim IS there and the judge
                 was wrong (it said Isaac Liebman was not in a source that
                 names him).
    distorted -> the source words it says are distorted must really be there.
    A flag with no usable evidence is kept: no evidence is not a reason to
    wave a possible invention through."""
    ev = _norm(str(issue.get("evidence", "")))
    kind = str(issue.get("kind", "")).lower()
    if not ev:
        return True
    if kind == "distorted":
        return ev in src
    toks = [t for t in ev.split() if len(t) > 3 or any(c.isdigit() for c in t)]
    if not toks:
        return True
    words = set(src.split())
    present = sum(1 for t in toks if t in words)
    return present / len(toks) < 0.6


def parse_verdict(out: Optional[str], candidate: str, source: str = "") -> Optional[str]:
    """None if the candidate may stand, else the reason (quotes and problems)
    that is also fed back to the writer."""
    if out is None:
        return "judge unavailable"
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return "judge unparseable: " + out.strip()[:80]
    try:
        issues = json.loads(m.group(0)).get("issues", [])
    except (ValueError, AttributeError):
        return "judge unparseable: " + out.strip()[:80]
    cand = _norm(candidate)
    majors = []
    for it in issues if isinstance(issues, list) else []:
        if not isinstance(it, dict) or str(it.get("severity", "")).lower() != "major":
            continue
        quote = _norm(str(it.get("candidate_quote", "")))
        # An objection to words the candidate never wrote is the judge's error.
        if len(quote) < 6 or quote not in cand:
            continue
        if source and not _evidence_holds(it, _norm(source)):
            continue
        majors.append(f'"{str(it["candidate_quote"]).strip()[:120]}": {str(it.get("problem", "")).strip()[:140]}')
    if not majors:
        return None
    return "judge: " + " | ".join(majors[:4])


async def judge(source: str, candidate: str, *, label: str = "story") -> Optional[str]:
    """None if the candidate may stand, else why not."""
    out = await openai_text(
        system=JUDGE_SYSTEM,
        user=f"SOURCE:\n{source}\n\nCANDIDATE ({label}):\n{candidate}",
        max_tokens=500,
        timeout_s=60.0,
        cache_key="jink-voice-judge",
        model=JUDGE_MODEL,
    )
    return parse_verdict(out, candidate, source)

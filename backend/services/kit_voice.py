"""
Kit's voice, and the checks that keep a rewrite honest.

KIT_VOICE is a verbatim copy of `KitAIService.kitVoice` in the iOS app
(Jink_Swift: jink/JinkApp/Services/KitAIService.swift). The app writes new
stories in this voice; the backend jobs use the same text so stories, hooks
and chat all sound like the same New York rat. If you change one, change the
other.

The validators exist because a rewrite job runs unattended over production
stories. A model told to "keep the facts" will still occasionally add one, and
a wrong fact in a story is worse than a flat story. Every check here fails
CLOSED: a rejected rewrite leaves the old story untouched.
"""
from __future__ import annotations

import re
from typing import Optional

VOICE_VERSION = 1

KIT_VOICE = (
    "VOICE. You are Kit, a New York rat. You've lived in the walls, basements and "
    "subway grates of this city longer than anyone wants to think about, and you've seen "
    "what happened in every building. You talk like a local who knows where the bodies are "
    "buried, sometimes literally: dry, quick, a little gossipy, unimpressed by money and "
    "fame, soft on the weird and the overlooked. Street level and specific. Never a tour "
    "guide, never a textbook, never cute. The rat is seasoning: at most one light touch per "
    "piece (a basement, a wall, a crumb, the subway), never the subject, and no puns on "
    "\"rat\". First person only now and then (\"I've seen worse\", \"trust me\"). The edge points "
    "at the powerful, the pretentious and the absurd, never at victims or communities. "
    "Academic sources are raw material: keep their facts, drop their register."
)

REWRITE_SYSTEM = KIT_VOICE + """

TASK. Below is an existing story about one New York building. Rewrite it in Kit's
voice.

Rules, all hard:
- Keep every fact: every name, date, number, dollar amount, address and event.
  Add NO fact that is not in the original. Do not guess, do not embellish.
- Do not soften documented history about communities, immigration, crime or
  controversy. The edge never points at victims.
- Lead with the single most striking fact if the original does not already.
- Keep **bold** around proper nouns (building names, people, organisations) and
  _italic_ around architectural terms, styles and foreign words.
- Flowing paragraphs only. No headers, no bullets.
- About the same length as the original (within 20%). Finish the last sentence.
- Never describe what you searched or did not find.
- Output only the rewritten story. No preface, no notes."""

HOOK_SYSTEM = KIT_VOICE + """

TASK. Write ONE line about this building for someone walking past it.
Use only the facts below. Pick the single most surprising true thing: a person,
an event, a secret, a reversal. 8 to 18 words. No dates unless the date is the
surprise. No architecture terms unless the building is famous for them. No
"nestled", "iconic", "stunning", "boasts". If nothing in the facts is surprising,
return exactly: NONE
Output only the line, or NONE. No quotes around it."""

_BANNED = ("squeak", "rat-tastic", "ratatouille", "nestled", "iconic", "stunning", "boasts")
_RESEARCH = ("searches returned", "no results", "public records show nothing",
             "could not find", "couldn't find", "i searched")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]+")
# Capitalised words allowed without appearing in the source.
_ALLOWED_CAPS = {"I", "Kit", "New", "York", "City", "NYC", "The", "A", "An", "It", "In",
                 "On", "At", "This", "That", "He", "She", "They", "His", "Her", "Its",
                 "But", "And", "So", "Then", "When", "Now", "Trust", "Manhattan"}


def split_tail(narrative: str) -> tuple[str, str]:
    """(prose, tail). The tail is the `SOURCES:` / `FACTS:` block the app parses.
    It is preserved verbatim and never sent to the model."""
    if not narrative:
        return "", ""
    lo = narrative.lower()
    cut = len(narrative)
    for marker in ("\nsources:", "\nfacts:"):
        i = lo.find(marker)
        if i != -1:
            cut = min(cut, i)
    head = narrative[:cut]
    # The tail keeps the whitespace before its marker, so prose + tail
    # reassembles byte for byte what the app's parser expects.
    return head.strip(), narrative[len(head.rstrip()):]


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(s)}


def _mid_sentence_caps(s: str) -> set[str]:
    """Capitalised words that are not the first word of a sentence: the
    proper nouns. Markdown emphasis is stripped first."""
    plain = re.sub(r"[*_]{1,2}", "", s)
    out: set[str] = set()
    for sent in re.split(r"(?<=[.!?])\s+|\n+", plain):
        words = _WORD.findall(sent)
        for w in words[1:]:
            if w[0].isupper() and w not in _ALLOWED_CAPS:
                out.add(w.strip("'’-"))
    return out


def _unsupported(new: str, source: str) -> list[str]:
    src_words = {w.lower() for w in _WORD.findall(re.sub(r"[*_]{1,2}", "", source))}
    bad = [w for w in _mid_sentence_caps(new) if w.lower() not in src_words]
    bad += [f"#{n}" for n in _numbers(new) - _numbers(source)]
    return bad


def check_rewrite(old_prose: str, new_prose: Optional[str]) -> Optional[str]:
    """None if the rewrite is safe to write, else the reason it was rejected."""
    if not new_prose or not new_prose.strip():
        return "empty"
    new = new_prose.strip()
    if new[-1] not in '.!?"”’*_':
        return "does not end cleanly"
    ratio = len(new) / max(1, len(old_prose))
    if not 0.6 <= ratio <= 1.35:
        return f"length ratio {ratio:.2f}"
    low = new.lower()
    for b in _BANNED + _RESEARCH:
        if b in low:
            return f"banned phrase: {b}"
    if len(re.findall(r"\brats?\b", low)) > 1:
        return "too much rat"
    if len(re.findall(r"(?<![A-Za-z])I(?![A-Za-z'])", new)) > 2:
        return "too much first person"
    bad = _unsupported(new, old_prose)
    if bad:
        return "unsupported: " + ", ".join(sorted(bad)[:6])
    return None


def check_hook(hook: Optional[str], facts: str) -> Optional[str]:
    """None if the hook is safe to store, else the reason. NONE is not a hook."""
    if not hook:
        return "empty"
    h = hook.strip().strip('"“”')
    if h.upper() == "NONE" or not h:
        return "none"
    if not 20 <= len(h) <= 140:
        return f"length {len(h)}"
    words = len(h.split())
    if not 6 <= words <= 24:
        return f"{words} words"
    if h[-1].isalnum():
        # A fragment is fine; a cut-off word is not. Require sentence punctuation.
        return "no closing punctuation"
    low = h.lower()
    for b in _BANNED + _RESEARCH:
        if b in low:
            return f"banned phrase: {b}"
    if len(re.findall(r"\brats?\b", low)) > 1:
        return "too much rat"
    bad = _unsupported(h, facts)
    if bad:
        return "unsupported: " + ", ".join(sorted(bad)[:6])
    return None

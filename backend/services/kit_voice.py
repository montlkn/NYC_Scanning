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

import difflib
import re
from typing import Optional

VOICE_VERSION = 1
# Inline citation as the app writes it: [[1]](https://...)
_CITE = re.compile(r"\[\[\d+\]\]\([^)\s]+\)")
# A rewrite more similar than this to the original is a copyedit, not a voice.
SIMILAR_MAX = 0.80

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
voice. The FACTS stay. The SENTENCES do not.

Do this:
- Rebuild every sentence. Change the structure, the order and the rhythm. Cut
  padding and throat-clearing. Mix short sentences with longer ones. If your
  version reads like the original with a few words swapped, you did it wrong.
- Open on the single most striking fact, even if the original buried it.
- Say it the way a local would say it out loud, with dry asides where the facts
  earn them. The aside comments on a fact; it never adds one.

Hard rules:
- Keep every fact: every name, date, number, dollar amount, address and event.
  Add NO fact that is not in the original. Do not guess, do not embellish, do
  not imply a motive or a connection the original does not state.
- Do not soften documented history about communities, immigration, crime or
  controversy. The edge never points at victims.
- Keep **bold** around proper nouns (building names, people, organisations) and
  _italic_ around architectural terms, styles and foreign words.
- Flowing paragraphs only. No headers, no bullets. No em dashes.
- About the same length as the original (within 25%). Finish the last sentence.
- Never describe what you searched or did not find.
- Never mention "the source", "the original", "the record", "the text" or
  anything "supplied" or "provided". Kit knows these things; he does not cite
  paperwork in the prose.
- Keep relationship words exactly: occupied is not owned, leased is not
  bought, bombed is not destroyed, proposed is not built.
- Never attach a date to an event unless the original gives that event that date.
- CITATIONS. Markers like [[1]](https://example.org/page) are citations. Copy
  every one exactly, character for character, and put it right after the
  sentence that carries the fact it backs. Do not drop, merge, renumber or
  invent any.
- Output only the rewritten story. No preface, no notes.

EXAMPLE (an invented building, to show the move, not to copy words from).
BEFORE: **The Alder Building** was completed in 1911 for the **Alder Hat Company**, designed by **Mara Quill**. Its terracotta facade features ornate cornices, and the structure is considered a significant example of early commercial architecture. In 1932, the building's owner, **Harold Pike**, was arrested for running an illegal card game on the fourth floor.
AFTER: **Harold Pike**, owner of **The Alder Building**, got arrested in 1932 for running an illegal card game on the fourth floor. Not a speakeasy. A hat company's building. **Mara Quill** designed it in 1911 for the **Alder Hat Company**: terracotta facade, ornate cornices, a textbook case of early commercial architecture, if that's your thing."""

HOOK_SYSTEM = KIT_VOICE + """

TASK. Write ONE line about this building for someone walking past it.
Use only the facts below. Pick the single most surprising true thing: a person,
an event, a secret, a reversal. 8 to 18 words. No dates unless the date is the
surprise. No architecture terms unless the building is famous for them. No
"nestled", "iconic", "stunning", "boasts". No em dashes.

A hook is a FACT, said plainly. The voice lives in word choice, not in asides.
- No jokes, asides or commentary after the fact ("because apparently...").
- Use the facts' own words. No intensifiers the facts do not give you (enormous,
  decades, never, always, extraordinary).
- Say "here" only if the facts say it happened at THIS building, not at a
  building that stood on the site before.
- Keep timing exactly as stated (after is not while).
- If you cannot say it plainly in the facts' own words, return exactly: NONE

A hook must make a stranger want to open the building. Prefer NONE to a dull line.
GOOD: "United States Steel was founded inside this building, then Dominique
Strauss-Kahn served house arrest here."
GOOD: "Its Bronx plant printed roughly half the securities traded on the New York
Stock Exchange."
DULL (return NONE instead): "The statue was intended to symbolize liberty."
DULL (return NONE instead): "The company produced bank notes, stamps and
certificates here."
Output only the line, or NONE. No quotes around it."""

_BANNED = ("squeak", "rat-tastic", "ratatouille", "nestled", "iconic", "stunning", "boasts")
# Meta words a story must not use unless the original already does.
_META = ("the source", "the original", "the record", "supplied", "provided text",
         "provided context", "the context", "the text says")
_RISKY = ("belonged", "owned by", "destroyed", "demolished", "bought", "purchased")
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


def _strip_possessive(w: str) -> str:
    """York's -> York, Brothers' -> Brothers (straight or curly apostrophe)."""
    for suf in ("'s", "\u2019s"):
        if w.endswith(suf) and len(w) > 3:
            return w[:-2]
    for suf in ("s'", "s\u2019"):
        if w.endswith(suf) and len(w) > 3:
            return w[:-1]
    return w


def _mid_sentence_caps(s: str) -> set[str]:
    """Capitalised words that are not the first word of a sentence: the
    proper nouns. Markdown emphasis is stripped first."""
    plain = re.sub(r"[*_]{1,2}", "", s)
    out: set[str] = set()
    for sent in re.split(r"(?<=[.!?])\s+|\n+", plain):
        words = _WORD.findall(sent)
        for w in words[1:]:
            w = _strip_possessive(w.strip("'\u2019-"))
            if w and w[0].isupper() and w not in _ALLOWED_CAPS:
                out.add(w)
    return out


def _unsupported(new: str, source: str) -> list[str]:
    src_words = {_strip_possessive(w).lower()
                 for w in _WORD.findall(re.sub(r"[*_]{1,2}", "", source))}
    src_words |= {w.lower() for w in _WORD.findall(re.sub(r"[*_]{1,2}", "", source))}
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
    if new.count("\u2014") > old_prose.count("\u2014") + 1:
        return "added em dashes"
    if len(re.findall(r"(?<![A-Za-z])I(?![A-Za-z'])", new)) > 2:
        return "too much first person"
    old_low = old_prose.lower()
    for m in _META:
        if m in low and m not in old_low:
            return f"meta phrase: {m}"
    for v in _RISKY:
        if re.search(rf"\b{v}\b", low) and not re.search(rf"\b{v}\b", old_low):
            return f"changed relationship word: {v}"
    old_cites, new_cites = _CITE.findall(old_prose), _CITE.findall(new)
    if sorted(old_cites) != sorted(new_cites):
        return f"citations changed ({len(old_cites)} before, {len(new_cites)} after)"
    bad = _unsupported(_CITE.sub("", new), _CITE.sub("", old_prose))
    if bad:
        return "unsupported: " + ", ".join(sorted(bad)[:6])
    plain = lambda t: re.sub(r"[*_]{1,2}", "", _CITE.sub("", t)).lower()
    if difflib.SequenceMatcher(None, plain(old_prose), plain(new), autojunk=False).ratio() > SIMILAR_MAX:
        return "too close to original (voice did not change)"
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

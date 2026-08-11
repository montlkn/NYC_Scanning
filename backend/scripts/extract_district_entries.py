#!/usr/bin/env python3
"""
Extract per-building entries from NYC LPC HISTORIC DISTRICT designation reports.

Why this exists
───────────────
`ingest_landmark_reports.py --include-districts` fans each district report out to
every member BIN as a single district-level blurb. Measured on the live corpus:
31,955 of 33,703 BINs (94%) have NO building-specific text — all 2,101 Greenwich
Village buildings share the same two paragraphs. Lore built on that would be
identical across whole neighborhoods.

But the per-building material IS in the PDFs. District reports are organised as
house-number entries under street headers, and they carry exactly the specific,
niche history the app wants:

    #13  "...Boorman was a generous benefactor of the blind, the orphans, and of
          Trinity Church. After his death in 1866, his adopted daughter Mrs.
          Josiah W. Wheeler sold No. 13 to William Butler Duncan..."

Layout, and how we exploit it
─────────────────────────────
Text carries coordinates, and the reports are rigidly columnar:

    x≈146+, y≈965   GV-HD AREA 5                        <- area line
    x≈146+, y≈940   WEST THIRTEENTH STREET South Side   <- street header
    x≈ 81,  y≈917   #230-232                            <- MARGINAL entry marker
    x≈146+, y≈917   Industry, in the form of this ...   <- body, same y as marker

So: markers live in the left margin (small x), body text to their right. A body
line belongs to the nearest marker at or above it. A page whose body starts
before any marker is a continuation of the previous entry ("cont.").

This is deliberately NOT an LLM job. The structure is deterministic, so a parser
gets exact attribution for free, and every extracted claim stays verbatim from a
primary source rather than being paraphrased by a model.

Usage:
  python scripts/extract_district_entries.py --pdf lp0489.pdf --report LP-0489
  python scripts/extract_district_entries.py --pdf lp0489.pdf --limit-pages 60 --sample 10
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    print("pypdf required: pip install pypdf", file=sys.stderr)
    raise

# The marker column sits this far left of the body's left edge, at minimum.
# Both columns drift page to page with scan alignment — one page has markers at
# x=81 with body at x=146, the next has 47 and 106 — so the split is computed
# per page from the body's own left edge rather than pinned to a constant. A
# fixed cutoff silently swallows body text as markers and shreds the prose.
MARGIN_GAP = 25

# Lines within this many units of each other are the same visual line. A marker
# and the body line it labels are typically 1-2 units apart (y=917 vs y=918).
Y_TOL = 3

# "#13", "#28-32", "(#2- 8)", "#230-232". OCR of these scans routinely mangles
# the dash into ~, _ or a stray space, so accept a loose separator.
MARKER_RE = re.compile(r"^\(?#\s*(\d+)\s*(?:[-~_–—]\s*(\d+))?\s*\)?$")

# "WEST THIRTEENTH STREET South Side (Betw. Seventh & Greenwich Aves.)"
# -> street = "WEST THIRTEENTH STREET". Side/cross-street qualifiers are
# navigation aids for a reader, not part of the address.
SIDE_RE = re.compile(
    r"\s+(North|South|East|West)\s+Side\b.*$|\s*\(.*$", re.IGNORECASE
)

# A street header, wherever it appears. These do NOT only sit at the top of a
# page: a new street section frequently opens mid-page, and attributing the rest
# of that page to the previous street silently files one street's history under
# another's addresses. Matched by shape (leading run of capitals ending in a
# thoroughfare word) rather than by position.
STREET_HEADER_RE = re.compile(
    r"^[A-Z][A-Z0-9 .,'&\-]{4,60}?"
    r"\b(STREET|AVENUE|PLACE|SQUARE|ROAD|LANE|MEWS|COURT|TERRACE|PARK|ALLEY|WAY)\b"
    # A direction may follow the thoroughfare word and be part of the NAME
    # ("WASHINGTON SQUARE NORTH"), not a side qualifier. Without this the whole
    # square is skipped: its header matches nothing and its pages go unattributed.
    r"(\s+(NORTH|SOUTH|EAST|WEST))?"
    r"(\s+(North|South|East|West)\s+Side\b|\s*\(|\s*$)"
)

# The area/section line ("GV-HD AREA 5"). Never prose.
AREA_RE = re.compile(r"\bAREA\s+[0-9lI]+\b", re.IGNORECASE)

# Running page number, e.g. "-31-". Lands mid-sentence when a paragraph spans a
# page break, so it has to be dropped rather than joined into the prose.
PAGE_NUM_RE = re.compile(r"^[-~_\s]*\d{1,4}[-~_\s]*$")

# Ordinal words -> digits. NOT a curated vocabulary: it's the closed, finite
# mapping of English ordinals, needed because the reports spell streets out
# ("WEST THIRTEENTH STREET") while the buildings DB stores digits
# ("199 WEST 10 STREET"). Generated, not hand-listed.
_ONES = ["", "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH",
         "EIGHTH", "NINTH", "TENTH", "ELEVENTH", "TWELFTH", "THIRTEENTH",
         "FOURTEENTH", "FIFTEENTH", "SIXTEENTH", "SEVENTEENTH", "EIGHTEENTH",
         "NINETEENTH"]
_TENS = {"TWENTIETH": 20, "THIRTIETH": 30, "FORTIETH": 40, "FIFTIETH": 50,
         "SIXTIETH": 60, "SEVENTIETH": 70, "EIGHTIETH": 80, "NINETIETH": 90}
_TENS_PREFIX = {"TWENTY": 20, "THIRTY": 30, "FORTY": 40, "FIFTY": 50,
                "SIXTY": 60, "SEVENTY": 70, "EIGHTY": 80, "NINETY": 90}


def _ordinal_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for i, w in enumerate(_ONES):
        if w:
            m[w] = str(i)
    for w, v in _TENS.items():
        m[w] = str(v)
    for pre, base in _TENS_PREFIX.items():
        for i, w in enumerate(_ONES[1:10], start=1):
            m[f"{pre}-{w}"] = str(base + i)
            m[f"{pre} {w}"] = str(base + i)
    return m


ORDINALS = _ordinal_map()


def normalize_street(raw: str) -> str:
    """'WEST THIRTEENTH STREET South Side (Betw. ...)' -> 'WEST 13 STREET'."""
    s = SIDE_RE.sub("", raw or "").strip()
    # Drop periods entirely. The reports write "ST. JOHN'S PLACE" while the DB
    # writes "ST JOHN'S PLACE"; keeping the dot makes the two never compare
    # equal and silently loses every building on such a street.
    s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).upper().strip(" .,")
    # Longest-first so "TWENTY-FIRST" wins over "FIRST".
    for word in sorted(ORDINALS, key=len, reverse=True):
        if word in s:
            s = s.replace(word, ORDINALS[word])
            break
    return re.sub(r"\s+", " ", s).strip()


def expand_range(lo: int, hi: int | None) -> list[int]:
    """'#4-10' -> [4,6,8,10]. Street numbering alternates parity by side, so a
    range steps by 2 when both ends share parity; mixed parity means the report
    lumped an odd pair together, so take both ends only."""
    if hi is None or hi == lo:
        return [lo]
    if hi < lo:
        lo, hi = hi, lo
    if (hi - lo) > 60:  # implausible span — almost always an OCR misread
        return [lo]
    if lo % 2 == hi % 2:
        return list(range(lo, hi + 1, 2))
    return [lo, hi]


# ── Modern-report format ──────────────────────────────────────────────────────
# Reports from roughly LP-2000 on abandon the typewritten marginal-marker layout
# for a structured per-building record in a single column:
#
#     70-10 60th Lane
#     Borough of Queens Tax Map Block 3517, Lot 28
#     Date: 1907 (NB 2414-1907)
#     Architect/Builder: Louis Berger & Company
#     Original Owner: Paul Stier
#
# This is strictly better than the old format: the fields are labelled, and the
# Block/Lot line yields a BBL, so buildings match EXACTLY instead of through
# fuzzy address comparison. Detected per PDF rather than guessed from LP number,
# since the changeover was gradual.
BLOCK_LOT_RE = re.compile(
    r"Borough of\s+(Manhattan|Bronx|Brooklyn|Queens|Staten Island)\s+"
    r"Tax Map Block\s+([\d]+)\s*,?\s*Lot\s+([\d]+)",
    re.IGNORECASE,
)

# A third layout (1980s-90s reports, e.g. LP-1647 Upper West Side) writes the
# same information as "Tax Map Block/Lot: 1121/29" — no borough on the line,
# and the scan OCRs "Block/Lot" into things like "BlockjlDt:". So match on the
# stable parts, "Tax Map" followed by block/lot digits, and tolerate whatever
# the OCR made of the words between. The borough comes from the district's own
# BINs instead, whose first digit IS the borough code.
BLOCK_LOT_ALT_RE = re.compile(
    r"Tax\s*Map\b[^\d\n]{0,24}?(\d{2,5})\s*[/|jl]\s*(\d{1,5})",
    re.IGNORECASE,
)

# "Date:", "Architect/Builder:", "Original Owner:", "Significant Architectural
# Features:". Label runs of capitalised words ending in a colon.
FIELD_RE = re.compile(r"^([A-Z][A-Za-z()/&,\- ]{2,45}):\s*(.+)$")

BOROUGH_CODE = {"manhattan": "1", "bronx": "2", "brooklyn": "3",
                "queens": "4", "staten island": "5"}

# Condition-survey fields to DROP: they describe present-day fabric and read as
# an inspection checklist rather than history.
#
# This is a blocklist, not a whitelist, because these scans mangle the labels —
# ARCHITECT becomes "AROITTECT", OWNER/DEVELOPER becomes "OWNER/DEVEIDPER". A
# whitelist of exact names silently discards every garbled label, which starves
# entries below the length threshold and looks like "the report has no data".
# Substring matching on the survey vocabulary is far more OCR-tolerant, and
# wrongly keeping an odd field is much cheaper than dropping the architect.
DROP_FIELD_SUBSTRINGS = (
    "window", "cornice", "stoop", "door", "areaway", "sidewalk", "curb",
    "paving", "roof", "fence", "railing", "gate", "security", "lighting",
    "utility", "meter", "conduit", "awning", "sign", "notable condition",
    "facade note", "planting", "flagpole", "antenna", "cable",
)


def is_lore_field(label: str) -> bool:
    low = label.lower()
    return not any(s in low for s in DROP_FIELD_SUBSTRINGS)


def bbl_from(borough: str, block: str, lot: str) -> str:
    """Borough digit + 5-digit block + 4-digit lot, the standard BBL form."""
    return f"{BOROUGH_CODE[borough.strip().lower()]}{int(block):05d}{int(lot):04d}"


@dataclass
class ModernEntry:
    report: str
    address: str
    bbl: str
    fields: dict[str, str] = field(default_factory=dict)
    page: int = 0

    @property
    def text(self) -> str:
        parts = [f"{k.title()}: {v}" for k, v in self.fields.items()]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


def extract_modern(pdf_path: str, report_id: str,
                   limit_pages: int | None = None,
                   default_borough: str | None = None) -> list[ModernEntry]:
    """`default_borough` is the borough DIGIT ('1'..'5') used by the alternate
    Block/Lot layout, which omits the borough from the line. Callers derive it
    from the district's BINs rather than guessing."""
    reader = PdfReader(pdf_path)
    pages = reader.pages if limit_pages is None else reader.pages[:limit_pages]
    out: list[ModernEntry] = []
    current: ModernEntry | None = None

    for pno, page in enumerate(pages):
        try:
            raw = page.extract_text() or ""
        except Exception:
            continue
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m = BLOCK_LOT_RE.search(line)
            bbl = None
            if m:
                try:
                    bbl = bbl_from(m.group(1), m.group(2), m.group(3))
                except (KeyError, ValueError):
                    bbl = None
            elif default_borough:
                alt = BLOCK_LOT_ALT_RE.search(line)
                if alt:
                    try:
                        bbl = (f"{default_borough}"
                               f"{int(alt.group(1)):05d}{int(alt.group(2)):04d}")
                    except ValueError:
                        bbl = None
            if bbl:
                # The address is the line above the Block/Lot line.
                addr = lines[i - 1].strip() if i > 0 else ""
                current = ModernEntry(report=report_id, address=addr, bbl=bbl, page=pno)
                out.append(current)
                continue
            if current is None:
                continue
            fm = FIELD_RE.match(line)
            if fm:
                label = fm.group(1).strip().lower()
                if is_lore_field(label):
                    current.fields[label] = fm.group(2).strip()
                    current._last = label  # type: ignore[attr-defined]
                else:
                    current._last = None  # type: ignore[attr-defined]
            else:
                # Continuation of the previous field's value (these wrap freely).
                last = getattr(current, "_last", None)
                if last:
                    current.fields[last] += " " + line
    return out


def looks_modern(pdf_path: str, probe_pages: int = 40) -> bool:
    """True if the PDF uses either structured Block/Lot record format.

    Probes from a quarter of the way in, not from page 0: these reports open
    with tables of contents and designation boilerplate, and the per-building
    entries can start hundreds of pages deep (LP-1647's begin around p.280 of
    1,537). Probing only the front returns False on a structured report and
    silently routes it to the legacy parser, which yields nothing.
    """
    reader = PdfReader(pdf_path)
    n = len(reader.pages)
    if n == 0:
        return False
    # Sample EVENLY across the whole document rather than one contiguous window.
    # A window can land entirely in front matter or an index — LP-1647 runs to
    # 1,537 pages with a long table of contents and a back index — and a probe
    # that sees neither returns False, routing a structured report to the legacy
    # parser, which yields nothing at all.
    step = max(n // probe_pages, 1)
    hits = 0
    for i in range(0, n, step):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            continue
        if BLOCK_LOT_RE.search(text) or BLOCK_LOT_ALT_RE.search(text):
            hits += 1
        if hits >= 2:
            return True
    return False


@dataclass
class Entry:
    report: str
    area: str
    street: str
    numbers: list[int]
    pages: list[int] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()

    def key(self) -> tuple[str, tuple[int, ...]]:
        return (self.street, tuple(self.numbers))


def parse_page(page) -> tuple[str, str, list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (area, street, markers[(y,text)], body_lines[(y,text)])."""
    items: list[tuple[int, int, str]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        t = (text or "").strip()
        if t:
            items.append((round(tm[4]), round(tm[5]), t))

    page.extract_text(visitor_text=visitor)
    if not items:
        return "", "", [], []

    # Cluster fragments into visual lines by y, tolerating scan jitter.
    lines: list[tuple[int, list[tuple[int, str]]]] = []
    for x, y, t in sorted(items, key=lambda z: (-z[1], z[0])):
        if lines and abs(lines[-1][0] - y) <= Y_TOL:
            lines[-1][1].append((x, t))
        else:
            lines.append((y, [(x, t)]))

    # The body's left edge is the modal line-start. Markers are the fragments
    # sitting clearly left of it; everything else is prose.
    starts_x = sorted(min(x for x, _ in frags) for _, frags in lines)
    body_left = starts_x[len(starts_x) // 2]
    margin_max = body_left - MARGIN_GAP

    def render(frags: list[tuple[int, str]]) -> str:
        return " ".join(t for _, t in sorted(frags))

    # Identify the area line and the page's opening street header by shape, not
    # by rank — some scans carry an extra stray line above them.
    area = ""
    street = ""
    for _, frags in lines[:4]:
        text = render(frags)
        if not area and AREA_RE.search(text):
            area = text
        elif not street and STREET_HEADER_RE.match(text):
            street = text

    markers: list[tuple[int, str]] = []
    body: list[tuple[int, str]] = []
    for y, frags in lines:
        text = render(frags)
        if text in (area, street):
            continue
        if AREA_RE.search(text) and len(text) < 40:
            continue  # repeated area furniture
        left = [(x, t) for x, t in frags if x <= margin_max]
        right = [(x, t) for x, t in frags if x > margin_max]
        for _, t in left:
            markers.append((y, t))
        if right:
            rendered = render(right)
            # A mid-page street header re-anchors everything below it. Emit it
            # as a marker-like signal so the caller can switch streets.
            if STREET_HEADER_RE.match(rendered):
                body.append((y, f"\x00STREET\x00{rendered}"))
            else:
                body.append((y, rendered))
    return area, street, markers, body


def extract(pdf_path: str, report_id: str, limit_pages: int | None = None) -> list[Entry]:
    reader = PdfReader(pdf_path)
    pages = reader.pages if limit_pages is None else reader.pages[:limit_pages]

    entries: dict[tuple[str, tuple[int, ...]], Entry] = {}
    current: Entry | None = None
    street = ""  # carries across pages: a continuation page repeats no header

    for pno, page in enumerate(pages):
        try:
            area, street_raw, markers, body = parse_page(page)
        except Exception:
            continue
        if not body:
            continue
        if street_raw:
            street = normalize_street(street_raw)
        if not street:
            # Nothing has established a street yet; text here is unattributable.
            current = None
            continue

        # Marker y -> house numbers. "cont." carries no numbers; it just says the
        # previous entry continues here, which `current` already encodes.
        starts: list[tuple[int, list[int]]] = []
        for y, t in sorted(markers, key=lambda z: -z[0]):
            m = MARKER_RE.match(t.replace(" ", "") if t.count(" ") < 2 else t)
            if not m:
                continue
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else None
            starts.append((y, expand_range(lo, hi)))

        street_switch_y: float = float("inf")
        for y, line in body:
            if line.startswith("\x00STREET\x00"):
                # New street section opens here; the previous entry ends with it,
                # and so do its markers — a marker above this line belongs to the
                # street that just ended, not the one starting.
                street = normalize_street(line.split("\x00")[2])
                street_switch_y = y
                current = None
                continue
            if PAGE_NUM_RE.match(line):
                continue  # running page number, not prose
            # The applicable marker is the lowest one still at or above this
            # line, and below any street switch already seen on this page.
            applicable = [
                nums for my, nums in starts
                if my >= y - 2 and my <= street_switch_y
            ]
            if applicable:
                nums = applicable[-1]
                key = (street, tuple(nums))
                if current is None or current.key() != key:
                    current = entries.get(key)
                    if current is None:
                        current = Entry(report=report_id, area=area, street=street,
                                        numbers=nums)
                        entries[key] = current
            if current is None:
                continue  # leading text before any marker, unattributable
            if pno not in current.pages:
                current.pages.append(pno)
            current.text_parts.append(line)

    return list(entries.values())


# ── Prose-report format ───────────────────────────────────────────────────────
# A third document shape, and by BIN count the largest one: continuous narrative
# organised by STREET BLOCK, with buildings named inline. No marginal markers at
# all, so `extract()` sees no `#13` in the left margin and returns nothing —
# which is exactly why 41 of 43 "legacy"-detected reports yielded zero.
#
#     PROSPECT PARK WEST Between Eleventh & Twelfth Streets     <- block header
#     WEST SIDE (Nos. 162-169)                                  <- side header
#
#         Nos. 164 and 165 are two four-story apartment houses   <- entry para
#         built for William Murphy, according to plans by ...
#
# The attribution signal is the PARAGRAPH OPENING, not the margin: an entry
# paragraph begins "No. 158." / "Nos. 172-178." / "No. 47 (1383/31)". That is a
# far stronger cue than an inline mention, because a paragraph that OPENS with a
# house number is about that house, whereas a mid-sentence "No. 50" is often a
# comparison ("unlike No. 50 across the street").
#
# Both cues are captured, at different confidence:
#   * head  — paragraph opens with the numbers. The building's own entry.
#   * inline— a sentence elsewhere names the numbers. Appended after the head
#             text, and used alone only when there is no head paragraph.
#   * block — prose under the street header naming no numbers. Kept as shared
#             context for every building on the block, so a building with no
#             entry of its own still gets something better than a district blurb.

# "PROSPECT PARK WEST Between Tenth and Eleventh Streets",
# "GATES AVENUE BETWEEN WAVERLY AVENUE AND WASHINGTON AVENUE",
# "EAST 68TH STREET North Side". The existing STREET_HEADER_RE rejects the first
# two: it only allows a side qualifier, "(", or end-of-line after the
# thoroughfare word, so every Park Slope block header fails and the whole report
# goes unattributed.
PROSE_STREET_RE = re.compile(
    r"^[A-Z][A-Z0-9 .,'&\-]{3,60}?"
    r"\b(STREET|AVENUE|PLACE|SQUARE|ROAD|LANE|MEWS|COURT|TERRACE|PARK|ALLEY|WAY|"
    r"BOULEVARD|DRIVE|PARKWAY|PROMENADE|WALK|OVAL|CIRCLE|SLIP|ROW)\b"
    r"(\s+(NORTH|SOUTH|EAST|WEST))?"
    r"(?=\s*$|\s*\(|\s+[Bb]etw|\s+BETW|\s+(North|South|East|West)\s+Side\b"
    r"|\s+(NORTH|SOUTH|EAST|WEST)\s+SIDE\b|\s*,)"
)

# Trailing navigation on a block header: "Between X & Y", "(Nos. 162-169)",
# "North Side". Not part of the address.
PROSE_TAIL_RE = re.compile(
    r"\s*,?\s+betw(?:een)?\.?\b.*$|\s*\(.*$"
    r"|\s+(?:north|south|east|west)\s+side\b.*$",
    re.IGNORECASE,
)

# "NORTH SIDE", "WEST SIDE (Nos. 162-169)" — a sub-header inside a block, never
# prose. Dropped rather than treated as a street, since it carries no street name.
SIDE_HEADER_RE = re.compile(
    r"^\(?(NORTH|SOUTH|EAST|WEST|SOUI'H)\s+SIDE\b|^\(?(North|South|East|West)\s+Side\b",
)

# Running headers: the district's own abbreviation, stamped on nearly every page
# ("PS-HD", "PLG-HD", "CH-HD", and their OCR corruptions "PS-IID", "PS-HO").
# Matched by shape — short, hyphenated, all-caps — not by a list of districts.
RUNNING_HEAD_RE = re.compile(r"^[A-Z0-9]{1,6}[-–][A-Z0-9]{1,6}\.?$")

# Section furniture that is prose but not building prose.
NON_BUILDING_SECTION_RE = re.compile(
    r"^(GLOSSARY|FOOTNOTES|BIBLIOGRAPHY|ACKNOWLEDGMENTS?|INDEX|"
    r"TABLE OF CONTENTS|FINDINGS AND DESIGNATIONS?|TESTIMONY|REFERENCES)\b"
)

# Paragraph opener: "No. 158.", "Nos. 172-178.", "No. 162-163 is", "Nos. 164 and
# 165", "No. 47 (1383/31)". The scans routinely OCR "No." as "N0.", "Nn.", "lb."
# — the last one is unrecoverable without false positives, so it is left alone.
NUM_HEAD_RE = re.compile(r"^\(?(?:Nos?|N0s?|Nns?|NOS?)\s*[\.\,:;]?\s*(?=\d)")

# An inline mention anywhere in a sentence.
NUM_INLINE_RE = re.compile(r"\b(?:Nos?|N0s?)\s*[\.\,:]?\s*(\d[\d\s,&\-–—]*?\d|\d)")

# A number run: "217-255", "172, 174 and 176", "164 and 165".
NUM_RUN_RE = re.compile(
    r"(\d{1,4})((?:\s*(?:,|&|and|through|to|[-–—~])\s*\d{1,4})*)"
)

# Queens-style hyphenated house numbers ("85-02", "70-10") are ONE address, not
# a range. Distinguished by the zero-padded second half.
QUEENS_NUM_RE = re.compile(r"^\d{1,3}-0\d\b")

# A house number immediately followed by a street name is a CROSS-REFERENCE to a
# building on another street ("entered at No. 649 Eleventh Street", "the rear
# yard of No. 148-154 Lefferts Avenue"). Attributing that sentence to number 649
# on the CURRENT street files one building's description under another's address,
# which is the single worst failure mode here — so these are dropped.
CROSSREF_TAIL_RE = re.compile(
    r"^\s*(?:[A-Z][A-Za-z']*\.?\s+){1,3}"
    r"(Street|Avenue|Place|Road|Square|Terrace|Court|Lane|Boulevard|Parkway|West|East|North|South)\b"
)

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")

# How far into a paragraph's first sentence a number may appear and still count
# as the paragraph's subject. Sized to the opening noun phrase ("The row of
# eight brownstone houses at Nos. 156-170 ..." is 44 chars), not to a whole
# clause, so a comparison later in the sentence stays at the `inline` tier.
LEAD_WINDOW = 70

TIER_RANK = {"head": 3, "lead": 2, "inline": 1}


def _best_tier(a: str, b: str) -> str:
    return a if TIER_RANK.get(a, 0) >= TIER_RANK.get(b, 0) else b


def parse_number_run(text: str) -> tuple[list[int], int]:
    """Parse a leading run of house numbers. Returns (numbers, chars consumed).

    Handles the three forms the reports use interchangeably — a single number, a
    hyphen range, and a comma/and list — because the same block mixes them
    ("Nos. 172-178" two paragraphs above "Nos. 172, 174 and 176").
    """
    if QUEENS_NUM_RE.match(text):
        m = re.match(r"^(\d{1,3}-0\d)", text)
        # Keep only the low half; the DB stores Queens addresses hyphenated and
        # these reports are almost all Brooklyn/Manhattan, so this is a rare path.
        return [int(m.group(1).split("-")[0])], m.end()
    m = NUM_RUN_RE.match(text)
    if not m:
        return [], 0
    nums: list[int] = []
    first = int(m.group(1))
    rest = m.group(2) or ""
    parts = re.findall(r"(,|&|and|through|to|[-–—~])\s*(\d{1,4})", rest)
    prev = first
    nums.append(first)
    for sep, val in parts:
        v = int(val)
        if sep in ("-", "–", "—", "~", "through", "to"):
            nums = [n for n in nums if n != prev]
            nums.extend(expand_range(prev, v))
        else:
            nums.append(v)
        prev = v
    nums = sorted({n for n in nums if 1 <= n <= 9999})
    return nums, m.end()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class ProseEntry:
    """Same shape as `Entry` so the ingest path needs no special case, plus the
    provenance the prose format makes available."""
    report: str
    area: str
    street: str
    numbers: list[int]
    pages: list[int] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    inline_parts: list[str] = field(default_factory=list)
    block: str = ""          # street-block prose, shared context
    kind: str = "building"   # "building" | "block"

    @property
    def text(self) -> str:
        parts = self.text_parts + self.inline_parts
        return _clean(" ".join(parts))

    tier: str = "inline"

    @property
    def confidence(self) -> str:
        """How the building was attributed, worst-case across its paragraphs:

        head   — a paragraph OPENED with this number ("Nos. 172-178. These four
                 dignified limestone apartment houses..."). The paragraph is
                 unambiguously that building's entry.
        lead   — the number appears in the opening clause of the paragraph's
                 first sentence ("The row of eight brownstone houses at Nos.
                 156-170 was built in 1851-52..."). Same grammatical subject,
                 different surface form; Cobble Hill and Boerum Hill write every
                 entry this way and have no openers at all.
        inline — named only in a later sentence. Often still about the building
                 ("No. 165 has happily retained its original roof cornice") but
                 sometimes a comparison to a neighbour, so it is kept separate
                 rather than presented as the building's description.
        """
        return self.tier

    def key(self) -> tuple[str, tuple[int, ...]]:
        return (self.street, tuple(self.numbers))


def _page_lines(items: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Cluster positioned fragments into visual lines -> (x_start, y, text)."""
    lines: list[tuple[int, list[tuple[int, str]]]] = []
    for x, y, t in sorted(items, key=lambda z: (-z[1], z[0])):
        if lines and abs(lines[-1][0] - y) <= Y_TOL:
            lines[-1][1].append((x, t))
        else:
            lines.append((y, [(x, t)]))
    out = []
    for y, frags in lines:
        frags.sort()
        out.append((frags[0][0], y, " ".join(t for _, t in frags)))
    return out


def _paragraphs(lines: list[tuple[int, int, str]]) -> list[list[str]]:
    """Split a page's lines into paragraphs.

    Two independent cues, because the reports disagree on which they use: Park
    Slope indents the first line of every entry (x=105 against a body edge of
    75) while Prospect Lefferts Gardens indents nothing and separates paragraphs
    by a blank line instead. Keying on either one alone loses a whole family of
    reports, so both are honoured, plus the number-opener itself.
    """
    if not lines:
        return []
    xs = sorted(x for x, _, _ in lines)
    body_left = xs[len(xs) // 2]
    gaps = sorted(abs(lines[i - 1][1] - lines[i][1]) for i in range(1, len(lines)))
    line_gap = gaps[len(gaps) // 2] if gaps else 12

    paras: list[list[str]] = []
    prev_y = None
    for x, y, text in lines:
        big_gap = prev_y is not None and (prev_y - y) > line_gap * 1.6
        indented = x >= body_left + 12
        if not paras or big_gap or indented or NUM_HEAD_RE.match(text):
            paras.append([text])
        else:
            paras[-1].append(text)
        prev_y = y
    return paras


def _inline_hits(sentence: str) -> list[int]:
    """House numbers this sentence names, minus cross-street references."""
    out: list[int] = []
    for m in NUM_INLINE_RE.finditer(sentence):
        nums, used = parse_number_run(sentence[m.start(1):])
        if not nums:
            continue
        tail = sentence[m.start(1) + used:]
        if CROSSREF_TAIL_RE.match(tail):
            continue  # "No. 649 Eleventh Street" — a different street entirely
        out.extend(nums)
    return sorted(set(out))


def extract_prose_pages(pages: "list[tuple[int, list[tuple[int,int,str]]]]",
                        report_id: str) -> list[ProseEntry]:
    """Core of the prose mode, over already-positioned text so it can be tested
    against cached page dumps without re-downloading 100MB PDFs."""
    entries: dict[tuple[str, tuple[int, ...]], ProseEntry] = {}
    blocks: dict[str, list[str]] = {}
    street = ""
    area = ""
    current: ProseEntry | None = None
    skipping = False  # inside a glossary/footnotes section

    def get(nums: list[int]) -> ProseEntry:
        key = (street, tuple(nums))
        e = entries.get(key)
        if e is None:
            e = ProseEntry(report=report_id, area=area, street=street,
                           numbers=list(nums))
            entries[key] = e
        return e

    for pno, items in pages:
        lines = _page_lines(items)
        if not lines:
            continue
        for para in _paragraphs(lines):
            head = para[0].strip()
            if RUNNING_HEAD_RE.match(head) or PAGE_NUM_RE.match(head):
                continue
            if NON_BUILDING_SECTION_RE.match(head):
                skipping = True
                current = None
                continue
            if AREA_RE.search(head) and len(head) < 40:
                area = head
                continue
            if PROSE_STREET_RE.match(head):
                street = normalize_street(PROSE_TAIL_RE.sub("", head))
                skipping = False
                current = None
                # A header line can carry prose after it on the same line; the
                # header itself is never body text, so the paragraph is dropped
                # only if it is header-only.
                if len(para) == 1 and len(head) < 90:
                    continue
                para = para[1:]
                if not para:
                    continue
                head = para[0].strip()
            if SIDE_HEADER_RE.match(head):
                current = None
                if len(para) == 1:
                    continue
                para = para[1:]
                head = para[0].strip()
            if skipping or not street:
                continue

            body = _clean(" ".join(para))
            if len(body) < 40:
                continue

            sents = [s.strip() for s in SENTENCE_SPLIT_RE.split(body)]

            hm = NUM_HEAD_RE.match(body)
            nums: list[int] = []
            tier = ""
            if hm:
                nums, _used = parse_number_run(body[hm.end():])
                tier = "head"
            if not nums and sents:
                # No opener. Cobble Hill / Boerum Hill / Fort Greene never use
                # one — every entry reads "The row of eight brownstone houses at
                # Nos. 156-170 was built in 1851-52 by William Alexander." The
                # subject noun phrase still carries the numbers, so a mention
                # inside the opening clause of the first sentence is the entry.
                first = sents[0]
                for m in NUM_INLINE_RE.finditer(first):
                    if m.start() > LEAD_WINDOW:
                        break
                    cand, used = parse_number_run(first[m.start(1):])
                    if cand and not CROSSREF_TAIL_RE.match(first[m.start(1) + used:]):
                        nums, tier = cand, "lead"
                        break

            if nums:
                current = get(nums)
                current.kind = "building"
                current.tier = _best_tier(current.tier, tier)
                if pno not in current.pages:
                    current.pages.append(pno)
                current.text_parts.append(body)
            else:
                # Names nobody: shared context for the whole street block.
                blocks.setdefault(street, []).append(body)
                current = None

            # Sentence-level attribution for everyone else the paragraph names.
            # A head paragraph mentions its neighbours constantly ("similar to
            # Nos. 154 and 156 in the block to the north"), so these are held at
            # the weaker `inline` tier instead of being merged into the entry.
            own = set(nums)
            for si, sent in enumerate(sents):
                if len(sent) < 30:
                    continue
                for n in _inline_hits(sent):
                    if n in own:
                        continue
                    e = get([n])
                    e.tier = _best_tier(e.tier, "inline")
                    if pno not in e.pages:
                        e.pages.append(pno)
                    if sent not in e.inline_parts:
                        e.inline_parts.append(sent)

    out = _canonicalize_streets(list(entries.values()))
    for e in out:
        e.block = _clean(" ".join(blocks.get(e.street, [])))[:1500]
    return out


def _canonicalize_streets(entries: list[ProseEntry]) -> list[ProseEntry]:
    """Fold OCR-corrupted street names into their clean twin.

    These scans produce "EIGIITH AVENUE", "EIGHI'H AVENUE" and "F'1URTEENTH
    STREET" alongside the correct spellings, and each corruption becomes its own
    unmatchable street. The fold is by similarity to a MORE FREQUENT name within
    the same report — no dictionary of street names, and no cross-report state,
    so it cannot invent a street the report never printed.
    """
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.street] = counts.get(e.street, 0) + len(e.numbers)
    ranked = sorted(counts, key=lambda s: (-counts[s], s))
    mapping: dict[str, str] = {}
    for s in ranked:
        better = [t for t in ranked if counts[t] > counts[s]]
        m = difflib.get_close_matches(s, better, n=1, cutoff=0.82)
        mapping[s] = mapping.get(m[0], m[0]) if m else s

    merged: dict[tuple[str, tuple[int, ...]], ProseEntry] = {}
    for e in entries:
        e.street = mapping.get(e.street, e.street)
        prev = merged.get(e.key())
        if prev is None:
            merged[e.key()] = e
            continue
        prev.tier = _best_tier(prev.tier, e.tier)
        prev.pages = sorted(set(prev.pages) | set(e.pages))
        prev.text_parts.extend(p for p in e.text_parts if p not in prev.text_parts)
        prev.inline_parts.extend(p for p in e.inline_parts if p not in prev.inline_parts)
    return list(merged.values())


def _pdf_pages(pdf_path: str, limit_pages: int | None = None):
    reader = PdfReader(pdf_path)
    pages = reader.pages if limit_pages is None else reader.pages[:limit_pages]
    for pno, page in enumerate(pages):
        items: list[tuple[int, int, str]] = []

        def visitor(text, cm, tm, font_dict, font_size, items=items):
            t = (text or "").strip()
            if t:
                items.append((round(tm[4]), round(tm[5]), t))
        try:
            page.extract_text(visitor_text=visitor)
        except Exception:
            continue
        yield pno, items


def extract_prose(pdf_path: str, report_id: str,
                  limit_pages: int | None = None) -> list[ProseEntry]:
    return extract_prose_pages(list(_pdf_pages(pdf_path, limit_pages)), report_id)


def count_layout_cues(pdf_path: str, probe_pages: int = 60) -> tuple[int, int]:
    """(marginal markers, prose number-openers) over an even sample of pages.

    Sampled evenly rather than from the front: these reports open with dozens of
    pages of boundary description and hearing testimony, and a front-loaded
    probe sees neither cue.
    """
    reader = PdfReader(pdf_path)
    n = len(reader.pages)
    if n == 0:
        return 0, 0
    step = max(n // probe_pages, 1)
    marginal = prose = 0
    for i in range(0, n, step):
        items: list[tuple[int, int, str]] = []

        def visitor(text, cm, tm, font_dict, font_size, items=items):
            t = (text or "").strip()
            if t:
                items.append((round(tm[4]), round(tm[5]), t))
        try:
            reader.pages[i].extract_text(visitor_text=visitor)
        except Exception:
            continue
        lines = _page_lines(items)
        if not lines:
            continue
        xs = sorted(x for x, _, _ in lines)
        margin_max = xs[len(xs) // 2] - MARGIN_GAP
        for x, _y, text in lines:
            if x <= margin_max and MARKER_RE.match(text.replace(" ", "")):
                marginal += 1
            if NUM_HEAD_RE.match(text):
                prose += 1
    return marginal, prose


def detect_format(pdf_path: str) -> str:
    """'modern' | 'legacy' | 'prose'.

    Order matters: the structured Block/Lot record is unambiguous, so it wins
    outright. Between the two typewritten shapes the decision is by evidence
    count, not by LP number — the changeover was not chronological, and LP-0489
    (marginal markers) and LP-0709 (prose) are only 220 numbers apart.
    """
    if looks_modern(pdf_path):
        return "modern"
    marginal, prose = count_layout_cues(pdf_path)
    if prose > max(marginal, 3):
        return "prose"
    return "legacy"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--report", required=True, help="e.g. LP-0489")
    ap.add_argument("--limit-pages", type=int)
    ap.add_argument("--sample", type=int, default=0, help="print N sample entries")
    ap.add_argument("--json-out")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="drop entries thinner than this; they read as stubs")
    args = ap.parse_args()

    modern = looks_modern(args.pdf)
    print(f"format              : {'modern (Block/Lot records)' if modern else 'legacy (marginal markers)'}")

    if modern:
        mentries = [e for e in extract_modern(args.pdf, args.report, args.limit_pages)
                    if len(e.text) >= args.min_chars]
        print(f"entries >= {args.min_chars} chars : {len(mentries)}")
        print(f"with BBL            : {sum(1 for e in mentries if e.bbl)}")
        if mentries:
            lens = sorted(len(e.text) for e in mentries)
            print(f"text length med/max : {lens[len(lens)//2]} / {lens[-1]}")
        for e in mentries[: args.sample]:
            print(f"\n--- {e.address}  BBL={e.bbl}  page={e.page}")
            print(f"    {e.text[:400]}")
        if args.json_out:
            with open(args.json_out, "w") as fh:
                json.dump([{"report": e.report, "address": e.address, "bbl": e.bbl,
                            "page": e.page, "fields": e.fields, "text": e.text}
                           for e in mentries], fh, indent=1)
            print(f"\nwrote {args.json_out}")
        return 0

    entries = extract(args.pdf, args.report, args.limit_pages)
    kept = [e for e in entries if len(e.text) >= args.min_chars]

    addresses = sum(len(e.numbers) for e in kept)
    print(f"entries parsed      : {len(entries)}")
    print(f"entries >= {args.min_chars} chars : {len(kept)}")
    print(f"addresses covered   : {addresses}")
    if kept:
        lens = sorted(len(e.text) for e in kept)
        print(f"text length med/max : {lens[len(lens)//2]} / {lens[-1]}")

    for e in kept[: args.sample]:
        nums = ", ".join(str(n) for n in e.numbers)
        print(f"\n--- {nums} {e.street}  [{e.area}] pages={e.pages}")
        print(f"    {e.text[:400]}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump([{ "report": e.report, "area": e.area, "street": e.street,
                         "numbers": e.numbers, "pages": e.pages, "text": e.text }
                       for e in kept], fh, indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

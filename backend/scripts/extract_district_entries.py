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

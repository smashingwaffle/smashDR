"""
places.py - find streets / addresses / intersections in transcripts.

    find_places("Code 6 in the area of 2315 Cesar Chavez, then Soto and 1st Street")
    -> [(start, end, "2315 Cesar Chavez, Los Angeles, CA"),
        (start, end, "Soto & 1st Street, Los Angeles, CA")]

maps_url(query) -> Google Maps search link. Local street names live in
data/streets.json ("ambiguous" ones like Main or State only count with a
suffix, a house number or as part of an intersection).
"""

import json
import re
from urllib.parse import quote_plus

from smashdr.paths import DATA

CITY = "Los Angeles, CA"
SUFFIX = (r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Place|Pl|Way|Lane|Ln|"
          r"Court|Ct|Terrace|Parkway|Pkwy|Freeway|Fwy)\b\.?")
_DIR = r"(?:(?:North|South|East|West|N|S|E|W)\.?\s+)?"
# Words that look like streets but are radio talk
_STOP = {"Code", "Adam", "Boy", "Charles", "David", "Edward", "Frank", "George", "Henry",
         "Ida", "John", "King", "Lincoln", "Mary", "Nora", "Ocean", "Paul", "Queen",
         "Robert", "Sam", "Tom", "Union", "Victor", "William", "Young", "Zebra",
         "Unit", "Units", "Air", "Drone", "Copy", "Roger", "Suspect", "Vehicle", "The",
         "Hollenbeck", "Central", "Rampart", "Newton", "Northeast", "Show", "Handle"}

_streets = None


def _load():
    global _streets
    if _streets is None:
        try:
            d = json.loads((DATA / "streets.json").read_text(encoding="utf-8"))
            _streets = [(s["name"], bool(s.get("ambiguous"))) for s in d["streets"]]
        except (OSError, ValueError, KeyError) as e:
            print(f"[!] Could not load data/streets.json: {e}")
            _streets = []
        _streets.sort(key=lambda s: -len(s[0]))          # longest name first
    return _streets


def maps_url(query: str) -> str:
    # path form: no "&" in the URL (it got mangled inside rich-text links)
    return "https://www.google.com/maps/search/" + quote_plus(query)


def _street_spans(text):
    """Single street mentions: (start, end, ambiguous_and_bare)."""
    spans = []

    def free(s, e):
        return all(e <= a or s >= b for a, b, _ in spans)

    # 1) known local streets (optional direction prefix and suffix)
    for name, ambiguous in _load():
        rx = re.compile(rf"\b{_DIR}{re.escape(name)}(?:\s+{SUFFIX})?(?![\w])")
        for m in rx.finditer(text):
            if free(*m.span()):
                has_suffix = re.search(rf"\s{SUFFIX}$", m.group(0)) is not None
                spans.append((m.start(), m.end(), ambiguous and not has_suffix))
    # 2) any "Capitalised Name + suffix", and numbered streets ("1st Street", "Avenue 26")
    for rx in (re.compile(rf"\b{_DIR}((?:[A-Z][a-z]+\s+){{1,3}}){SUFFIX}"),
               re.compile(rf"\b{_DIR}\d{{1,3}}(?:st|nd|rd|th)(?:\s+{SUFFIX})?"),
               re.compile(r"\bAvenue\s+\d{1,3}\b")):
        for m in rx.finditer(text):
            first = m.group(0).split()[0].rstrip(".")
            if first in _STOP or not free(*m.span()):
                continue
            spans.append((m.start(), m.end(), False))
    return sorted(spans)


def find_places(text: str):
    """[(start, end, maps query)] for addresses, streets and intersections."""
    spans = _street_spans(text)
    out = []
    i = 0
    while i < len(spans):
        s, e, weak = spans[i]
        # intersection: "<street> and|&|at <street>"
        if i + 1 < len(spans):
            s2, e2, _w2 = spans[i + 1]
            if re.fullmatch(r"\s*(?:and|&|at)\s*", text[e:s2], re.I):
                q = f"{text[s:e]} & {text[s2:e2]}, {CITY}"
                out.append((s, e2, q))
                i += 2
                continue
        # house number in front: "2315 Cesar Chavez", or "3000 block of Whittier"
        m = re.search(r"(\d{2,5})\s+(?:block\s+of\s+)?$", text[:s], re.I)
        if m:
            q = f"{m.group(1)} {text[s:e]}, {CITY}"
            s = m.start(1)
            weak = False
        else:
            q = f"{text[s:e]}, {CITY}"
        if not weak:
            out.append((s, e, q))
        i += 1
    return out

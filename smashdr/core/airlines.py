"""
airlines.py - callsign -> airline (data/airlines.json) + a stable badge colour.

    split("AAL1234")  -> ("AAL", "American", "1234")
    split("N512LA")   -> (None, None, None)     # private / registration callsign
"""

import json
import re
import zlib

from smashdr.paths import DATA

# muted, readable on dark; picked per airline code by hash (not brand colours)
BADGE_COLORS = ["#3949ab", "#00897b", "#c0392b", "#8e24aa", "#6d4c41", "#2e7d32",
                "#ad1457", "#0277bd", "#ef6c00", "#546e7a", "#5e35b1", "#00838f"]
_table = None


def _load():
    global _table
    if _table is None:
        try:
            _table = json.loads((DATA / "airlines.json").read_text(encoding="utf-8"))["airlines"]
        except (OSError, ValueError, KeyError) as e:
            print(f"[!] Could not load data/airlines.json: {e}")
            _table = {}
    return _table


def split(callsign: str):
    m = re.fullmatch(r"([A-Z]{3})(\d[0-9A-Z]{0,4})", (callsign or "").strip())
    if not m:
        return None, None, None
    code, num = m.groups()
    return code, _load().get(code), num


def badge_color(code: str) -> str:
    return BADGE_COLORS[zlib.crc32(code.encode()) % len(BADGE_COLORS)]


# altitude colour scale, shared with the map (feet -> hex)
ALT_STOPS = [(0, (255, 152, 0)), (5000, (255, 235, 59)), (10000, (139, 195, 74)),
             (20000, (38, 198, 218)), (30000, (66, 165, 245)), (40000, (171, 71, 188))]


def alt_color(alt) -> str:
    if alt is None:
        return "#9e9e9e"
    a = max(0, min(alt, ALT_STOPS[-1][0]))
    for (a0, c0), (a1, c1) in zip(ALT_STOPS, ALT_STOPS[1:]):
        if a <= a1:
            t = (a - a0) / (a1 - a0)
            return "#%02x%02x%02x" % tuple(int(x0 + (x1 - x0) * t) for x0, x1 in zip(c0, c1))
    return "#%02x%02x%02x" % ALT_STOPS[-1][1]

"""
squawks.py - transponder (Mode A) code meanings, from data/squawks.json.

    describe("7700") -> {"label": "EMERGENCY", "meaning": "...", "level": "emergency"}
    describe("4521") -> {"label": "ATC", "meaning": "Discrete code assigned by ATC", "level": "normal"}
"""

import json

from smashdr.paths import DATA

EMERGENCY_CODES = {"7500", "7600", "7700"}
# ADS-B emergency/priority status (TC 28 subtype 1, ME bits 9-11)
EMERGENCY_STATE = {1: "general emergency", 2: "medical", 3: "minimum fuel", 4: "no communications",
                   5: "unlawful interference", 6: "downed aircraft"}
_codes = None
_ranges = None


def _load():
    global _codes, _ranges
    if _codes is None:
        _codes, _ranges = {}, []
        try:
            data = json.loads((DATA / "squawks.json").read_text(encoding="utf-8"))["codes"]
        except (OSError, ValueError, KeyError) as e:
            print(f"[!] Could not load data/squawks.json: {e}")
            data = []
        for e in data:
            if "code" in e:
                _codes[e["code"]] = e
            elif "range" in e:
                _ranges.append((int(e["range"][0], 8), int(e["range"][1], 8), e))
    return _codes


def describe(code: str):
    if not code:
        return None
    c = _load().get(code)
    if c:
        return {"label": c["label"], "meaning": c["meaning"], "level": c["level"]}
    try:
        v = int(code, 8)
    except ValueError:
        return None
    for lo, hi, e in _ranges:
        if lo <= v <= hi:
            return {"label": e["label"], "meaning": e["meaning"], "level": e["level"]}
    return {"label": "ATC", "meaning": "Discrete code assigned by ATC", "level": "normal"}

"""
codes.py - explain radio / penal / vehicle codes found in transcripts.

    annotate("13A27, Code 6 in the area of 2315 Cesar Chavez, 211 suspect")
    -> [("Code 6", "Out of the car to investigate (LAPD)", start, end),
        ("211", "Robbery (PC)", start, end)]

Glossary: data/codes.json (edit freely; entries with "lapd": true override the
generic meaning). Bare numbers like 211 or 459 count only when they don't look
like an address ("2315 Cesar Chavez", "3000 block").
"""

import json
import re

from smashdr.paths import DATA

_KIND = {"PC": "PC", "VC": "VC", "HS": "H&S"}
_ADDRESS_NEXT = re.compile(
    r"\s+(block|north|south|east|west|n|s|e|w|street|st|avenue|ave|boulevard|blvd|"
    r"drive|dr|road|rd|place|pl|way|lane|ln|court|ct)\b", re.I)
_PATTERNS = [
    ("code", re.compile(r"\bcode\s+(\d{1,2}[A-Za-z]?)\b", re.I)),
    ("ten", re.compile(r"\b(1[01])[- ](\d{1,3}[A-Za-z]?)\b")),
    ("num", re.compile(r"(?<![\w.-])(\d{3,5}(?:\.\d{1,2})?[a-zA-Z]?)(?![\w-])")),
]
_codes = None
_lower = None


def _load():
    global _codes, _lower
    if _codes is None:
        try:
            _codes = json.loads((DATA / "codes.json").read_text(encoding="utf-8"))["codes"]
        except (OSError, ValueError, KeyError) as e:
            print(f"[!] Could not load data/codes.json: {e}")
            _codes = {}
        _lower = {k.lower(): k for k in _codes}
    return _codes


def _lookup(key):
    k = _lower.get(key.lower())
    return (k, _codes[k]) if k else (None, None)


def annotate(text: str):
    """[(matched text, meaning, start, end)] in text order, no overlaps."""
    _load()
    found = []
    taken = []
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            if kind == "code":
                key = f"Code {m.group(1)}"
            elif kind == "ten":
                key = f"{m.group(1)}-{m.group(2)}"
            else:
                key = m.group(1)
                rest = text[e:]
                if _ADDRESS_NEXT.match(rest) or re.match(r"\s+[A-Z][a-z]", rest):
                    continue                                   # house number / block
            k, v = _lookup(key)
            if k is None and kind in ("code", "ten") and key[-1].isalpha():
                k, v = _lookup(key[:-1])                       # "Code 6C" -> Code 6
            if k is None:
                continue
            meaning = v["m"].rstrip(".")
            if v.get("kind") in _KIND:
                meaning += f" ({_KIND[v['kind']]})"
            found.append((m.group(0), meaning, s, e))
            taken.append((s, e))
    return sorted(found, key=lambda f: f[2])

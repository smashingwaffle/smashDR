"""settings.py - tiny JSON settings store (settings.json next to the app, not in git)."""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / "settings.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict):
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PATH)

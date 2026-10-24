"""settings.py - tiny JSON settings store (user/settings.json, not in git)."""

import json

from smashdr.paths import user_file

PATH = user_file("settings.json")


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict):
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PATH)

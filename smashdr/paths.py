"""
Where things live.

    <root>/main.py            start here (F5)
    <root>/smashdr/           the code
    <root>/data/              defaults shipped in git (presets_default.json)
    <root>/user/              your settings.json and presets.json (not in git)
    <root>/recordings/        IQ recordings (not in git)
    <root>/ or <root>/lib/    rtlsdr.dll, msvcr100.dll, pthreadVC2.dll, mbe-neo.dll
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
USER = ROOT / "user"
RECORDINGS = ROOT / "recordings"
LIB_DIRS = (ROOT, ROOT / "lib")


def user_file(name: str) -> Path:
    """Path of a user file in user/, moving an old copy from the root there once."""
    USER.mkdir(exist_ok=True)
    new, old = USER / name, ROOT / name
    if old.exists() and not new.exists():
        try:
            old.replace(new)
            print(f"[i] Moved {name} to {USER}")
        except OSError as e:
            print(f"[!] Could not move {old} to {new}: {e}")
            return old
    return new

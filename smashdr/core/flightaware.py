"""
flightaware.py - FlightAware AeroAPI (v4) flight lookups: today's real route.

    URL = flight_url("JBU1301")          GET with header  x-apikey: <key>
    summary(json_bytes)  -> {"orig": ("JFK", "New York"), "dest": ("LAX", "Los Angeles"),
                             "status": "En Route / On Time", "dep": "06:12", "arr": "09:31 est",
                             "dep_delay": 12, "arr_delay": -3, "type": "A321", "reg": "N979JT"}
                         or None when no matching flight

Personal tier: free usage credit each month, ~$0.005 per /flights/{ident}
call, so smashDR keeps a monthly counter and a cap (Settings).
"""

import json
from datetime import datetime, timezone
from urllib.parse import quote

BASE = "https://aeroapi.flightaware.com/aeroapi"
COST_PER_CALL = 0.005            # USD, as billed (docs say 0.001)


def flight_url(ident: str) -> str:
    return f"{BASE}/flights/{quote(ident)}?max_pages=1"


def _t(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def pick(flights, now=None):
    """The flight that is in the air now; else the one closest to now."""
    now = now or datetime.now(timezone.utc)
    best, best_d = None, None
    for f in flights or []:
        if not isinstance(f, dict) or f.get("cancelled"):
            continue
        off, on = _t(f.get("actual_off")), _t(f.get("actual_on"))
        if off and not on:
            return f                                            # airborne
        ref = (off or _t(f.get("estimated_off")) or _t(f.get("scheduled_off"))
               or _t(f.get("scheduled_out")))
        if ref is None:
            continue
        d = abs((ref - now).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = f, d
    return best


def _local(dt):
    return dt.astimezone().strftime("%H:%M") if dt else None


def _ap(a):
    a = a if isinstance(a, dict) else {}
    code = a.get("code_iata") or a.get("code_icao") or a.get("code") or "?"
    return code, a.get("city") or a.get("name") or ""


def summary(data: bytes, now=None):
    try:
        flights = json.loads(data).get("flights")
    except (ValueError, AttributeError):
        return None
    f = pick(flights, now)
    if f is None:
        return None
    dep = f.get("actual_off") or f.get("actual_out")
    dep_s = _local(_t(dep)) if dep else (
        (_local(_t(f.get("estimated_out") or f.get("scheduled_out"))) or "?") + " sched")
    arr = f.get("actual_on") or f.get("actual_in")
    if arr:
        arr_s = _local(_t(arr))
    else:
        est = f.get("estimated_on") or f.get("estimated_in")
        arr_s = (_local(_t(est)) + " est") if est else (
            (_local(_t(f.get("scheduled_in"))) or "?") + " sched")
    mins = lambda s: None if s is None else round(s / 60)          # noqa: E731
    return {"orig": _ap(f.get("origin")), "dest": _ap(f.get("destination")),
            "status": f.get("status") or "", "dep": dep_s, "arr": arr_s,
            "dep_delay": mins(f.get("departure_delay")), "arr_delay": mins(f.get("arrival_delay")),
            "type": f.get("aircraft_type") or "", "reg": f.get("registration") or "",
            "diverted": bool(f.get("diverted"))}

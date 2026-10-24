"""
airports.py - guess "arriving at / departed from" from what a plane is doing.

Route databases (adsbdb) map a flight number to its *usual* route; airlines
reuse numbers, so it is often wrong. A plane low, descending and pointed at a
nearby airport is arriving there, whatever the database says.

    infer(plane) -> ("arr" | "dep", "LAX", "Los Angeles") or None
"""

import math

# SoCal airports: IATA (or FAA id) -> (name, lat, lon)
AIRPORTS = {
    "LAX": ("Los Angeles", 33.9425, -118.4081),
    "BUR": ("Burbank", 34.2007, -118.3587),
    "LGB": ("Long Beach", 33.8177, -118.1516),
    "SNA": ("Santa Ana / John Wayne", 33.6757, -117.8682),
    "ONT": ("Ontario", 34.0560, -117.6012),
    "VNY": ("Van Nuys", 34.2098, -118.4898),
    "SMO": ("Santa Monica", 34.0158, -118.4513),
    "HHR": ("Hawthorne", 33.9228, -118.3352),
    "EMT": ("El Monte", 34.0861, -118.0350),
    "CPM": ("Compton", 33.8900, -118.2437),
    "FUL": ("Fullerton", 33.8720, -117.9798),
    "TOA": ("Torrance", 33.8034, -118.3396),
    "WHP": ("Whiteman", 34.2593, -118.4134),
    "POC": ("La Verne / Brackett", 34.0916, -117.7818),
    "CNO": ("Chino", 33.9747, -117.6366),
    "RAL": ("Riverside", 33.9519, -117.4451),
    "SBD": ("San Bernardino", 34.0954, -117.2349),
    "PMD": ("Palmdale", 34.6294, -118.0846),
    "WJF": ("Lancaster / Fox", 34.7411, -118.2186),
    "SLI": ("Los Alamitos Army Airfield", 33.7900, -118.0517),
    "AVX": ("Catalina", 33.4049, -118.4158),
    "AJO": ("Corona", 33.8975, -117.6024),
    "RIV": ("March Air Reserve Base", 33.8807, -117.2594),
    "HMT": ("Hemet-Ryan", 33.7340, -117.0226),
    "F70": ("French Valley", 33.5742, -117.1285),
    "VCV": ("Victorville / SCLA", 34.5975, -117.3830),
    "APV": ("Apple Valley", 34.5753, -117.1862),
    "L35": ("Big Bear City", 34.2637, -116.8560),
    "EDW": ("Edwards Air Force Base", 34.9054, -117.8839),
    "PSP": ("Palm Springs", 33.8297, -116.5067),
    "UDD": ("Bermuda Dunes", 33.7484, -116.2748),
    "TRM": ("Thermal / Jacqueline Cochran", 33.6267, -116.1600),
    "SAN": ("San Diego", 32.7336, -117.1897),
    "CRQ": ("Carlsbad / McClellan-Palomar", 33.1283, -117.2800),
    "OCN": ("Oceanside", 33.2190, -117.3496),
    "MYF": ("San Diego / Montgomery-Gibbs", 32.8157, -117.1396),
    "SEE": ("El Cajon / Gillespie", 32.8262, -116.9724),
    "SDM": ("San Diego / Brown Field", 32.5723, -116.9802),
    "NKX": ("MCAS Miramar", 32.8684, -117.1428),
    "NZY": ("NAS North Island", 32.6992, -117.2153),
    "OXR": ("Oxnard", 34.2008, -119.2072),
    "CMA": ("Camarillo", 34.2137, -119.0943),
    "NTD": ("NAS Point Mugu", 34.1203, -119.1210),
    "SZP": ("Santa Paula", 34.3472, -119.0610),
    "SBA": ("Santa Barbara", 34.4262, -119.8401),
    "SMX": ("Santa Maria", 34.8989, -120.4575),
    "IPL": ("Imperial County", 32.8342, -115.5786),
    "BLH": ("Blythe", 33.6192, -114.7169),
}
MILITARY = {"SLI", "RIV", "EDW", "NKX", "NZY", "NTD"}
KM_PER_MI = 1.609344
AIRLINE_FIELDS = {"LAX", "BUR", "LGB", "SNA", "ONT", "SAN", "PSP", "SBA"}   # airline service
JET_FIELDS = AIRLINE_FIELDS | {"VNY", "SMO", "HHR", "PMD", "SBD", "CNO", "FUL", "RAL", "CRQ",
                               "MYF", "TRM", "CMA", "OXR", "VCV", "SMX", "IPL"} | MILITARY


def kind(code):
    return "airline" if code in AIRLINE_FIELDS else ("military" if code in MILITARY else "ga")


def map_list():
    """[{code, name, lat, lon, kind}] for the aircraft map."""
    return [{"code": c, "name": n, "lat": la, "lon": lo, "kind": kind(c)}
            for c, (n, la, lo) in AIRPORTS.items()]


def _dist_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    km = 6371.0 * 2 * math.asin(math.sqrt(h))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km / KM_PER_MI, math.degrees(math.atan2(y, x)) % 360


def _is_airline(callsign):
    from smashdr.core import airlines
    code, name, _num = airlines.split(callsign or "")
    return bool(name)


def _angle(a, b):
    return abs((a - b + 180) % 360 - 180)


def infer(p):
    """p: tracker dict with lat, lon, alt, track, vrate."""
    lat, lon, alt, trk, vr = p.get("lat"), p.get("lon"), p.get("alt"), p.get("track"), p.get("vrate")
    if lat is None or alt is None or trk is None or alt > 9000:
        return None
    if _is_airline(p.get("callsign")):
        fields = AIRLINE_FIELDS
    elif (p.get("gs") or 0) > 180:
        fields = JET_FIELDS
    else:
        fields = [c for c in AIRPORTS if c not in MILITARY]
    best = None
    for code in fields:
        name, alat, alon = AIRPORTS[code]
        mi, brg = _dist_bearing(lat, lon, alat, alon)
        if mi < 0.5:
            continue
        # arriving: descending (or low and level on final), heading at the field
        arriving = ((vr is not None and vr <= -300) or alt < 2500) and mi <= 25 \
            and _angle(trk, brg) <= 20 and alt <= 400 * mi + 1500
        # departing: climbing, heading away from the field, close in
        departing = vr is not None and vr >= 300 and mi <= 15 \
            and _angle(trk, (brg + 180) % 360) <= 30 and alt <= 700 * mi + 2500
        for kind, ok in (("arr", arriving), ("dep", departing)):
            if ok and (best is None or mi < best[0]):
                best = (mi, kind, code, name)
    return None if best is None else best[1:]

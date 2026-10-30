"""
satellites.py - satellite position, look angles and pass prediction (no Qt).

    tle = fetch_tle(25544)                      # CelesTrak, cached in user/cache/tle
    sat = Sat("ISS", *tle)
    sat.subpoint(t) -> (lat, lon, alt_km, speed_kmh)
    sat.look(t, home) -> (az, el, range_km, range_rate_km_s)
    passes(sat, home, t0, hours=48) -> [Pass, ...]

Orbit model: SGP4 (vendored python-sgp4) in TEME, rotated to Earth-fixed with
GMST (no polar motion / nutation: ~1 km, fine for antennas and maps).
"""

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field

from smashdr.paths import user_file
from smashdr.thirdparty.sgp4.model import Satrec

R_EQ = 6378.137            # WGS84
F_WGS = 1 / 298.257223563
OMEGA_E = 7.292115146706979e-5      # rad/s
C_KM_S = 299792.458

SATS = {                   # name -> (NORAD id, downlinks to show / tune)
    "ISS": (25544, {"APRS / packet 145.825": 145_825_000, "Voice / SSTV 145.800": 145_800_000,
                    "Voice uplink 144.490": 144_490_000}),
}
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={id}&FORMAT=tle"
TLE_MAX_AGE_S = 12 * 3600


# ---- TLEs ------------------------------------------------------------------
def _cache_path():
    return user_file("cache/tle.json")


def fetch_tle(norad: int, timeout=10.0):
    """(line1, line2) from CelesTrak (cached 12 h; the cache is used when offline).
    Returns (line1, line2, age_s) or None."""
    path = _cache_path()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(str(norad))
    if hit and time.time() - hit["t"] < TLE_MAX_AGE_S:
        return hit["l1"], hit["l2"], time.time() - hit["t"]
    try:
        req = urllib.request.Request(TLE_URL.format(id=norad), headers={"User-Agent": "smashDR"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            lines = [ln.strip() for ln in r.read().decode("ascii", "replace").splitlines() if ln.strip()]
        l1 = next(ln for ln in lines if ln.startswith("1 "))
        l2 = next(ln for ln in lines if ln.startswith("2 "))
        cache[str(norad)] = {"l1": l1, "l2": l2, "t": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
        return l1, l2, 0.0
    except Exception as e:                      # offline: fall back to an old TLE
        print(f"[sat] TLE download failed ({e}); using the cache" if hit else f"[sat] TLE download failed: {e}")
        if hit:
            return hit["l1"], hit["l2"], time.time() - hit["t"]
        return None


# ---- frames ------------------------------------------------------------------
def _jd(t):
    jd = t / 86400.0 + 2440587.5
    return math.floor(jd), jd - math.floor(jd)


def gmst(t):
    """Greenwich mean sidereal time (rad), IAU 1982."""
    jd = t / 86400.0 + 2440587.5
    tu = (jd - 2451545.0) / 36525.0
    s = (67310.54841 + (876600 * 3600 + 8640184.812866) * tu + 0.093104 * tu * tu
         - 6.2e-6 * tu ** 3)
    return math.radians((s % 86400) / 240.0)


def ecef_of_site(lat, lon, h_km=0.0):
    la, lo = math.radians(lat), math.radians(lon)
    e2 = F_WGS * (2 - F_WGS)
    n = R_EQ / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + h_km) * math.cos(la) * math.cos(lo), (n + h_km) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + h_km) * math.sin(la))


def geodetic(x, y, z):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    e2 = F_WGS * (2 - F_WGS)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        n = R_EQ / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    n = R_EQ / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return math.degrees(lat), math.degrees(lon), p / math.cos(lat) - n


def sun_eci(t):
    """Unit vector to the Sun (low precision, ~0.01 deg), equatorial frame."""
    n = t / 86400.0 + 2440587.5 - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 4e-7 * n)
    return math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)


# ---- satellite -------------------------------------------------------------------
class Sat:
    def __init__(self, name, line1, line2):
        self.name = name
        self.rec = Satrec.twoline2rv(line1, line2)
        self.epoch = (self.rec.jdsatepoch + self.rec.jdsatepochF - 2440587.5) * 86400.0

    def teme(self, t):
        e, r, v = self.rec.sgp4(*_jd(t))
        if e != 0:
            raise ValueError(f"SGP4 error {e}")
        return r, v

    def ecef(self, t):
        (x, y, z), (vx, vy, vz) = self.teme(t)
        th = gmst(t)
        c, s = math.cos(th), math.sin(th)
        rx, ry = c * x + s * y, -s * x + c * y
        vx2, vy2 = c * vx + s * vy, -s * vx + c * vy
        # Earth-fixed velocity: remove the frame's rotation (omega x r)
        return (rx, ry, z), (vx2 + OMEGA_E * ry, vy2 - OMEGA_E * rx, vz)

    def subpoint(self, t):
        (x, y, z), _ = self.ecef(t)
        lat, lon, alt = geodetic(x, y, z)
        _, v = self.teme(t)
        return lat, lon, alt, math.sqrt(sum(c * c for c in v)) * 3600

    def look(self, t, home):
        """home = (lat, lon, h_km) -> (az deg, el deg, range km, range rate km/s)."""
        (x, y, z), (vx, vy, vz) = self.ecef(t)
        ox, oy, oz = ecef_of_site(*home)
        dx, dy, dz = x - ox, y - oy, z - oz
        la, lo = math.radians(home[0]), math.radians(home[1])
        sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
        south = sl * co * dx + sl * so * dy - cl * dz
        east = -so * dx + co * dy
        up = cl * co * dx + cl * so * dy + sl * dz
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        el = math.degrees(math.asin(up / rng))
        az = math.degrees(math.atan2(east, -south)) % 360
        rr = (dx * vx + dy * vy + dz * vz) / rng
        return az, el, rng, rr

    def sunlit(self, t):
        r, _ = self.teme(t)
        s = sun_eci(t)
        d = sum(a * b for a, b in zip(r, s))
        if d > 0:
            return True                                   # on the Sun's side
        perp = math.sqrt(max(0.0, sum(a * a for a in r) - d * d))
        return perp > R_EQ                                # outside Earth's shadow cylinder


def sun_elevation(t, home):
    s = sun_eci(t)
    th = gmst(t)
    c, sn = math.cos(th), math.sin(th)
    x, y, z = c * s[0] + sn * s[1], -sn * s[0] + c * s[1], s[2]
    la, lo = math.radians(home[0]), math.radians(home[1])
    up = math.cos(la) * math.cos(lo) * x + math.cos(la) * math.sin(lo) * y + math.sin(la) * z
    return math.degrees(math.asin(max(-1.0, min(1.0, up))))


def doppler(f_hz, range_rate_km_s):
    return -f_hz * range_rate_km_s / C_KM_S


def compass(az):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((az + 22.5) % 360 // 45)]


# ---- passes ----------------------------------------------------------------
@dataclass
class Pass:
    aos: float
    los: float
    tmax: float
    max_el: float
    aos_az: float
    los_az: float
    visible: bool = False                  # sunlit satellite in a dark sky
    track: list = field(default_factory=list)       # [(t, az, el)]

    @property
    def duration(self):
        return self.los - self.aos


def _el(sat, home, t):
    return sat.look(t, home)[1]


def _cross(sat, home, a, b, rising):
    """Time (1 s) where elevation crosses 0 between a and b."""
    for _ in range(20):
        m = (a + b) / 2
        if (_el(sat, home, m) > 0) == rising:
            b = m
        else:
            a = m
        if b - a < 1:
            break
    return (a + b) / 2


def passes(sat, home, t0=None, hours=48.0, step=30.0, min_el=0.0):
    t0 = time.time() if t0 is None else t0
    out = []
    t = t0
    prev = _el(sat, home, t)
    aos = t0 if prev > 0 else None
    end = t0 + hours * 3600
    while t < end:
        t2 = t + step
        el = _el(sat, home, t2)
        if prev <= 0 < el:
            aos = _cross(sat, home, t, t2, True)
        elif prev > 0 >= el and aos is not None:
            los = _cross(sat, home, t, t2, False)
            out.append(_make_pass(sat, home, aos, los))
            aos = None
        prev, t = el, t2
    return [p for p in out if p.max_el >= min_el]


def _make_pass(sat, home, aos, los):
    track = []
    n = max(20, int((los - aos) / 10))
    for i in range(n + 1):
        tt = aos + (los - aos) * i / n
        az, el, _r, _rr = sat.look(tt, home)
        track.append((tt, az, max(0.0, el)))
    tmax, _az, max_el = max(track, key=lambda x: x[2])
    vis = any(sat.sunlit(tt) and sun_elevation(tt, home) < -6 and el > 10
              for tt, _a, el in track[::3])
    return Pass(aos, los, tmax, max_el, track[0][1], track[-1][1], vis, track)


def ground_track(sat, t0, minutes_back=45, minutes_fwd=95, step_s=30):
    pts = []
    for k in range(-minutes_back * 60 // step_s, minutes_fwd * 60 // step_s + 1):
        lat, lon, _a, _s = sat.subpoint(t0 + k * step_s)
        pts.append((round(lat, 3), round(lon, 3), k >= 0))
    return pts


def footprint_radius_km(alt_km):
    """Ground distance to the horizon circle (elevation 0) for a satellite at alt_km."""
    return R_EQ * math.acos(R_EQ / (R_EQ + alt_km))

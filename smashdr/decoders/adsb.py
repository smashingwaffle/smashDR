"""
adsb.py - ADS-B (Mode S extended squitter, 1090 MHz) receive + decode.

    rx = AdsbDemod()                    # 2.0 MS/s, centre 1090 MHz
    for msg in rx.process(iq):          # iq: complex64 block
        ...                             # 14-byte DF17/18 messages, CRC checked
    tracker = Tracker(home_lat, home_lon)
    tracker.update(msg_bytes, t)        # -> aircraft dict, kept per ICAO address

Pulse-position modulation, 1 Mbit/s: at 2 samples/us each bit is two samples
(high-low = 1, low-high = 0) after an 8 us preamble (pulses at 0, 1, 3.5, 4.5 us).
Positions use CPR "local" decoding against a reference (the aircraft's last
fix, or home), so one message is enough; good to ~180 NM from home.
"""

import math

import numpy as np

RATE = 2_000_000
FREQ = 1_090_000_000
PREAMBLE = 16                 # samples (8 us)
LONG_BITS = 112
NEED = PREAMBLE + 2 * LONG_BITS
CRC_POLY = 0xFFF409
CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


# --------------------------------------------------------------------------
# CRC-24 (Mode S)
# --------------------------------------------------------------------------
def crc24(data: bytes) -> int:
    """Remainder of the whole message; 0 for a valid DF17/18."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1000000 | CRC_POLY
    return crc & 0xFFFFFF


def _crc_ok_batch(bits: np.ndarray) -> np.ndarray:
    """bits: (n, 112) uint8 -> bool (n,) remainder == 0, vectorised long division."""
    reg = bits.astype(np.uint8).copy()
    gen = np.array([(0x1FFF409 >> (24 - i)) & 1 for i in range(25)], dtype=np.uint8)
    for i in range(LONG_BITS - 24):
        lead = reg[:, i].astype(bool)
        if lead.any():
            reg[lead, i:i + 25] ^= gen
    return ~reg[:, LONG_BITS - 24:].any(axis=1)


# --------------------------------------------------------------------------
# Demodulator
# --------------------------------------------------------------------------
class AdsbDemod:
    def __init__(self):
        self.tail = np.zeros(0, dtype=np.float32)
        self.messages = 0
        self.candidates = 0

    def process(self, iq: np.ndarray):
        m = np.abs(iq).astype(np.float32)
        m = np.concatenate([self.tail, m])
        out = []
        n = len(m) - NEED
        if n > 0:
            out = self._scan(m, n)
            self.tail = m[n:]
        else:
            self.tail = m
        return out

    def _scan(self, m, n):
        s = [m[k:k + n] for k in range(10)]
        hi = (s[0] + s[2] + s[7] + s[9]) * 0.25
        lo = (s[1] + s[3] + s[4] + s[5] + s[6] + s[8]) * (1 / 6)
        noise = float(np.median(m)) + 1e-6
        ok = ((s[0] > s[1]) & (s[1] < s[2]) & (s[2] > s[3]) & (s[3] < s[0]) &
              (s[4] < s[0]) & (s[5] < s[0]) & (s[6] < s[0]) & (s[7] > s[8]) &
              (s[8] < s[9]) & (s[9] > s[6]) & (hi > 1.6 * lo) & (hi > 2.2 * noise))
        idx = np.flatnonzero(ok)
        if len(idx) == 0:
            return []
        # quiet zone between the preamble pulses and the data
        q = idx[(m[idx + 11] < hi[idx]) & (m[idx + 12] < hi[idx]) &
                (m[idx + 13] < hi[idx]) & (m[idx + 14] < hi[idx])]
        if len(q) == 0:
            return []
        self.candidates += len(q)
        pos = q[:, None] + PREAMBLE + 2 * np.arange(LONG_BITS)[None, :]
        bits = (m[pos] > m[pos + 1]).astype(np.uint8)
        df = (bits[:, 0] << 4) | (bits[:, 1] << 3) | (bits[:, 2] << 2) | (bits[:, 3] << 1) | bits[:, 4]
        keep = (df == 17) | (df == 18)
        q, bits = q[keep], bits[keep]
        if len(q) == 0:
            return []
        good = _crc_ok_batch(bits)
        out, last = [], -10 ** 9
        for start, b in zip(q[good], bits[good]):
            if start - last < NEED:                   # same message seen at an offset
                continue
            last = start
            out.append(np.packbits(b).tobytes())
        self.messages += len(out)
        return out


# --------------------------------------------------------------------------
# Message decoding
# --------------------------------------------------------------------------
def _bits(msg: bytes, a: int, b: int) -> int:
    """Bits a..b (1-based, inclusive) of the message."""
    v = int.from_bytes(msg, "big")
    n = len(msg) * 8
    return (v >> (n - b)) & ((1 << (b - a + 1)) - 1)


def icao(msg: bytes) -> str:
    return msg[1:4].hex().upper()


def typecode(msg: bytes) -> int:
    return _bits(msg, 33, 37)


def callsign(msg: bytes) -> str:
    cs = "".join(CHARSET[_bits(msg, 41 + 6 * i, 46 + 6 * i)] for i in range(8))
    return cs.replace("#", "").strip()


def category(msg: bytes) -> tuple:
    return typecode(msg), _bits(msg, 38, 40)


def altitude(msg: bytes):
    tc = typecode(msg)
    raw = _bits(msg, 41, 52)
    if 20 <= tc <= 22:                                # GNSS height, metres
        return round(raw * 3.28084)
    if raw & 0x10:                                    # Q bit: 25 ft steps
        n = ((raw & 0xFE0) >> 1) | (raw & 0x0F)
        return n * 25 - 1000
    return None                                       # Gillham (rare), skip


def cpr(msg: bytes):
    return _bits(msg, 54, 54), _bits(msg, 55, 71), _bits(msg, 72, 88)


def _nl(lat: float) -> int:
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    a = 1 - math.cos(math.pi / 30)
    b = math.cos(math.pi / 180 * abs(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(1 - a / b)))


def cpr_local(f: int, lat_cpr: int, lon_cpr: int, ref_lat: float, ref_lon: float):
    yz, xz = lat_cpr / 131072, lon_cpr / 131072
    dlat = 360 / (60 - f)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + (ref_lat % dlat) / dlat - yz)
    lat = dlat * (j + yz)
    ni = max(_nl(lat) - f, 1)
    dlon = 360 / ni
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + (ref_lon % dlon) / dlon - xz)
    lon = dlon * (m + xz)
    return lat, lon


def velocity(msg: bytes):
    """(ground speed kt, track deg, vertical rate ft/min) or None."""
    st = _bits(msg, 38, 40)
    vr_sign, vr = _bits(msg, 69, 69), _bits(msg, 70, 78)
    rate = None if vr == 0 else (vr - 1) * 64 * (-1 if vr_sign else 1)
    if st in (1, 2):
        ew_dir, ew = _bits(msg, 46, 46), _bits(msg, 47, 56)
        ns_dir, ns = _bits(msg, 57, 57), _bits(msg, 58, 67)
        if ew == 0 or ns == 0:
            return None
        k = 4 if st == 2 else 1
        vx = (ew - 1) * k * (-1 if ew_dir else 1)
        vy = (ns - 1) * k * (-1 if ns_dir else 1)
        gs = math.hypot(vx, vy)
        trk = math.degrees(math.atan2(vx, vy)) % 360
        return gs, trk, rate
    if st in (3, 4):                                  # airspeed + heading
        if not _bits(msg, 46, 46):
            return None
        hdg = _bits(msg, 47, 56) * 360 / 1024
        spd = _bits(msg, 58, 67)
        return (None if spd == 0 else (spd - 1) * (4 if st == 4 else 1)), hdg, rate
    return None


def squawk(msg: bytes):
    """Mode A code from an emergency/priority status message (TC 28/1)."""
    if typecode(msg) != 28 or _bits(msg, 38, 40) != 1:
        return None
    c = _bits(msg, 44, 56)                            # 13-bit identity field
    bit = lambda k: (c >> (12 - k)) & 1               # noqa: E731
    # field order: C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4
    a = bit(1) | bit(3) << 1 | bit(5) << 2
    b = bit(7) | bit(9) << 1 | bit(11) << 2
    cc = bit(0) | bit(2) << 1 | bit(4) << 2
    d = bit(8) | bit(10) << 1 | bit(12) << 2
    return f"{a}{b}{cc}{d}"


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------
class Tracker:
    """Aircraft state per ICAO address."""

    MAX_RANGE_KM = 400
    TRAIL = 120                                       # positions kept per aircraft

    def __init__(self, home_lat: float, home_lon: float):
        self.home = (home_lat, home_lon)
        self.planes = {}

    def update(self, msg: bytes, t: float):
        a = icao(msg)
        p = self.planes.get(a)
        if p is None:
            p = self.planes[a] = {"icao": a, "callsign": "", "lat": None, "lon": None,
                                  "alt": None, "gs": None, "track": None, "vrate": None,
                                  "squawk": None, "rotor": False, "msgs": 0,
                                  "first": t, "last": t, "pos_t": None, "trail": []}
        p["msgs"] += 1
        p["last"] = t
        tc = typecode(msg)
        if 1 <= tc <= 4:
            p["callsign"] = callsign(msg) or p["callsign"]
            p["rotor"] = (tc == 4 and _bits(msg, 38, 40) == 7)
        elif 9 <= tc <= 18 or 20 <= tc <= 22:
            alt = altitude(msg)
            if alt is not None:
                p["alt"] = alt
            f, la, lo = cpr(msg)
            ref = (p["lat"], p["lon"]) if (p["lat"] is not None and p["pos_t"] is not None
                                           and t - p["pos_t"] < 60) else self.home
            lat, lon = cpr_local(f, la, lo, *ref)
            if distance_km(*self.home, lat, lon) <= self.MAX_RANGE_KM:
                if p["lat"] is not None and t - (p["pos_t"] or 0) < 10:
                    # reject jumps a jet couldn't make (bad local decode)
                    if distance_km(p["lat"], p["lon"], lat, lon) > 5 + 0.4 * (t - p["pos_t"]):
                        return p
                p["lat"], p["lon"], p["pos_t"] = lat, lon, t
                p["trail"].append((round(lat, 5), round(lon, 5)))
                del p["trail"][:-self.TRAIL]
        elif tc == 19:
            v = velocity(msg)
            if v:
                p["gs"], p["track"], p["vrate"] = v
        elif tc == 28:
            sq = squawk(msg)
            if sq:
                p["squawk"] = sq
        return p

    def expire(self, t: float, after_s: float = 90.0):
        for a in [a for a, p in self.planes.items() if t - p["last"] > after_s]:
            del self.planes[a]

    def distance(self, p):
        if p["lat"] is None:
            return None
        return distance_km(*self.home, p["lat"], p["lon"])

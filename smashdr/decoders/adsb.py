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
    """Mode S CRC of data (= the parity field for data without its last 3 bytes);
    0 for a whole valid DF17/18 message."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1000000 | CRC_POLY
    return crc & 0xFFFFFF


def _crc_rem_batch(bits: np.ndarray, nbits: int) -> np.ndarray:
    """bits: (n, >=nbits) -> int64 (n,) 24-bit CRC remainder over the first nbits."""
    reg = bits[:, :nbits].astype(np.uint8).copy()
    gen = np.array([(0x1FFF409 >> (24 - i)) & 1 for i in range(25)], dtype=np.uint8)
    for i in range(nbits - 24):
        lead = reg[:, i].astype(bool)
        if lead.any():
            reg[lead, i:i + 25] ^= gen
    weights = (1 << np.arange(23, -1, -1)).astype(np.int64)
    return reg[:, nbits - 24:nbits].astype(np.int64) @ weights


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
        self.clock = 0                        # samples processed
        self.known = {}                       # ICAO int -> clock last seen (DF11/17/18)

    def process(self, iq: np.ndarray):
        self.clock += len(iq)
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
        found = []                                    # (start, nbits, bits)
        # 1) self-checking messages: DF17/18 (CRC 0), DF11 all-call (CRC = II/SI code)
        sel = np.flatnonzero((df == 17) | (df == 18))
        if len(sel):
            ok = _crc_rem_batch(bits[sel], 112) == 0
            for k in sel[ok]:
                found.append((q[k], 112, bits[k]))
        sel = np.flatnonzero(df == 11)
        if len(sel):
            rem = _crc_rem_batch(bits[sel], 56)
            for k in sel[(rem & 0xFFFF80) == 0]:
                found.append((q[k], 56, bits[k]))
        for _start, nb, b in found:
            a = int.from_bytes(np.packbits(b[8:32]).tobytes(), "big")
            self.known[a] = self.clock
        # 2) address/parity replies (DF4/5 short, DF20/21 long): CRC remainder = ICAO
        #    address; accepted only for aircraft heard via DF11/17 in the last minute
        horizon = self.clock - 60 * RATE
        for dfs, nb in (((4, 5), 56), ((20, 21), 112)):
            sel = np.flatnonzero(np.isin(df, dfs))
            if len(sel):
                rem = _crc_rem_batch(bits[sel], nb)
                for k, r in zip(sel, rem):
                    seen = self.known.get(int(r))
                    if seen is not None and seen >= horizon:
                        found.append((q[k], nb, bits[k]))
        if len(self.known) > 5000:
            self.known = {a: t for a, t in self.known.items() if t >= horizon}
        found.sort(key=lambda f: f[0])
        out, last_end = [], -1
        for start, nb, b in found:
            if start < last_end:                      # same message seen at an offset
                continue
            last_end = start + PREAMBLE + 2 * nb
            out.append(np.packbits(b[:nb]).tobytes())
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


def df_of(msg: bytes) -> int:
    return msg[0] >> 3


def icao(msg: bytes) -> str:
    """Address: in the message for DF11/17/18, the CRC remainder for DF4/5/20/21."""
    if df_of(msg) in (4, 5, 20, 21):
        return f"{crc24(msg[:-3]) ^ int.from_bytes(msg[-3:], 'big'):06X}"
    return msg[1:4].hex().upper()


def ac13_altitude(msg: bytes):
    """DF4/20 altitude (25 ft steps only; Gillham / metric skipped)."""
    ac = _bits(msg, 20, 32)
    if ac == 0 or (ac >> 6) & 1 or not (ac >> 4) & 1:
        return None
    n = ((ac & 0x1F80) >> 2) | ((ac & 0x0020) >> 1) | (ac & 0x000F)
    return n * 25 - 1000


def _id13(c: int) -> str:
    bit = lambda k: (c >> (12 - k)) & 1               # noqa: E731
    # field order: C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4
    a = bit(1) | bit(3) << 1 | bit(5) << 2
    b = bit(7) | bit(9) << 1 | bit(11) << 2
    cc = bit(0) | bit(2) << 1 | bit(4) << 2
    d = bit(8) | bit(10) << 1 | bit(12) << 2
    return f"{a}{b}{cc}{d}"


def id13_squawk(msg: bytes) -> str:
    """DF5/21 identity (squawk)."""
    return _id13(_bits(msg, 20, 32))


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
    return _id13(_bits(msg, 44, 56))               # 13-bit identity field


# --------------------------------------------------------------------------
# Comm-B (DF20/21 MB field): the radar asked for a register; the reply doesn't
# say which, so it is inferred from the content (like pyModeS). Kept: BDS 2,0
# callsign, 4,0 selected altitude / baro, 5,0 track & turn, 6,0 heading & speed.
# --------------------------------------------------------------------------
_AIS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"
MAG_DECL = 11.5                       # LA magnetic declination (deg E): BDS 6,0 is magnetic


def _mb(msg: bytes) -> int:
    return int.from_bytes(msg[4:11], "big")


def _f(mb: int, a: int, b: int) -> int:
    """Bits a..b of the 56-bit MB, 1-indexed (as in the ICAO tables)."""
    return (mb >> (56 - b)) & ((1 << (b - a + 1)) - 1)


def _status_ok(mb, st, a, b):
    """A field whose status bit is 0 must be all zero."""
    return _f(mb, st, st) or _f(mb, a, b) == 0


def _signed(sign, v, n):
    return v - (1 << n) if sign else v


def _angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def bds20(mb):
    if mb >> 48 != 0x20:
        return None
    cs = "".join(_AIS[_f(mb, 9 + 6 * i, 14 + 6 * i)] for i in range(8))
    if "#" in cs or not cs.strip("_ "):
        return None
    return {"callsign": cs.replace("_", " ").strip()}


def bds40(mb):
    if mb == 0 or _f(mb, 40, 47) or _f(mb, 52, 53):
        return None
    for st, a, b in ((1, 2, 13), (14, 15, 26), (27, 28, 39), (48, 49, 51), (54, 55, 56)):
        if not _status_ok(mb, st, a, b):
            return None
    out = {}
    for st, a, b, key in ((1, 2, 13, "sel_alt"), (14, 15, 26, "fms_alt")):
        if _f(mb, st, st):
            alt = _f(mb, a, b) * 16
            if alt > 50_000 or abs(alt - round(alt / 100) * 100) > 16:
                return None                        # selected altitudes are round numbers
            out[key] = int(round(alt / 100) * 100)
    if _f(mb, 27, 27):
        baro = _f(mb, 28, 39) * 0.1 + 800
        if not 900 <= baro <= 1100:
            return None
        out["baro"] = round(baro, 1)
    if _f(mb, 48, 48):
        out["ap_modes"] = [n for bit, n in ((49, "VNAV"), (50, "ALT HOLD"), (51, "APPROACH"))
                           if _f(mb, bit, bit)]
    return out if ("sel_alt" in out or "fms_alt" in out) else None


def bds50(mb):
    if mb == 0:
        return None
    for st, a, b in ((1, 2, 11), (12, 13, 23), (24, 25, 34), (35, 36, 45), (46, 47, 56)):
        if not _status_ok(mb, st, a, b):
            return None
    if not (_f(mb, 12, 12) and _f(mb, 24, 24)):
        return None
    out = {"track": _signed(_f(mb, 13, 13), _f(mb, 14, 23), 10) * 90 / 512 % 360,
           "gs": _f(mb, 25, 34) * 2}
    if _f(mb, 1, 1):
        out["roll"] = _signed(_f(mb, 2, 2), _f(mb, 3, 11), 9) * 45 / 256
        if abs(out["roll"]) > 35:
            return None
    if _f(mb, 35, 35):
        out["turn_rate"] = _signed(_f(mb, 36, 36), _f(mb, 37, 45), 9) * 8 / 256
    if _f(mb, 46, 46):
        out["tas"] = _f(mb, 47, 56) * 2
        if out["tas"] > 600 or abs(out["tas"] - out["gs"]) > 200:
            return None
    if out["gs"] > 600:
        return None
    return out


def bds60(mb):
    if mb == 0:
        return None
    for st, a, b in ((1, 2, 12), (13, 14, 23), (24, 25, 34), (35, 36, 45), (46, 47, 56)):
        if not _status_ok(mb, st, a, b):
            return None
    if not _f(mb, 1, 1) or not (_f(mb, 13, 13) or _f(mb, 24, 24)):
        return None
    out = {"heading": _signed(_f(mb, 2, 2), _f(mb, 3, 12), 10) * 90 / 512 % 360}
    if _f(mb, 13, 13):
        out["ias"] = _f(mb, 14, 23)
        if not 0 < out["ias"] <= 500:
            return None
    if _f(mb, 24, 24):
        out["mach"] = _f(mb, 25, 34) * 2.048 / 512
        if not 0 < out["mach"] <= 1:
            return None
    if "ias" in out and "mach" in out and not 250 <= out["ias"] / out["mach"] <= 700:
        return None
    for st, sg, a, b, key in ((35, 36, 37, 45, "vr_baro"), (46, 47, 48, 56, "vr_ins")):
        if _f(mb, st, st):
            out[key] = _signed(_f(mb, sg, sg), _f(mb, a, b), 9) * 32
            if abs(out[key]) > 6000:
                return None
    return out


def commb(msg: bytes, ref: dict = None):
    """(bds, fields) for a DF20/21 reply, or None when empty / ambiguous.
    ref: the plane's state (ADS-B gs/track, earlier Comm-B) to pick between
    registers that fit the same bits."""
    mb = _mb(msg)
    if mb == 0:
        return None
    r = bds20(mb)
    if r:
        return "2,0", r
    cand = {k: v for k, v in (("4,0", bds40(mb)), ("5,0", bds50(mb)), ("6,0", bds60(mb))) if v}
    if not cand:
        return None
    ref = ref or {}
    gs, trk = ref.get("gs"), ref.get("track")
    if gs is None:                       # no ADS-B: fall back to earlier Comm-B track/speed
        e = ref.get("ehs") or {}
        gs, trk = e.get("gs"), e.get("track")
    alt = ref.get("alt")
    if "5,0" in cand and gs is not None and trk is not None:
        c = cand["5,0"]
        if abs(c["gs"] - gs) > 40 or _angdiff(c["track"], trk) > 20:
            del cand["5,0"]
    if "6,0" in cand and trk is not None:
        c = cand["6,0"]
        if _angdiff(c["heading"] + MAG_DECL, trk) > 45:
            del cand["6,0"]
    if "6,0" in cand and gs is not None and "ias" in cand["6,0"]:
        if cand["6,0"]["ias"] > gs + 150 or (alt is not None and alt < 10_000
                                             and abs(cand["6,0"]["ias"] - gs) > 120):
            del cand["6,0"]
    if len(cand) > 1 and trk is None:
        return None                      # can't tell them apart yet
    if len(cand) != 1:
        return None
    return next(iter(cand.items()))


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
                                  "squawk": None, "ehs": {}, "rotor": False, "hover": False, "msgs": 0,
                                  "adsb": False, "emergency_state": 0, "squawk_since": None,
                                  "first": t, "last": t, "pos_t": None, "trail": []}
        p["msgs"] += 1
        p["last"] = t
        df = df_of(msg)
        if df in (4, 20):
            alt = ac13_altitude(msg)
            if alt is not None and not p["adsb"]:
                p["alt"] = alt
            if df == 20:
                self._commb(p, msg, t)
            return p
        if df in (5, 21):
            sq = id13_squawk(msg)
            if sq != p["squawk"]:
                p["squawk_since"] = t
            p["squawk"] = sq
            if df == 21:
                self._commb(p, msg, t)
            return p
        if df == 11:
            return p
        p["adsb"] = True
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
                # hovering / very slow while clearly airborne: a helicopter
                if (p["gs"] is not None and p["gs"] < 35 and p["alt"] is not None
                        and p["alt"] > 400):
                    p["hover"] = True
        elif tc == 28:
            sq = squawk(msg)
            if sq:
                if sq != p["squawk"]:
                    p["squawk_since"] = t
                p["squawk"] = sq
            if _bits(msg, 38, 40) == 1:
                p["emergency_state"] = _bits(msg, 41, 43)      # 0 none, 1-6 kind
        return p

    def _commb(self, p, msg, t):
        r = commb(msg, p)
        if r is None:
            return
        bds, f = r
        if bds == "2,0" and not p["callsign"]:
            p["callsign"] = f["callsign"]
        ehs = dict(p["ehs"])                  # replaced, never mutated (UI holds copies)
        ehs.update(f)
        ehs["t_" + bds] = t
        p["ehs"] = ehs
        if bds == "5,0" and not p["adsb"]:    # transponder-only: speed / track from radar data
            p["gs"], p["track"] = f["gs"], f["track"]

    def expire(self, t: float, after_s: float = 90.0):
        for a in [a for a, p in self.planes.items() if t - p["last"] > after_s]:
            del self.planes[a]

    def distance(self, p):
        if p["lat"] is None:
            return None
        return distance_km(*self.home, p["lat"], p["lon"])

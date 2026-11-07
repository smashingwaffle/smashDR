"""
ais.py - AIS ship receiver: 161.975 / 162.025 MHz, 9600 bit/s GMSK, NRZI, HDLC.

    rx = AisRx(rate, center_hz)        # both channels must be inside the SDR span
    msgs = rx.process(iq)              # -> [{"type": 1, "mmsi": ..., "lat": ..., ...}, ...]
    tracker = ShipTracker(); tracker.update(msg, t)

Chain per channel: channelizer to 48 kHz (5 samples/bit), FM discriminator,
1-bit smoothing, DC removal, slice at all 5 sample phases (no clock loop:
an AIS burst is only ~26 ms), NRZI (no transition = 1), HDLC flags 0x7E,
bit de-stuffing, CRC-16/X.25, LSB-first bytes. Messages 1/2/3, 5, 18, 19,
21 and 24 are parsed; the rest are passed on as {"type": n, "mmsi": ...}.
Tested with a synthetic GMSK generator (encode_frame + modulate in tests).
"""

import time

import numpy as np

from smashdr.core.dsp import Channelizer

CHANNELS = (161_975_000, 162_025_000)      # AIS 1 (87B), AIS 2 (88B)
BAUD = 9600
FS = 48_000
SPS = FS // BAUD                            # 5
HALF_BW = 9_000
KEEP = 6_000                                # samples kept between blocks (> longest frame)
SIXBIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
FLAG = np.array([0, 1, 1, 1, 1, 1, 1, 0], dtype=np.uint8)

SHIP_TYPES = [(20, 29, "Wing in ground"), (30, 30, "Fishing"), (31, 32, "Towing"), (33, 33, "Dredging"),
              (34, 34, "Diving"), (35, 35, "Military"), (36, 36, "Sailing"), (37, 37, "Pleasure craft"),
              (40, 49, "High-speed craft"), (50, 50, "Pilot"), (51, 51, "Search and rescue"),
              (52, 52, "Tug"), (53, 53, "Port tender"), (54, 54, "Anti-pollution"),
              (55, 55, "Law enforcement"), (58, 58, "Medical"), (60, 69, "Passenger"),
              (70, 79, "Cargo"), (80, 89, "Tanker"), (90, 99, "Other")]
NAV_STATUS = ["under way (engine)", "at anchor", "not under command", "restricted manoeuvrability",
              "constrained by draught", "moored", "aground", "fishing", "under way (sailing)",
              "", "", "", "", "", "AIS-SART", ""]


def ship_type_name(code):
    if code is None:
        return ""
    return next((n for a, b, n in SHIP_TYPES if a <= code <= b), "")


def ship_category(code):
    """Colour group for the map."""
    if code is None:
        return "unknown"
    if 70 <= code <= 79:
        return "cargo"
    if 80 <= code <= 89:
        return "tanker"
    if 60 <= code <= 69:
        return "passenger"
    if code in (31, 32, 52, 50, 53):
        return "tug"
    if code in (36, 37):
        return "pleasure"
    if code in (30,):
        return "fishing"
    if code in (35, 51, 55, 58):
        return "special"
    return "other"


# ---- CRC / bits ------------------------------------------------------------------
def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for x in data:
        crc ^= x
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


def _destuff(bits: np.ndarray):
    """Remove the 0 after every run of five 1s; None if a run of six 1s (abort)."""
    if len(bits) >= 6 and np.any(np.convolve(bits, np.ones(6, dtype=np.int32), "valid") == 6):
        return None
    if len(bits) < 6:
        return bits
    run5 = np.convolve(bits, np.ones(5, dtype=np.int32), "valid") == 5
    drop = np.flatnonzero(run5) + 5
    drop = drop[drop < len(bits)]
    return np.delete(bits, drop)


def _frame_to_payload(bits: np.ndarray):
    """De-stuffed frame bits (LSB-first bytes, + CRC) -> payload bit array (MSB-first) or None."""
    n = len(bits)
    if n < 72 or n % 8:
        return None
    by = np.packbits(bits.reshape(-1, 8)[:, ::-1], axis=1).ravel().tobytes()
    data, fcs = by[:-2], by[-2] | (by[-1] << 8)
    if crc16_x25(data) != fcs:
        return None
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


# ---- message parsing -----------------------------------------------------------------
def _u(b, s, n):
    v = 0
    for x in b[s:s + n]:
        v = (v << 1) | int(x)
    return v


def _i(b, s, n):
    v = _u(b, s, n)
    return v - (1 << n) if v >> (n - 1) else v


def _t(b, s, n):
    chars = "".join(SIXBIT[_u(b, s + 6 * k, 6)] for k in range(n // 6) if s + 6 * k + 6 <= len(b))
    return chars.split("@")[0].strip()


def _pos(b, lon_s, lat_s):
    lon = _i(b, lon_s, 28) / 600000.0
    lat = _i(b, lat_s, 27) / 600000.0
    if abs(lon) > 180 or abs(lat) > 90:
        return None, None
    return lat, lon


def parse(b: np.ndarray) -> dict:
    if len(b) < 38:
        return {}
    t = _u(b, 0, 6)
    m = {"type": t, "mmsi": _u(b, 8, 30)}
    try:
        if t in (1, 2, 3) and len(b) >= 137:
            sog = _u(b, 50, 10)
            cog = _u(b, 116, 12)
            hdg = _u(b, 128, 9)
            lat, lon = _pos(b, 61, 89)
            m.update(status=_u(b, 38, 4), sog=None if sog == 1023 else sog / 10,
                     lat=lat, lon=lon, cog=None if cog >= 3600 else cog / 10,
                     heading=None if hdg == 511 else hdg)
        elif t == 5 and len(b) >= 420:
            m.update(imo=_u(b, 40, 30) or None, callsign=_t(b, 70, 42), name=_t(b, 112, 120),
                     shiptype=_u(b, 232, 8), length=_u(b, 240, 9) + _u(b, 249, 9),
                     beam=_u(b, 258, 6) + _u(b, 264, 6),
                     eta=(_u(b, 274, 4), _u(b, 278, 5), _u(b, 283, 5), _u(b, 288, 6)),
                     draught=_u(b, 294, 8) / 10, destination=_t(b, 302, 120))
        elif t in (18, 19) and len(b) >= 133:
            sog = _u(b, 46, 10)
            cog = _u(b, 112, 12)
            hdg = _u(b, 124, 9)
            lat, lon = _pos(b, 57, 85)
            m.update(sog=None if sog == 1023 else sog / 10, lat=lat, lon=lon,
                     cog=None if cog >= 3600 else cog / 10, heading=None if hdg == 511 else hdg)
            if t == 19 and len(b) >= 271:
                m.update(name=_t(b, 143, 120), shiptype=_u(b, 263, 8))
        elif t == 21 and len(b) >= 219:
            lat, lon = _pos(b, 164, 192)
            m.update(name=_t(b, 43, 120), aid_type=_u(b, 38, 5), lat=lat, lon=lon)
        elif t == 24 and len(b) >= 160:
            if _u(b, 38, 2) == 0:
                m.update(name=_t(b, 40, 120))
            elif len(b) >= 132:
                m.update(shiptype=_u(b, 40, 8), callsign=_t(b, 90, 42))
    except (IndexError, ValueError):
        pass
    return m


# ---- receiver ---------------------------------------------------------------------
class _Chan:
    def __init__(self, rate, offset):
        self.ch = Channelizer(rate, FS, HALF_BW, offset)
        self.prev = np.complex64(0)
        self.buf = np.zeros(0, dtype=np.float32)

    def push(self, iq):
        y = self.ch.process(iq)
        if not len(y):
            return
        z = np.concatenate([[self.prev], y])
        self.prev = y[-1]
        fm = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32)
        self.buf = np.concatenate([self.buf, fm])


class AisRx:
    def __init__(self, rate, center_hz):
        self.rate = float(rate)
        self.center = float(center_hz)
        self.chans = [_Chan(rate, f - center_hz) for f in CHANNELS]
        self._seen = {}                                   # payload bytes -> time (dedupe)

    @staticmethod
    def covers(rate, center_hz):
        return all(abs(f - center_hz) < rate / 2 - 15_000 for f in CHANNELS)

    def process(self, iq):
        out = []
        now = time.time()
        for c in self.chans:
            c.push(iq)
            if len(c.buf) < 2 * KEEP:
                continue
            out += self._decode(c.buf, now)
            c.buf = c.buf[-KEEP:]
        if len(self._seen) > 500:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 5}
        return out

    def _decode(self, fm, now):
        x = np.convolve(fm, np.ones(SPS, dtype=np.float32) / SPS, "same")
        dc = np.convolve(x, np.ones(24 * SPS, dtype=np.float32) / (24 * SPS), "same")
        x = x - dc
        out = []
        for ph in range(SPS):
            s = (x[ph::SPS] > 0).astype(np.uint8)
            if len(s) < 64:
                continue
            nrzi = (s[1:] == s[:-1]).astype(np.uint8)       # no transition = 1
            w = np.lib.stride_tricks.sliding_window_view(nrzi, 8)
            flags = np.flatnonzero((w == FLAG).all(1))
            for a, b in zip(flags[:-1], flags[1:]):
                if not 72 <= b - (a + 8) <= 1100:
                    continue
                fb = _destuff(nrzi[a + 8:b])
                if fb is None:
                    continue
                pay = _frame_to_payload(fb)
                if pay is None:
                    continue
                key = pay.tobytes()
                if now - self._seen.get(key, 0) < 3:
                    continue
                self._seen[key] = now
                m = parse(pay)
                if m:
                    out.append(m)
        return out


# ---- ships --------------------------------------------------------------------------
MID = {**{k: "us" for k in (338, 366, 367, 368, 369, 303)}, 316: "ca", 345: "mx",
       **{k: "pa" for k in (351, 352, 353, 354, 355, 356, 357, 370, 371, 372, 373, 374)},
       636: "lr", 637: "lr", 538: "mh", **{k: "mt" for k in (215, 229, 248, 249, 256)},
       **{k: "cy" for k in (209, 210, 212)}, 308: "bs", 309: "bs", 311: "bs",
       **{k: "sg" for k in (563, 564, 565, 566)}, 477: "hk", 412: "cn", 413: "cn", 414: "cn",
       431: "jp", 432: "jp", 440: "kr", 441: "kr", 416: "tw", 533: "my", 525: "id", 548: "ph",
       574: "vn", 419: "in", **{k: "gb" for k in (232, 233, 234, 235)}, 211: "de", 218: "de",
       244: "nl", 245: "nl", 246: "nl", 226: "fr", 227: "fr", 228: "fr", 247: "it",
       224: "es", 225: "es", **{k: "gr" for k in (237, 239, 240, 241)}, 257: "no", 258: "no",
       259: "no", 219: "dk", 220: "dk", 265: "se", 266: "se", 273: "ru", 271: "tr", 503: "au",
       512: "nz", 710: "br", 725: "cl", 760: "pe", 730: "co", 470: "ae", 403: "sa", 205: "be",
       230: "fi", 261: "pl", 269: "ch", 305: "ag", 319: "ky", 375: "vc", 376: "vc", 377: "vc"}


def flag_of(mmsi):
    """ISO country code from the MMSI's Maritime Identification Digits (ships only)."""
    s = f"{mmsi:09d}"
    if s[0] in "234567":
        return MID.get(int(s[:3]), "")
    return ""


class ShipTracker:
    def __init__(self, expire_s=1800):
        self.ships = {}
        self.expire_s = expire_s

    def update(self, m, t=None):
        t = time.time() if t is None else t
        mmsi = m.get("mmsi")
        if not mmsi:
            return None
        s = self.ships.setdefault(mmsi, {"mmsi": mmsi, "name": "", "callsign": "", "shiptype": None,
                                         "destination": "", "lat": None, "lon": None, "sog": None,
                                         "cog": None, "heading": None, "status": None, "length": None,
                                         "beam": None, "draught": None, "eta": None, "imo": None,
                                         "trail": [], "seen": t, "msgs": 0, "aton": False,
                                         "flag": flag_of(mmsi)})
        s["seen"] = t
        s["msgs"] += 1
        if m["type"] == 21:
            s["aton"] = True
        for k in ("name", "callsign", "destination"):
            if m.get(k):
                s[k] = m[k]
        for k in ("shiptype", "sog", "cog", "heading", "status", "length", "beam", "draught",
                  "eta", "imo"):
            if m.get(k) is not None:
                s[k] = m[k]
        if m.get("lat") is not None and m.get("lon") is not None:
            if not s["trail"] or (abs(s["trail"][-1][0] - m["lat"]) + abs(s["trail"][-1][1] - m["lon"]) > 2e-4):
                s["trail"] = (s["trail"] + [[round(m["lat"], 5), round(m["lon"], 5)]])[-60:]
            s["lat"], s["lon"] = m["lat"], m["lon"]
        return s

    def expire(self, t=None):
        t = time.time() if t is None else t
        for k in [k for k, s in self.ships.items() if t - s["seen"] > self.expire_s]:
            del self.ships[k]


# ---- test signal (tests only) ----------------------------------------------------------
def encode_frame(payload_bits):
    """Payload bits -> NRZI-coded AIS burst bits (preamble, flags, stuffed data + CRC)."""
    pb = np.asarray(payload_bits, dtype=np.uint8)
    pad = (-len(pb)) % 8
    pb = np.concatenate([pb, np.zeros(pad, np.uint8)])
    data = np.packbits(pb).tobytes()
    fcs = crc16_x25(data)
    by = data + bytes([fcs & 255, fcs >> 8])
    raw = np.unpackbits(np.frombuffer(by, np.uint8)).reshape(-1, 8)[:, ::-1].ravel()
    stuffed, run = [], 0
    for x in raw:
        stuffed.append(int(x))
        run = run + 1 if x else 0
        if run == 5:
            stuffed.append(0)
            run = 0
    bits = [0, 1] * 12 + list(FLAG) + stuffed + list(FLAG) + [0] * 8
    out, level = [], 1
    for x in bits:
        if x == 0:
            level ^= 1
        out.append(level)
    return np.array(out, dtype=np.uint8)

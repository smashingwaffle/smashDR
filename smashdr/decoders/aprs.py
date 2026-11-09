"""
aprs.py - 1200 baud AFSK (Bell 202) APRS packet receiver straight from IQ.

    rx = AprsRx(rate, center_hz)          # watches every APRS channel inside the span
    pkts = rx.process(iq)                 # -> [{"src": "KO6IKZ-9", "text": "KO6IKZ-9>APK004,...", ...}]

Channels: 145.825 (ISS / satellites) and 144.390 (North American APRS). Each
channel is fixed at its nominal frequency (ISS Doppler is at most ±3.5 kHz,
well inside the 15 kHz channel; the tones are relative, so it doesn't matter).

Chain: channelizer -> 48 kHz, FM discriminator, mark/space correlators (one
bit long), three twist weightings (de-emphasised / flat / pre-emphasised
transmitters), sign -> run-length clock recovery (bit stuffing guarantees a
transition at least every 6 bits, so each run is round(len / 40) bits), NRZI,
HDLC flags, de-stuff, CRC-16/X.25, AX.25 UI frame -> TNC2 text + APRS parse
(uncompressed / compressed / Mic-E positions, messages, status).
"""

import time

import numpy as np

from smashdr.core.dsp import Channelizer
from smashdr.decoders.ais import FLAG, _destuff, crc16_x25

CHANNELS = {145_825_000: "ISS", 144_390_000: "APRS"}
FS = 48_000
SPB = FS // 1200                    # 40 samples per bit
HALF_BW = 8_000
WINDOW = 4 * FS                     # decode the last 4 s every time (longest frame ~2.8 s)
STEP = FS                           # ... once a second
TWISTS = (0.5, 1.0, 2.0)


# ---- AX.25 ----------------------------------------------------------------------------
def _call(b):
    c = "".join(chr(x >> 1) for x in b[:6]).strip()
    ssid = (b[6] >> 1) & 15
    return f"{c}-{ssid}" if ssid else c, bool(b[6] & 0x80), bool(b[6] & 1)


def parse_ax25(frame: bytes):
    """AX.25 frame (no FCS) -> dict, or None if it isn't a UI frame we understand."""
    if len(frame) < 16:
        return None
    calls, i = [], 0
    while i + 7 <= len(frame):
        c, h, last = _call(frame[i:i + 7])
        calls.append((c, h))
        i += 7
        if last:
            break
    else:
        return None
    if len(calls) < 2 or len(calls) > 10 or i + 2 > len(frame):
        return None
    if frame[i] != 0x03 or frame[i + 1] != 0xF0:          # UI frame, no layer 3
        return None
    if not all(c and all(ch.isalnum() or ch == "-" for ch in c) for c, _h in calls):
        return None
    dst, src = calls[0][0], calls[1][0]
    digis = calls[2:]
    info = frame[i + 2:].decode("latin-1", "replace")
    path = ",".join(c + ("*" if h else "") for c, h in digis)
    p = {"src": src, "dst": dst, "path": path, "info": info,
         "text": f"{src}>{dst}{',' + path if path else ''}:{info}",
         "via_iss": any(c.split("-")[0] in ("RS0ISS", "NA1SS", "ARISS") and h for c, h in digis)
                    or src.split("-")[0] == "RS0ISS"}
    p.update(parse_aprs(info, dst))
    return p


# ---- APRS info field ---------------------------------------------------------------------
def _uncompressed(s):
    """'4903.50N/07201.75W-' -> lat, lon, symbol table, symbol code."""
    if len(s) < 19 or s[7] not in "NnSs" or s[17] not in "EeWw":
        return None
    try:
        la = s[:7].replace(" ", "0")
        lo = s[9:17].replace(" ", "0")
        lat = int(la[:2]) + float(la[2:7]) / 60
        lon = int(lo[:3]) + float(lo[3:8]) / 60
    except ValueError:
        return None
    lat = -lat if s[7] in "Ss" else lat
    lon = -lon if s[17] in "Ww" else lon
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon, s[8], s[18]


def _compressed(s):
    if len(s) < 10:
        return None
    try:
        y = sum((ord(c) - 33) * 91 ** (3 - k) for k, c in enumerate(s[1:5]))
        x = sum((ord(c) - 33) * 91 ** (3 - k) for k, c in enumerate(s[5:9]))
    except TypeError:
        return None
    if not all(33 <= ord(c) <= 124 for c in s[1:9]):
        return None
    return 90 - y / 380926, -180 + x / 190463, s[0], s[9]


def _mic_e(info, dst):
    d = dst.split("-")[0]
    if len(d) != 6 or len(info) < 9:
        return None
    digits = []
    for c in d:
        if "0" <= c <= "9":
            digits.append(ord(c) - 48)
        elif "A" <= c <= "J":
            digits.append(ord(c) - 65)
        elif "P" <= c <= "Y":
            digits.append(ord(c) - 80)
        elif c in "KLZ":
            digits.append(0)
        else:
            return None
    lat = digits[0] * 10 + digits[1] + (digits[2] * 10 + digits[3] + (digits[4] * 10 + digits[5]) / 100) / 60
    if not "P" <= d[3] <= "Z":
        lat = -lat
    lon = ord(info[1]) - 28 + (100 if "P" <= d[4] <= "Z" else 0)
    if 180 <= lon <= 189:
        lon -= 80
    elif 190 <= lon <= 199:
        lon -= 190
    m = ord(info[2]) - 28
    if m >= 60:
        m -= 60
    lon = lon + (m + (ord(info[3]) - 28) / 100) / 60
    if "P" <= d[5] <= "Z":
        lon = -lon
    sp, dc, se = ord(info[4]) - 28, ord(info[5]) - 28, ord(info[6]) - 28
    speed = sp * 10 + dc // 10
    course = (dc % 10) * 100 + se
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return {"lat": lat, "lon": lon, "sym": info[8] + info[7], "speed_kn": speed, "course": course,
            "comment": info[9:].strip()}


def parse_aprs(info: str, dst: str = "") -> dict:
    out = {"kind": "other"}
    if not info:
        return out
    t = info[0]
    try:
        if t == "}":                                          # third party: parse the inner packet
            inner = info[1:]
            if ":" in inner:
                head, body = inner.split(":", 1)
                d = head.split(">")[1].split(",")[0] if ">" in head else ""
                r = parse_aprs(body, d)
                r["via"] = head
                return r
        if t in "!=/@":
            body = info[1:] if t in "!=" else info[8:]
            if body and not (body[0].isdigit() or body[0] == " "):    # compressed starts with the table
                pos = _compressed(body)
                comment = body[13:]
            else:
                pos = _uncompressed(body)
                comment = body[19:]
            if pos:
                out.update(kind="position", lat=pos[0], lon=pos[1], sym=pos[2] + pos[3],
                           comment=comment.strip())
        elif t in "`'\x1c\x1d":
            r = _mic_e(info, dst)
            if r:
                out.update(kind="position", **r)
        elif t == ":" and len(info) >= 11 and info[10] == ":":
            out.update(kind="message", to=info[1:10].strip(), message=info[11:].strip())
        elif t == ">":
            out.update(kind="status", comment=info[1:].strip())
        elif t == "T":
            out.update(kind="telemetry")
    except (ValueError, IndexError):
        pass
    return out


# ---- demodulator -------------------------------------------------------------------------
def _bits_from_sign(sig):
    """Sign of the mark-space difference -> NRZI levels, one per bit (run-length clock)."""
    s = sig > 0
    edges = np.flatnonzero(s[1:] != s[:-1]) + 1
    if len(edges) < 32:
        return np.zeros(0, np.uint8)
    starts = np.concatenate([[0], edges])
    lens = np.diff(np.concatenate([starts, [len(s)]]))
    n = np.rint(lens / SPB).astype(np.int64)
    keep = n > 0                                   # glitches shorter than half a bit are dropped
    levels = s[starts][keep].astype(np.uint8)
    return np.repeat(levels, n[keep])


def _frames(levels):
    """NRZI levels -> list of CRC-good frame bytes (without FCS)."""
    if len(levels) < 80:
        return []
    bits = (levels[1:] == levels[:-1]).astype(np.uint8)        # no transition = 1
    w = np.lib.stride_tricks.sliding_window_view(bits, 8)
    flags = np.flatnonzero((w == FLAG).all(1))
    out = []
    for a, b in zip(flags[:-1], flags[1:]):
        n = b - (a + 8)
        if not 136 <= n <= 4000:
            continue
        fb = _destuff(bits[a + 8:b])
        if fb is None or len(fb) % 8:
            continue
        by = np.packbits(fb.reshape(-1, 8)[:, ::-1], axis=1).ravel().tobytes()
        data, fcs = by[:-2], by[-2] | (by[-1] << 8)
        if crc16_x25(data) == fcs:
            out.append(data)
    return out


def demod_audio(fm, rate=FS):
    """FM discriminator output (48 kHz) -> list of frame bytes (all twists, de-duplicated)."""
    n = np.arange(len(fm))
    box = np.ones(SPB, dtype=np.float32)
    mark = np.abs(np.convolve(fm * np.exp(-2j * np.pi * 1200 * n / rate), box, "same"))
    space = np.abs(np.convolve(fm * np.exp(-2j * np.pi * 2200 * n / rate), box, "same"))
    seen, out = set(), []
    for g in TWISTS:
        for f in _frames(_bits_from_sign(mark - g * space)):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


class _Chan:
    def __init__(self, rate, offset, hz):
        self.hz = hz
        self.ch = Channelizer(rate, FS, HALF_BW, offset)
        self.prev = np.complex64(0)
        self.buf = np.zeros(0, np.float32)
        self.new = 0

    def push(self, iq):
        y = self.ch.process(iq)
        if not len(y):
            return
        z = np.concatenate([[self.prev], y])
        self.prev = y[-1]
        fm = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32)
        self.buf = np.concatenate([self.buf, fm])[-WINDOW:]
        self.new += len(fm)


class AprsRx:
    def __init__(self, rate, center_hz):
        self.chans = [_Chan(rate, f - center_hz, f) for f in CHANNELS
                      if abs(f - center_hz) < rate / 2 - 15_000]
        self._seen = {}

    @staticmethod
    def covers(rate, center_hz):
        return any(abs(f - center_hz) < rate / 2 - 15_000 for f in CHANNELS)

    def process(self, iq, now=None):
        now = time.time() if now is None else now
        out = []
        for c in self.chans:
            c.push(iq)
            if c.new < STEP:
                continue
            c.new = 0
            for f in demod_audio(c.buf):
                if now - self._seen.get(f, -1e9) < 30:
                    continue
                self._seen[f] = now
                p = parse_ax25(f)
                if p:
                    p.update(freq=c.hz, band=CHANNELS[c.hz], t=now)
                    out.append(p)
        if len(self._seen) > 400:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 30}
        return out

    def flush(self, now=None):
        """Decode what's buffered right away (end of a file)."""
        for c in self.chans:
            c.new = STEP
        return self.process(np.zeros(0, np.complex64), now)


# ---- test signal (tests only) ------------------------------------------------------------
def ax25_frame(src, dst, path, info):
    def enc(call, last, h=False):
        c, _, s = call.partition("-")
        b = bytes((ord(x) << 1) for x in c.ljust(6)[:6])
        return b + bytes([0x60 | ((int(s or 0) & 15) << 1) | (0x80 if h else 0) | (1 if last else 0)])
    calls = [dst, src] + list(path)
    a = b"".join(enc(c.rstrip("*"), i == len(calls) - 1, c.endswith("*")) for i, c in enumerate(calls))
    return a + b"\x03\xf0" + info.encode("latin-1")


def afsk_audio(frame, rate=FS, flags=40):
    """AX.25 frame -> AFSK audio (unit amplitude), NRZI + stuffing + FCS + flags."""
    fcs = crc16_x25(frame)
    raw = np.unpackbits(np.frombuffer(frame + bytes([fcs & 255, fcs >> 8]), np.uint8)
                        ).reshape(-1, 8)[:, ::-1].ravel()
    st, run = [], 0
    for x in raw:
        st.append(int(x))
        run = run + 1 if x else 0
        if run == 5:
            st.append(0)
            run = 0
    bits = list(FLAG) * flags + st + list(FLAG) * 4
    lv, tones = 1, []
    for x in bits:
        if x == 0:
            lv ^= 1
        tones.append(1200 if lv else 2200)
    f = np.repeat(np.array(tones, float), rate // 1200)
    return np.sin(2 * np.pi * np.cumsum(f) / rate)

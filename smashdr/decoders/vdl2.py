"""
vdl2.py - VDL Mode 2 receiver (aircraft datalink, 136 MHz): D8PSK 10 500 sym/s bursts ->
AVLC frames -> ACARS messages, with the aircraft's 24-bit ICAO address.

    rx = Vdl2Rx(rate, center_hz)            # every VDL2 channel inside the span
    for m in rx.process(iq): ...            # {"freq", "src", "dst", "acars": {...}, ...}

Physical layer, as measured on the air (Stefaan's 136.100 recording, 2026-11-09) and in
line with the published burst format:
  * 5 ramp-up symbols + 16-symbol unique word 000 010 011 110 000 001 101 110 001 100 011
    111 101 111 100 010, then the header: 3 reserved bits, 17-bit transmission length
    (LSB first, data bits without FEC), 5 header FEC bits.
  * Symbols are phase changes of k * 45 degrees, k -> bits = Gray(k), first bit = MSB.
  * Everything after the unique word (header included) is scrambled with the
    m-sequence of x^15 + x + 1 (state at the reserved bits = SCRAMBLE_SEED).
  * Data octets are sent LSB first. Reed-Solomon over GF(256) (x^8+x^7+x^2+x+1),
    roots a^120..a^125, 249 data octets per block; the last block gets 6/4/2/0 FEC
    octets for 68+/31-67/3-30/0-2 data octets. Octets are interleaved row by row
    across the blocks (octet i of every block, then i+1 ...), FEC octets the same way.
  * The data is a run of HDLC (AVLC) frames: flags, bit stuffing, CRC-16/X.25.
"""

import re
import time

import numpy as np

from smashdr.core.dsp import Channelizer

CHANNELS = (136_650_000, 136_700_000, 136_725_000, 136_775_000, 136_800_000, 136_825_000,
            136_875_000, 136_900_000, 136_975_000, 136_100_000)
SYM_RATE = 10_500
CH_RATE = 105_000                 # 10 samples per symbol
UNIQUE_WORD = (0, 3, 2, 4, 0, 1, 6, 4, 1, 7, 2, 5, 6, 5, 7, 3)    # as phase steps k (x 45 deg)


# ---- GF(256) Reed-Solomon -------------------------------------------------------------
_PRIM = 0x187
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= _PRIM
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
FCR = 120


def _mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _div(a, b):
    return _EXP[(_LOG[a] - _LOG[b]) % 255] if a else 0


def _poly_eval(p, x):
    """p: coefficients, highest degree first."""
    y = 0
    for c in p:
        y = _mul(y, x) ^ c
    return y


def rs_decode(cw, nroots):
    """Correct a (shortened) codeword in place-order (data then FEC, highest degree first).
    Returns the corrected list or None if uncorrectable."""
    cw = list(cw)
    if nroots == 0:
        return cw
    synd = [_poly_eval(cw, _EXP[FCR + j]) for j in range(nroots)]
    if not any(synd):
        return cw
    # Berlekamp-Massey -> error locator (lowest degree first)
    lam, b = [1], [1]
    L, m = 0, 1
    for n in range(nroots):
        d = synd[n]
        for i in range(1, L + 1):
            if i < len(lam):
                d ^= _mul(lam[i], synd[n - i])
        if d == 0:
            m += 1
            continue
        t = lam[:]
        coef = d
        shifted = [0] * m + b
        lam = [x ^ _mul(coef, y) for x, y in zip(lam + [0] * (len(shifted) - len(lam)),
                                                  shifted + [0] * (len(lam) - len(shifted)))]
        if 2 * L <= n:
            L = n + 1 - L
            b = [_div(x, d) for x in t]
            m = 1
        else:
            m += 1
    lam = lam[:L + 1]
    if L == 0 or L * 2 > nroots:
        return None
    n = len(cw)
    # Chien search over the positions of this (shortened) codeword
    errs = []
    for pos in range(n):                       # pos 0 = first symbol = degree n-1
        deg = n - 1 - pos
        xinv = _EXP[(255 - deg) % 255]
        if _poly_eval(lam[::-1], xinv) == 0:
            errs.append((pos, deg))
    if len(errs) != L:
        return None
    # Forney: omega = synd(x) * lam(x) mod x^nroots   (lowest degree first)
    omega = [0] * nroots
    for i in range(nroots):
        v = 0
        for j in range(min(i, L) + 1):
            v ^= _mul(lam[j], synd[i - j])
        omega[i] = v
    for pos, deg in errs:
        x = _EXP[deg]
        xinv = _EXP[(255 - deg) % 255]
        num = _poly_eval(omega[::-1], xinv)
        den = 0                                    # lam'(xinv): odd terms
        for j in range(1, L + 1, 2):
            den ^= _mul(lam[j], _EXP[(_LOG[xinv] * (j - 1)) % 255] if j > 1 else 1)
        if den == 0:
            return None
        e = _mul(_div(num, den), _EXP[(deg * (1 - FCR)) % 255])
        cw[pos] ^= e
        del x
    if any(_poly_eval(cw, _EXP[FCR + j]) for j in range(nroots)):
        return None
    return cw


def rs_encode(data, nroots):
    """Parity octets for `data` (tests)."""
    g = [1]
    for j in range(nroots):
        r = _EXP[FCR + j]
        g = [a ^ _mul(r, b) for a, b in zip(g + [0], [0] + g)]
    rem = [0] * nroots
    for c in data:
        f = c ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for i in range(nroots):
                rem[i] ^= _mul(f, g[i + 1])
    return rem


def fec_octets(n_last):
    return 0 if n_last <= 2 else 2 if n_last <= 30 else 4 if n_last <= 67 else 6


# ---- scrambler ------------------------------------------------------------------------------
def _mseq():
    s = [1] * 15
    out = np.zeros(32767, np.uint8)
    for i in range(32767):
        out[i] = s[-1]
        fb = s[14] ^ s[0]                      # taps 15 and 1  (x^15 + x + 1)
        s = [fb] + s[:-1]
    return out


_SEQ = _mseq()
SCRAMBLE_START = 10680                         # sequence index at the first header bit (on air)
_SCR = np.concatenate([_SEQ, _SEQ])[SCRAMBLE_START:SCRAMBLE_START + 32767]


# ---- HDLC / AVLC ----------------------------------------------------------------------------
def _crc16_x25(data):
    crc = 0xFFFF
    for x in data:
        crc ^= x
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


def hdlc_frames(bits):
    """Bit array (in transmission order) -> list of CRC-good frames (bytes, FCS removed)."""
    bits = np.asarray(bits, np.uint8)
    out = []
    if len(bits) < 32:
        return out
    w = np.lib.stride_tricks.sliding_window_view(bits, 8)
    flags = np.flatnonzero((w == np.array([0, 1, 1, 1, 1, 1, 1, 0], np.uint8)).all(1))
    for a, b in zip(flags[:-1], flags[1:]):
        seg = bits[a + 8:b]
        if len(seg) < 64:
            continue
        # destuff: drop the 0 after five 1s
        res, run, bad = [], 0, False
        for x in seg:
            if run == 5:
                run = 0
                if x == 0:
                    continue
                bad = True
                break
            res.append(int(x))
            run = run + 1 if x else 0
        if bad or len(res) % 8:
            continue
        by = np.packbits(np.array(res, np.uint8).reshape(-1, 8)[:, ::-1], axis=1).ravel().tobytes()
        if _crc16_x25(by[:-2]) == (by[-2] | (by[-1] << 8)):
            out.append(by[:-2])
    return out


ADDR_TYPES = {1: "aircraft", 4: "ground", 5: "ground", 7: "all"}


def _addr(b4):
    """4 address octets -> (type, 24-bit address, flag bit). Each octet: extension bit first,
    then 7 address bits; read in transmission order the 28 bits are flag, 3-bit type, then
    the 24-bit address MSB first (checked on air: N225WN = A1F595, ground stations type 101)."""
    v = 0
    for x in b4:
        for j in range(1, 8):
            v = (v << 1) | ((x >> j) & 1)
    return (v >> 24) & 7, v & 0xFFFFFF, (v >> 27) & 1


def parse_avlc(fr):
    if len(fr) < 9:
        return None
    dt, dst, cr = _addr(fr[0:4])
    st, src, ag = _addr(fr[4:8])
    ctrl = fr[8]
    m = {"dst_type": ADDR_TYPES.get(dt, f"type {dt}"), "dst": f"{dst:06X}",
         "src_type": ADDR_TYPES.get(st, f"type {st}"), "src": f"{src:06X}",
         "from_air": st == 1, "ctrl": ctrl, "info": fr[9:]}
    if ctrl & 1 == 0:
        m["frame"] = "I"
    elif ctrl & 3 == 1:
        m["frame"] = "S"
    else:
        m["frame"] = {0x03: "UI", 0xAF: "XID", 0xBF: "XID", 0x63: "UA", 0x2F: "SABM", 0x43: "DISC",
                      0x87: "FRMR", 0x13: "UI"}.get(ctrl & 0xEF, f"U {ctrl:02X}")
    info = fr[9:]
    if len(info) >= 3 and info[0] == 0xFF and info[1] == 0xFF and info[2] == 0x01:
        m["acars"] = parse_acars(info[3:])
    return m


def parse_acars(b):
    """ACARS block (after FF FF 01): mode, reg, ack, label, block id, [STX msg no, flight, text] ETX."""
    s = bytes(x & 0x7F for x in b)
    try:
        t = s.decode("ascii", "replace")
    except Exception:          # noqa: BLE001
        return {}
    if len(t) < 13:
        return {}
    a = {"mode": t[0], "reg": t[1:8].replace(".", "").strip(), "ack": t[8], "label": t[9:11],
         "block": t[11]}
    rest = t[12:]
    if rest.startswith("\x02"):
        body = rest[1:]
        end = min([i for i in (body.find("\x03"), body.find("\x17")) if i >= 0] or [len(body)])
        body = body[:end]
        if a["mode"] != "2" or True:
            if len(body) >= 10 and t[1] != " ":
                a["msg_no"], a["flight"], a["text"] = body[:4], body[4:10].strip(), body[10:]
            else:
                a["text"] = body
    return a


# ---- burst decoder ---------------------------------------------------------------------------
def _gray(k):
    return k ^ (k >> 1)


_BITS = np.array([[(_gray(k) >> 2) & 1, (_gray(k) >> 1) & 1, _gray(k) & 1] for k in range(8)], np.uint8)
_LP = None


def _lowpass(fs):
    global _LP
    if _LP is None or _LP[0] != fs:
        n = np.arange(-20, 21)
        h = np.sinc(2 * 7000 / fs * n) * np.hamming(len(n))
        _LP = (fs, (h / h.sum()).astype(np.float32))
    return _LP[1]


def burst_symbols(z, fs, coherent=True):
    """Complex baseband burst at fs -> (phase steps k, quality). Picks timing + frequency itself;
    coherent: decision-directed carrier tracking, then differential decoding of the decisions
    (about 2-3 dB better than plain differential detection on weak bursts)."""
    z = np.convolve(z, _lowpass(fs), "same")
    sps = fs / SYM_RATE
    p = np.abs(z) ** 2
    thr = 0.15 * np.percentile(p, 95)
    on = np.flatnonzero(p > thr)
    if len(on) < 40 * sps:
        return None, 0
    z = z[max(0, on[0] - int(3 * sps)):on[-1] + int(2 * sps)]
    n_sym = int((len(z) - 1) / sps)
    best = None
    t_all = np.arange(n_sym) * sps
    for ph in np.arange(0, sps, sps / 16):
        t = t_all + ph
        t = t[t < len(z) - 1]
        i = t.astype(int)
        f = (t - i).astype(np.float32)
        s = z[i] * (1 - f) + z[i + 1] * f
        d = s[1:] * np.conj(s[:-1])
        d8 = np.mean((d / (np.abs(d) + 1e-9)) ** 8)
        q = abs(d8)
        if best is None or q > best[0]:
            best = (q, s, d, np.angle(d8) / 8)
    q, s, d, off = best
    if not coherent:
        a = np.angle(d * np.exp(-1j * off))
        return np.round(a / (np.pi / 4)).astype(int) % 8, q
    s = s * np.exp(-1j * off * np.arange(len(s)))
    s = s / (np.abs(s) + 1e-9)
    # starting phase from the first 32 symbols (8th power), then a 2nd-order decision-directed loop
    th = np.angle(np.mean(s[2:34] ** 8)) / 8
    fr = 0.0
    m = np.empty(len(s), np.int64)
    step = np.pi / 4
    for n, x in enumerate(s):
        a = np.angle(x) - th
        k = int(np.floor(a / step + 0.5))
        e = a - k * step
        e = (e + np.pi) % (2 * np.pi) - np.pi
        fr += 0.002 * e
        th += 0.08 * e + fr
        m[n] = k
    k = (m[1:] - m[:-1]) % 8
    return k, q


def decode_burst(k):
    """Phase steps -> list of AVLC frames (bytes). Uses the unique word, header, RS and CRC."""
    ks = np.asarray(k)
    uw = np.array(UNIQUE_WORD)
    best, at = 99, -1
    for i in range(0, min(len(ks) - 16, 80)):
        e = int((ks[i:i + 16] != uw).sum())
        if e < best:
            best, at = e, i
    if best > 3:
        return [], "no sync"
    bits = _BITS[ks[at + 16:]].ravel()
    n = len(bits)
    if n < 25 + 64:
        return [], "short"
    bits = bits ^ _SCR[:n]
    L = int(sum(int(b) << j for j, b in enumerate(bits[3:20])))
    nd = (L + 7) // 8
    if nd == 0:
        return [], "empty"
    nb = (nd + 248) // 249
    lens = [249] * (nb - 1) + [nd - 249 * (nb - 1)]
    fl = [6] * (nb - 1) + [fec_octets(lens[-1])]
    total = nd + sum(fl)
    if 25 + 8 * total > n:
        return [], f"length {L} > burst"
    raw = np.packbits(bits[25:25 + 8 * total].reshape(-1, 8)[:, ::-1], axis=1).ravel().tolist()
    blocks = [[] for _ in lens]
    fec = [[] for _ in lens]
    p = 0
    for r in range(max(lens)):
        for b in range(nb):
            if r < lens[b]:
                blocks[b].append(raw[p])
                p += 1
    for r in range(6):
        for b in range(nb):
            if r < fl[b]:
                fec[b].append(raw[p])
                p += 1
    data = []
    fixed = 0
    for b in range(nb):
        cw = rs_decode(blocks[b] + fec[b], fl[b])
        if cw is None:
            cw = blocks[b] + fec[b]                 # uncorrectable: let the CRC decide
        else:
            fixed += sum(x != y for x, y in zip(cw, blocks[b] + fec[b]))
        data += cw[:lens[b]]
    dbits = np.unpackbits(np.array(data, np.uint8).reshape(-1, 1), axis=1)[:, ::-1].ravel()[:L]
    frames = hdlc_frames(dbits)
    return frames, f"L={L} rs_fixed={fixed}"


class _Stage:
    """A 480 kHz slice of the span: per-millisecond channel powers from one FFT per ms (cheap
    burst detector for every channel in the slice), and only the bursts themselves are mixed
    down, filtered and decimated to 96 kHz."""
    RATE = 480_000
    FS = 96_000

    def __init__(self, rate, center_hz, stage_hz, chans):
        self.ch = Channelizer(rate, self.RATE, self.RATE / 2 - 10_000, stage_hz - center_hz)
        self.hz = stage_hz
        self.chans = chans
        self.blk = self.RATE // 1000
        fr = np.fft.fftfreq(self.blk, 1 / self.RATE)
        self.masks = [np.abs(fr - (f - stage_hz)) < 9_000 for f in chans]
        self.buf = np.zeros(0, np.complex64)
        self.base = 0                                  # absolute ms index of buf[0]
        self.lvl = [[] for _ in chans]
        self.floor = [None] * len(chans)
        self.start = [None] * len(chans)
        n = np.arange(-60, 61)
        h = np.sinc(2 * 13_000 / self.RATE * n) * np.hamming(len(n))
        self.h = (h / h.sum()).astype(np.complex64)

    def push(self, iq):
        y = self.ch.process(iq)
        if len(y):
            self.buf = np.concatenate([self.buf, y])
        out = []
        n_ms = len(self.buf) // self.blk
        done = getattr(self, "_done", 0)               # ms already measured
        if n_ms > done:
            seg = self.buf[done * self.blk:n_ms * self.blk].reshape(-1, self.blk)
            P = np.abs(np.fft.fft(seg, axis=1)) ** 2
            for c, m in enumerate(self.masks):
                lv = 10 * np.log10(P[:, m].sum(1) + 1e-12)
                med = float(np.median(lv))
                fl = self.floor[c]
                self.floor[c] = med if fl is None else min(med, 0.98 * fl + 0.02 * med)
                for j, v in enumerate(lv):
                    ms = done + j
                    if v > self.floor[c] + 7:
                        if self.start[c] is None:
                            self.start[c] = ms
                    elif self.start[c] is not None:
                        a = self.start[c]
                        self.start[c] = None
                        if 3 <= ms - a <= 400:
                            out.append((self.chans[c], self._extract(c, a, ms)))
            self._done = n_ms
        # keep the last 450 ms (longest burst + margin), or from the oldest open burst
        open_ = [a for a in self.start if a is not None]
        keep = min([n_ms - 450] + [a - 3 for a in open_]) if open_ else n_ms - 10
        keep = max(0, min(keep, n_ms - 10))
        if keep > 0:
            self.buf = self.buf[keep * self.blk:]
            self._done -= keep
            self.start = [None if a is None else a - keep for a in self.start]
        return out

    def _extract(self, c, a, b):
        a0, b0 = max(0, (a - 3) * self.blk), min(len(self.buf), (b + 3) * self.blk)
        z = self.buf[a0:b0]
        f = self.chans[c] - self.hz
        z = z * np.exp(-2j * np.pi * f / self.RATE * np.arange(a0, b0)).astype(np.complex64)
        z = np.convolve(z, self.h, "same")[::self.RATE // self.FS]
        return z


class _Chan:
    """One VDL2 channel: channelizer -> burst detector."""
    def __init__(self, rate, offset, hz):
        self.hz = hz
        self.fs = next((f for f in (96_000, 100_000, 120_000) if rate % f == 0), None)
        if self.fs is None:
            raise ValueError("unsupported rate")
        self.ch = Channelizer(rate, self.fs, 14_000, offset)
        self.buf = np.zeros(0, np.complex64)
        self.floor = None
        self.blk = int(self.fs / 1000)             # 1 ms

    def push(self, iq):
        y = self.ch.process(iq)
        if len(y):
            self.buf = np.concatenate([self.buf, y])
        out = []
        n = len(self.buf) // self.blk
        if n < 20:
            return out
        p = (np.abs(self.buf[:n * self.blk]) ** 2).reshape(n, self.blk).mean(1)
        lvl = 10 * np.log10(p + 1e-12)
        med = float(np.median(lvl))
        self.floor = med if self.floor is None else min(med, 0.95 * self.floor + 0.05 * med)
        on = lvl > self.floor + 7
        # bursts that have ended inside the buffer
        d = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
        st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        keep_from = n
        for a, b in zip(st, en):
            if b >= n - 2:                        # still going: keep it for next time
                keep_from = min(keep_from, max(0, a - 3))
                break
            if b - a >= 3:
                out.append(self.buf[max(0, a - 2) * self.blk:(b + 2) * self.blk].copy())
        cut = min(keep_from, n - 5) if keep_from < n else n - 5
        if len(self.buf) > 400 * self.blk and keep_from < n and keep_from < n - 300:
            cut = n - 300                         # never hold more than ~0.3 s
        self.buf = self.buf[max(0, cut) * self.blk:]
        return out


STAGES = (136_800_000, 136_100_000)        # 480 kHz slices: 136.56-137.04 and 135.86-136.34


class Vdl2Rx:
    """Each VDL2 channel inside the span is watched through a 480 kHz stage (_Stage)."""
    def __init__(self, rate, center_hz):
        self.stages = []
        used = set()
        if rate % _Stage.RATE == 0:
            for sh in STAGES:
                if abs(sh - center_hz) > rate / 2 - _Stage.RATE / 2:
                    continue
                chans = [f for f in CHANNELS if f not in used and abs(f - sh) < _Stage.RATE / 2 - 20_000
                         and abs(f - center_hz) < rate / 2 - 20_000]
                if chans:
                    self.stages.append(_Stage(rate, center_hz, sh, chans))
                    used.update(chans)
        self.chans = []
        for f in CHANNELS:
            if f not in used and abs(f - center_hz) < rate / 2 - 20_000:
                try:
                    self.chans.append(_Chan(rate, f - center_hz, f))
                except ValueError:
                    pass
        self.stats = {"bursts": 0, "frames": 0}

    @staticmethod
    def covers(rate, center_hz):
        return any(abs(f - center_hz) < rate / 2 - 20_000 for f in CHANNELS)

    def process(self, iq, now=None):
        now = time.time() if now is None else now
        out = []
        bursts = []
        for c in self.chans:
            bursts += [(c.hz, c.fs, z) for z in c.push(iq)]
        for st in self.stages:
            bursts += [(hz, st.FS, z) for hz, z in st.push(iq)]
        for hz, fs, z in bursts:
            self.stats["bursts"] += 1
            k, q = burst_symbols(z, fs)
            if k is None:
                continue
            frames, _info = decode_burst(k)
            for fr in frames:
                m = parse_avlc(fr)
                if m:
                    self.stats["frames"] += 1
                    m.update(freq=hz, t=now, q=round(float(q), 2))
                    out.append(m)
        return out


# ---- plain-language summaries --------------------------------------------------------------
# ACARS flight ids use the 2-character IATA airline code; ADS-B callsigns the 3-letter ICAO one.
IATA_ICAO = {"UA": "UAL", "AA": "AAL", "DL": "DAL", "WN": "SWA", "AS": "ASA", "B6": "JBU", "NK": "NKS",
             "F9": "FFT", "G4": "AAY", "HA": "HAL", "OO": "SKW", "YX": "RPA", "MQ": "ENY", "9E": "EDV",
             "QX": "QXE", "OH": "JIA", "PT": "PDT", "ZW": "AWI", "CZ": "CSN", "CA": "CCA", "MU": "CES",
             "CI": "CAL", "BR": "EVA", "CX": "CPA", "NH": "ANA", "JL": "JAL", "KE": "KAL", "OZ": "AAR",
             "SQ": "SIA", "QF": "QFA", "NZ": "ANZ", "PR": "PAL", "EK": "UAE", "QR": "QTR", "EY": "ETD",
             "TK": "THY", "BA": "BAW", "VS": "VIR", "LH": "DLH", "AF": "AFR", "KL": "KLM", "LX": "SWR",
             "IB": "IBE", "EI": "EIN", "AY": "FIN", "AC": "ACA", "WS": "WJA", "AM": "AMX", "Y4": "VOI",
             "VB": "VIV", "CM": "CMP", "AV": "AVA", "LA": "LAN", "FX": "FDX", "5X": "UPS", "5Y": "GTI",
             "K4": "CKS", "SY": "SCX", "MX": "MXY", "XP": "VXP", "FJ": "FJI", "ET": "ETH", "CK": "CKK",
             "OY": "OAE", "3S": "OAE", "KZ": "NCA", "PO": "PAC", "GB": "ABX", "NC": "NAC"}


def callsign(flight):
    """ACARS 'UA0794' -> 'UAL794' (ADS-B style), '' if unknown."""
    f = (flight or "").strip()
    if len(f) < 3:
        return ""
    code = IATA_ICAO.get(f[:2])
    num = f[2:].lstrip("0") or "0"
    return code + num if code and num.isalnum() else ""


_POS = (re.compile(r"([NS])\s?(\d{2})(\d{2}\.\d)[ ,]*([EW])\s?(\d{3})(\d{2}\.\d)"),          # N3403.4,W11738.7
        re.compile(r"([NS])\s?(\d{2})(\d{3})[ ,]*([EW])\s?(\d{3})(\d{3})"))                  # N34034 W117387


def position(text):
    """First lat/lon in an ACARS text (degrees + decimal minutes), or None."""
    for i, rx in enumerate(_POS):
        m = rx.search(text or "")
        if not m:
            continue
        try:
            lat_m = float(m.group(3)) if i == 0 else int(m.group(3)) / 10
            lon_m = float(m.group(6)) if i == 0 else int(m.group(6)) / 10
            lat = int(m.group(2)) + lat_m / 60
            lon = int(m.group(5)) + lon_m / 60
        except ValueError:
            continue
        if lat_m >= 60 or lon_m >= 60 or lat > 90 or lon > 180:
            continue
        return (-lat if m.group(1) == "S" else lat), (-lon if m.group(4) == "W" else lon)
    return None


LABELS = {
    "_d": "Keep-alive (no content)", "Q0": "Link test", "SQ": "Ground station beacon",
    "5Z": "Airline message", "B9": "ATIS request", "A9": "ATIS", "H1": "Avionics message",
    "C1": "Uplink to the cockpit printer", "RA": "Uplink to the cockpit printer",
    ":;": "Radio auto-tune (change of ACARS frequency)",
}
H1_KINDS = (("#DF", "Engine and flight data report (airline-specific format)"),
            ("#CF", "Maintenance fault report"), ("#M1", "Flight management computer (FMS)"),
            ("#M2", "Flight management computer (FMS)"), ("#MD", "Flight management computer (FMS)"),
            ("#T", "Cabin terminal message"), ("#EI", "Engine report"),
            ("#10", "Avionics data block"), ("#1", "Avionics data block"))
# ARINC 622 air traffic services inside ACARS: '/<sub> <ground address>.<IMI>.<reg><hex>'
ATS_IMI = {"AT1": "CPDLC: controller-pilot datalink message", "CR1": "CPDLC logon (connect request)",
           "CC1": "CPDLC connect confirm", "DR1": "CPDLC disconnect", "ADS": "ADS-C position report",
           "DIS": "ADS-C disconnect", "FMH": "FANS message", "FPO": "Flight plan"}
_ATS = re.compile(r"/([A-Z0-9]{2})\s?([A-Z0-9]{7})\.([A-Z0-9]{3})\.")
SA_MEDIA = {"V": "VHF ACARS", "S": "satcom", "H": "HF", "G": "Globalstar satcom", "C": "ICO satcom",
            "2": "VDL mode 2", "X": "Inmarsat satcom", "I": "Iridium satcom"}


def media_advisory(text):
    """Label SA, e.g. '0EV215623V/': which datalink was established or lost, at what time (UTC)."""
    t = (text or "").strip()
    if len(t) < 10 or t[1] not in "EL":
        return None
    media = SA_MEDIA.get(t[2], t[2])
    hh, mm, ss = t[3:5], t[5:7], t[7:9]
    up = [SA_MEDIA.get(c, c) for c in t[9:].split("/")[0] if c in SA_MEDIA]
    return (f"Datalink status: {media} {'connected' if t[1] == 'E' else 'lost'} at {hh}:{mm}:{ss} UTC"
            + (f" (available: {', '.join(up)})" if up else ""))


_ROUTE = re.compile(r"(?<![A-Z])([KPC][A-Z]{3}|M[MGHNRPSUWYZ][A-Z]{2})[ ,/-]?([KPC][A-Z]{3}|M[MGHNRPSUWYZ][A-Z]{2})(?![A-Z])")


def route(text):
    """'...KDENKSBA...' / 'KONT,KLAS' -> ('KDEN', 'KSBA'): two airport codes back to back."""
    for m in _ROUTE.finditer(text or ""):
        a, b = m.group(1), m.group(2)
        if a != b:
            return a, b
    return None


def describe(acars):
    """Short plain-language line for an ACARS message (what it is, plus a position if it has one)."""
    lab = (acars or {}).get("label", "")
    text = (acars or {}).get("text", "") or ""
    ats = _ATS.search(text[:40])
    if lab.startswith("_"):
        what = "Keep-alive (no content)"
    elif ats and ats.group(3) in ATS_IMI:
        what = f"{ATS_IMI[ats.group(3)]} (with {ats.group(2)})"
    elif lab == "SA" and media_advisory(text):
        what = media_advisory(text)
    elif lab == "H1":
        what = next((d for p, d in H1_KINDS if text.startswith(p)), "Avionics message")
    elif lab in LABELS:
        what = LABELS[lab]
    elif lab[:1] == "Q" and lab != "Q0":
        what = "Out / Off / On / In time report (gate and runway times)"
    elif lab[:1].isdigit():
        what = f"Airline message (label {lab})"
    else:
        what = f"Label {lab}" if lab else "Message"
    rt = route(text)
    if rt and not ats:
        what += f" · route {rt[0]} → {rt[1]}"
    pos = position(text)
    if pos:
        what += f" · position {abs(pos[0]):.2f}°{'N' if pos[0] >= 0 else 'S'} {abs(pos[1]):.2f}°{'E' if pos[1] >= 0 else 'W'}"
    return what

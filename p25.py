"""
p25.py - P25 Phase 1 receive chain (C4FM, 4800 baud), stage 1.

    IQ  -> Channelizer (48 kHz, +-6 kHz)          [dsp.py]
        -> FM discriminator, in symbol units (600 Hz deviation per unit)
        -> integrate-and-dump (1-symbol boxcar), the C4FM receive filter: it
           undoes the transmitter's inverse-sinc shaping -> raised-cosine eye
        -> Gardner symbol timing recovery (10 samples/symbol)
        -> adaptive scale + 4-level slicer -> dibits
        -> frame sync (48 bits, 0x5575F5FF77FF)
        -> NID: NAC (12 bits) + DUID (4 bits), BCH(63,16) max-likelihood decode

Only clear-channel framing is handled here; nothing in this module deals
with (or ever will deal with) encryption keys.

Reference: TIA-102.BAAA (FDMA common air interface).
"""

import json
from pathlib import Path

import numpy as np

from dsp import Channelizer, StreamFir, cu8_to_iq

SYMBOL_RATE = 4800
CHAN_RATE = 48_000
SPS = CHAN_RATE // SYMBOL_RATE            # 10 samples per symbol
DEV_PER_LEVEL = 600.0                     # Hz per symbol unit: +-1 -> 600 Hz, +-3 -> 1800 Hz
CHAN_HALF_BW = 6_000.0

FRAME_SYNC = 0x5575F5FF77FF
DIBIT_TO_SYM = {0: 1.0, 1: 3.0, 2: -1.0, 3: -3.0}
SYNC_DIBITS = np.array([(FRAME_SYNC >> (46 - 2 * i)) & 3 for i in range(24)], dtype=np.uint8)
SYNC_SYMS = np.array([DIBIT_TO_SYM[d] for d in SYNC_DIBITS], dtype=np.float32)
STATUS_PERIOD = 36                        # a status dibit after every 35 data dibits

DUID_NAMES = {0x0: "HDU", 0x3: "TDU", 0x5: "LDU1", 0x7: "TSDU", 0xA: "LDU2",
              0xC: "PDU", 0xF: "TDULC"}


# --------------------------------------------------------------------------
# NID: BCH(63,16,23) + even parity bit
# --------------------------------------------------------------------------
NID_GEN = int("6331141367235453", 8)      # TIA-102.BAAA generator, degree 47


def _pmod(a: int, m: int) -> int:
    dm = m.bit_length() - 1
    while a and a.bit_length() - 1 >= dm:
        a ^= m << (a.bit_length() - 1 - dm)
    return a


def nid_codeword63(info16: int) -> int:
    shifted = (info16 & 0xFFFF) << 47
    return shifted | _pmod(shifted, NID_GEN)


def nid_encode(nac: int, duid: int) -> int:
    """64-bit NID: 63-bit BCH codeword followed by an even-parity bit."""
    cw = nid_codeword63(((nac & 0xFFF) << 4) | (duid & 0xF))
    return (cw << 1) | (bin(cw).count("1") & 1)


def _popcount64(a: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(a)
    b = a.view(np.uint8).reshape(-1, 8)
    return _POP8[b].sum(axis=1)


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _build_nid_table() -> np.ndarray:
    basis = np.array([nid_codeword63(1 << i) for i in range(16)], dtype=np.uint64)
    idx = np.arange(65536, dtype=np.uint32)
    table = np.zeros(65536, dtype=np.uint64)
    for i in range(16):
        table[(idx >> i) & 1 == 1] ^= basis[i]
    return table


_NID_TABLE = _build_nid_table()
NID_MAX_ERRORS = 11                       # BCH(63,16,23) corrects up to 11


def nid_decode(nid64: int):
    """Max-likelihood decode. Returns (nac, duid, bit_errors) or None if too damaged."""
    r = np.uint64(nid64 >> 1)
    dist = _popcount64(_NID_TABLE ^ r)
    info = int(np.argmin(dist))
    err = int(dist[info])
    if err > NID_MAX_ERRORS:
        return None
    return info >> 4, info & 0xF, err


# --------------------------------------------------------------------------
# C4FM demodulator
# --------------------------------------------------------------------------
class C4FMDemod:
    """48 kHz complex baseband -> soft symbols (nominally +-1, +-3)."""

    TIMING_GAIN = -0.005         # Gardner loop gain (negative: e > 0 means sampling early)
    SCALE_ALPHA = 0.002          # per-symbol smoothing of the amplitude estimate

    def __init__(self):
        self.lpf = StreamFir(np.full(SPS, 1.0 / SPS, dtype=np.float32), np.float32)
        self.last = None
        self.dc = None
        self.buf = np.zeros(0, dtype=np.float32)
        self.t = float(SPS)                   # next sampling instant in buf
        self.prev = 0.0
        self.mean_abs = 2.0                   # E|s| = 2 for random 4-level data

    def process(self, y: np.ndarray) -> np.ndarray:
        if len(y) == 0:
            return np.zeros(0, dtype=np.float32)
        if self.last is None:
            self.last = y[0]
        z = np.concatenate([[self.last], y])
        self.last = y[-1]
        disc = (np.angle(z[1:] * np.conj(z[:-1])) * (CHAN_RATE / (2 * np.pi * DEV_PER_LEVEL))
                ).astype(np.float32)
        # carrier offset shows up as DC; random data averages to ~0 over a block
        m = float(disc.mean())
        self.dc = m if self.dc is None else 0.5 * self.dc + 0.5 * m
        x = self.lpf(disc - np.float32(self.dc))

        buf = np.concatenate([self.buf, x])
        out = []
        t = self.t
        half = SPS / 2
        n = len(buf)
        while t + 2 < n:
            i = int(t)
            f = t - i
            s = buf[i] + (buf[i + 1] - buf[i]) * f
            tm = t - half
            im = int(tm)
            fm = tm - im
            m = buf[im] + (buf[im + 1] - buf[im]) * fm
            # Gardner timing error, normalised by the current amplitude estimate
            e = (s - self.prev) * m / (self.mean_abs * self.mean_abs)
            self.prev = s
            self.mean_abs += self.SCALE_ALPHA * (abs(s) - self.mean_abs)
            out.append(s * 2.0 / max(self.mean_abs, 1e-3))
            t += SPS + self.TIMING_GAIN * SPS * e
        keep_from = max(0, int(t) - SPS - 2)
        self.buf = buf[keep_from:]
        self.t = t - keep_from
        return np.asarray(out, dtype=np.float32)


def c4fm_tx_filter(sps: int = SPS, alpha: float = 0.2, ntaps: int = 161) -> np.ndarray:
    """Standard C4FM transmit filter: raised cosine x inverse-sinc shaping (TIA-102.BAAA)."""
    f = np.fft.fftfreq(4096, d=1 / sps)          # in units of the symbol rate
    af = np.abs(f)
    rc = np.where(af <= (1 - alpha) / 2, 1.0,
                  np.where(af <= (1 + alpha) / 2,
                           0.5 * (1 + np.cos(np.pi / alpha * (af - (1 - alpha) / 2))), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        shaping = np.where(af < 1e-9, 1.0, np.pi * af / np.sin(np.pi * np.minimum(af, 0.999)))
    h = np.fft.fftshift(np.real(np.fft.ifft(rc * shaping)))
    c = len(h) // 2
    h = h[c - ntaps // 2: c + ntaps // 2 + 1] * np.hamming(ntaps)
    return h / h.sum()


def slice_dibits(syms: np.ndarray) -> np.ndarray:
    """+3 -> 1, +1 -> 0, -1 -> 2, -3 -> 3"""
    d = np.empty(len(syms), dtype=np.uint8)
    d[syms >= 2] = 1
    d[(syms >= 0) & (syms < 2)] = 0
    d[(syms < 0) & (syms >= -2)] = 2
    d[syms < -2] = 3
    return d


# --------------------------------------------------------------------------
# Framer
# --------------------------------------------------------------------------
class P25Framer:
    """Finds frame syncs in the dibit stream and decodes the NID after each."""

    MAX_SYNC_ERRORS = 3          # dibit mismatches allowed in the 24-dibit sync
    NID_SPAN = 33                # 32 NID dibits + the status dibit at frame position 35

    def __init__(self):
        self.d = np.zeros(0, dtype=np.uint8)
        self.s = np.zeros(0, dtype=np.float32)
        self.base = 0            # absolute symbol index of self.d[0]
        self.skip_until = 0      # don't look for a new sync before this symbol
        self.frames = []

    def push(self, syms: np.ndarray):
        self.d = np.concatenate([self.d, slice_dibits(syms)])
        self.s = np.concatenate([self.s, syms])
        found = []
        n = len(self.d)
        start = max(0, self.skip_until - self.base)
        i = start
        # vectorised sync search over the new window
        if n - 24 - self.NID_SPAN > i:
            windows = np.lib.stride_tricks.sliding_window_view(self.d[i:n - self.NID_SPAN], 24)
            errs = (windows != SYNC_DIBITS).sum(axis=1)
            cand = np.nonzero(errs <= self.MAX_SYNC_ERRORS)[0]
            last_taken = -10**9
            for c in cand:
                pos = i + int(c)
                if pos < last_taken + 24 + self.NID_SPAN:
                    continue
                f = self._read_frame(pos, int(errs[c]))
                if f is not None:
                    found.append(f)
                    last_taken = pos
                    self.skip_until = self.base + pos + 24 + self.NID_SPAN
        # keep a tail long enough to catch a sync straddling the block edge
        keep = 24 + self.NID_SPAN + 8
        drop = max(0, n - keep)
        self.d = self.d[drop:]
        self.s = self.s[drop:]
        self.base += drop
        self.frames.extend(found)
        return found

    def _read_frame(self, pos: int, sync_err: int):
        nid_d = self.d[pos + 24: pos + 24 + self.NID_SPAN]
        if len(nid_d) < self.NID_SPAN:
            return None
        nid_d = np.concatenate([nid_d[:11], nid_d[12:]])      # drop status dibit (frame pos 35)
        nid = 0
        for dib in nid_d:
            nid = (nid << 2) | int(dib)
        dec = nid_decode(nid)
        if dec is None:
            return None
        nac, duid, bch_err = dec
        sync_syms = self.s[pos:pos + 24]
        level = float(np.mean(np.abs(sync_syms)))             # ideal 3.0
        return {"symbol": self.base + pos, "t": (self.base + pos) / SYMBOL_RATE,
                "nac": nac, "duid": duid, "duid_name": DUID_NAMES.get(duid, f"0x{duid:X}"),
                "bch_errors": bch_err, "sync_errors": sync_err, "sync_level": level}


# --------------------------------------------------------------------------
# Whole chain
# --------------------------------------------------------------------------
class P25Phase1Receiver:
    def __init__(self, rate: float, offset_hz: float):
        self.chan = Channelizer(rate, CHAN_RATE, CHAN_HALF_BW, offset_hz)
        self.demod = C4FMDemod()
        self.framer = P25Framer()

    def process(self, iq: np.ndarray):
        return self.framer.push(self.demod.process(self.chan.process(iq)))


def decode_recording(cu8_path, target_hz=None, block=262144):
    """Run a smashDR .cu8 recording (with its .json sidecar) through the chain."""
    cu8_path = Path(cu8_path)
    meta = json.loads(cu8_path.with_suffix(".json").read_text())
    rate = float(meta["sample_rate"])
    target = float(target_hz if target_hz is not None else meta["target_hz"])
    rx = P25Phase1Receiver(rate, target - float(meta["center_hz"]))
    raw = np.memmap(cu8_path, dtype=np.uint8, mode="r")
    for i in range(0, len(raw) // 2, block):
        rx.process(cu8_to_iq(np.asarray(raw[2 * i:2 * (i + block)]).tobytes()))
    return rx.framer.frames, meta

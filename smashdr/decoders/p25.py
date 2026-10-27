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

from smashdr.decoders import p25_fec as pf
from smashdr.core.dsp import Channelizer, StreamFir, cu8_to_iq

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
    """
    48 kHz complex baseband -> raw symbol samples in deviation units
    (nominally +-1, +-3 plus a small offset). Gain and offset are NOT
    normalised here: the framer fits them per frame from the frame sync,
    which makes the first frame of a transmission as reliable as the rest.
    """

    TIMING_GAIN = -0.008         # Gardner loop gain (negative: e > 0 means sampling early)
    SCALE_ALPHA = 0.02           # amplitude estimate used only to normalise the timing error

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
            out.append(s)
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
# Frame layout
# --------------------------------------------------------------------------
# frame lengths in dibits, from the first frame-sync dibit, status dibits included
FRAME_DIBITS = {0x0: 396, 0x3: 72, 0x5: 864, 0xA: 864, 0xF: 216,
                0x7: 360}      # TSDU: room for 3 TSBKs; the real length comes from LB
NID_ONLY_DIBITS = 57          # sync + NID + status, for frame types not decoded yet

# LDU data-bit positions (status dibits removed): sync 48 + NID 64, then
# VC1 VC2 LC1 VC3 LC2 VC4 LC3 VC5 LC4 VC6 LC5 VC7 LC6 VC8 LSD VC9
VOICE_STARTS = [112, 256, 440, 624, 808, 992, 1176, 1360, 1536]
LC_STARTS = [400, 584, 768, 952, 1136, 1320]          # 40 bits = 4 Hamming(10,6) words each
LSD_START = 1504
HDU_START = 112                                       # 36 Golay(18,6) words

# IMBE de-interleave: codeword bit j = row r (j // 24), column c (j % 24)
_A = [0, 1, 2, 3, 4, 5]
_B = [7, 6, 9, 8, 11, 10]
IMBE_PERM = np.array([12 * ((j % 24) // 2) + (_A[j // 24] if (j % 24) % 2 == 0 else _B[j // 24])
                      for j in range(144)], dtype=np.int32)

ALGIDS = {0x80: "clear", 0x81: "DES-OFB", 0x83: "Triple DES", 0x84: "AES-256",
          0x85: "AES-128", 0x9F: "DES-XL", 0xAA: "ADP/RC4"}


def algid_name(a: int) -> str:
    return ALGIDS.get(a, f"0x{a:02X}")


def frame_bits(dibits: np.ndarray) -> np.ndarray:
    """Frame dibits (status included) -> data bits with the status dibits removed."""
    keep = (np.arange(len(dibits)) % STATUS_PERIOD) != STATUS_PERIOD - 1
    d = dibits[keep]
    bits = np.empty(2 * len(d), dtype=np.uint8)
    bits[0::2] = d >> 1
    bits[1::2] = d & 1
    return bits


def _bits_to_int(bits) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def imbe_codewords(bits: np.ndarray, vc_start: int) -> np.ndarray:
    """One de-interleaved 144-bit IMBE frame as mbelib's imbe_fr[8][23] (MSB at the top index)."""
    cw = bits[vc_start + IMBE_PERM]
    fr = np.zeros((8, 23), dtype=np.uint8)
    spans = [(0, 23), (23, 46), (46, 69), (69, 92), (92, 107), (107, 122), (122, 137), (137, 144)]
    for i, (a, b) in enumerate(spans):
        seg = cw[a:b]
        fr[i, :len(seg)] = seg[::-1]              # first bit on air -> highest index
    return fr


def _ldu_hexbits(bits: np.ndarray):
    hb, herr = [], 0
    for st in LC_STARTS:
        for w in range(4):
            v = _bits_to_int(bits[st + 10 * w: st + 10 * w + 10])
            d, e = pf.hamming1063_decode(v)
            hb.append(d)
            herr += e
    return hb, herr


def parse_lc(lc: bytes) -> dict:
    pb, sf, lco, mfid = lc[0] >> 7, (lc[0] >> 6) & 1, lc[0] & 0x3F, lc[1]
    out = {"lco": lco, "mfid": mfid, "protected": bool(pb)}
    standard = mfid in (0x00, 0x01) or sf == 1
    if pb:
        return out                                # LC contents protected: nothing to read
    if lco == 0x00 and standard:
        out.update(kind="group", svc=lc[2], emergency=bool(lc[2] & 0x80),
                   tgid=(lc[4] << 8) | lc[5], src=(lc[6] << 16) | (lc[7] << 8) | lc[8])
    elif lco == 0x03 and standard:
        out.update(kind="unit", svc=lc[2], dst=(lc[3] << 16) | (lc[4] << 8) | lc[5],
                   src=(lc[6] << 16) | (lc[7] << 8) | lc[8])
    return out


_G24 = None


def _golay24_decode(cw: int):
    """Golay(24,12) by nearest codeword -> (data12, bit errors)."""
    global _G24
    if _G24 is None:
        _G24 = np.array([pf.golay24_encode(d) for d in range(4096)], dtype=np.uint32)
    x = _G24 ^ np.uint32(cw)
    w = np.unpackbits(x.view(np.uint8)).reshape(-1, 32).sum(1)
    d = int(np.argmin(w))
    return d, int(w[d])


def decode_frame_body(duid: int, bits: np.ndarray) -> dict:
    """Decode what we can from a frame's data bits."""
    info = {}
    if duid in (0x5, 0xA):
        hb, herr = _ldu_hexbits(bits)
        info["hamming_errors"] = herr
        if duid == 0x5:
            data, rerr = pf.rs_decode(hb, 12)                 # RS(24,12,13)
            info["rs_errors"] = rerr
            if data is not None:
                lc = pf.hexbits_to_bytes(data)
                info["lc"] = lc.hex()
                info.update(parse_lc(lc))
        else:
            data, rerr = pf.rs_decode(hb, 8)                  # RS(24,16,9)
            info["rs_errors"] = rerr
            if data is not None:
                es = pf.hexbits_to_bytes(data)                # MI(9) ALGID(1) KID(2)
                info["algid"] = es[9]
                info["kid"] = (es[10] << 8) | es[11]
                info["encrypted"] = es[9] != 0x80
        info["imbe"] = np.stack([imbe_codewords(bits, st) for st in VOICE_STARTS])
    elif duid == 0x7:
        from smashdr.decoders.p25_tsbk import tsbks_from_tsdu, used_dibits
        tsbks = tsbks_from_tsdu(bits)
        info["tsbks"] = tsbks
        info["_used"] = used_dibits(len(tsbks))
    elif duid == 0xF:
        # TDULC: 12 Golay(24,12) words after the NID -> 24 hexbits -> RS(24,12,13) -> LC
        hb, gerr = [], 0
        for w in range(12):
            st = 112 + 24 * w
            d, e = _golay24_decode(_bits_to_int(bits[st:st + 24]))
            hb += [d >> 6, d & 0x3F]
            gerr += e
        data, rerr = pf.rs_decode(hb, 12)
        info.update(golay_errors=gerr, rs_errors=rerr)
        if data is not None:
            lc = pf.hexbits_to_bytes(data)
            info["lc"] = lc.hex()
            info.update(parse_lc(lc))
    elif duid == 0x0:
        hb, gerr = [], 0
        for w in range(36):
            st = HDU_START + 18 * w
            d, e = pf.golay18_decode(_bits_to_int(bits[st:st + 18]))
            hb.append(d)
            gerr += e
        data, rerr = pf.rs_decode(hb, 16)                     # RS(36,20,17)
        info.update(golay_errors=gerr, rs_errors=rerr)
        if data is not None:
            h = pf.hexbits_to_bytes(data)                     # MI(9) MFID ALGID KID(2) TGID(2)
            info.update(mfid=h[9], algid=h[10], kid=(h[11] << 8) | h[12],
                        tgid=(h[13] << 8) | h[14], encrypted=h[10] != 0x80)
    return info


# --------------------------------------------------------------------------
# Framer
# --------------------------------------------------------------------------
_SYNC_C = SYNC_SYMS - SYNC_SYMS.mean()
_SYNC_N = float(np.linalg.norm(_SYNC_C))
_LS = np.linalg.pinv(np.stack([SYNC_SYMS, np.ones(24, dtype=np.float32)], axis=1))   # [A, D]
_LEVELS = np.array([-3.0, -1.0, 1.0, 3.0], dtype=np.float32)


def _slice(x: np.ndarray) -> np.ndarray:
    return slice_dibits(x)


def _fit_and_slice(raw: np.ndarray, A: float, D: float, passes: int = 2):
    """Slice with gain/offset from the sync, then refine both from the decisions."""
    for _ in range(passes):
        x = (raw - D) / A
        L = _LEVELS[np.argmin(np.abs(x[:, None] - _LEVELS[None, :]), axis=1)]
        M = np.stack([L, np.ones_like(L)], axis=1)
        sol, *_ = np.linalg.lstsq(M, raw, rcond=None)
        A, D = float(sol[0]), float(sol[1])
        if A <= 0.1:
            break
    return _slice((raw - D) / A), A, D


class P25Framer:
    """
    Finds frame syncs in the raw symbol stream (normalised correlation, so it
    doesn't depend on gain or offset), fits gain/offset to each frame's own
    sync, decodes the NID, then collects and decodes the whole frame.
    """

    SYNC_CORR = 0.80            # normalised correlation threshold for a sync candidate
    NID_SPAN = 33               # 32 NID dibits + the status dibit at frame position 35

    def __init__(self, on_frame=None):
        self.s = np.zeros(0, dtype=np.float32)
        self.base = 0           # absolute symbol index of self.s[0]
        self.search_from = 0    # absolute index where the next sync search starts
        self.pending = None     # (abs_pos, length, info) waiting for more symbols
        self.frames = []
        self.on_frame = on_frame

    def push(self, syms: np.ndarray):
        self.s = np.concatenate([self.s, syms.astype(np.float32)])
        found = []
        while True:
            if self.pending is not None:
                pos, length, f = self.pending
                rel = pos - self.base
                if rel + length > len(self.s):
                    break                                  # wait for the rest of the frame
                self.pending = None
                self._finish(f, rel, length)
                found.append(f)
                self.search_from = pos + f.pop("_used", length)
                continue
            f = self._next_sync()
            if f is None:
                break
            pos, length = f["symbol"], f.pop("_len")
            self.pending = (pos, length, f)
        # trim: keep from the pending frame, else a tail long enough for a sync + NID
        if self.pending is not None:
            drop = self.pending[0] - self.base
        else:
            drop = max(0, len(self.s) - (24 + self.NID_SPAN + 8))
            drop = min(drop, max(0, self.search_from - self.base))
        if drop > 0:
            self.s = self.s[drop:]
            self.base += drop
        self.frames.extend(found)
        if self.on_frame:
            for f in found:
                self.on_frame(f)
        return found

    def _next_sync(self):
        start = max(0, self.search_from - self.base)
        end = len(self.s) - 24 - self.NID_SPAN
        if end <= start:
            return None
        w = np.lib.stride_tricks.sliding_window_view(self.s[start:end + 24], 24)
        wc = w - w.mean(axis=1, keepdims=True)
        corr = (wc @ _SYNC_C) / (np.linalg.norm(wc, axis=1) * _SYNC_N + 1e-9)
        for c in np.nonzero(corr >= self.SYNC_CORR)[0]:
            rel = start + int(c)
            # local peak only
            if c + 1 < len(corr) and corr[c + 1] > corr[c]:
                continue
            A, D = (_LS @ self.s[rel:rel + 24]).tolist()
            if A <= 0.1:
                continue
            seg = _slice((self.s[rel:rel + 24 + self.NID_SPAN] - D) / A)
            nid_d = np.concatenate([seg[24:35], seg[36:57]])
            dec = nid_decode(_bits_to_int(np.concatenate([(nid_d >> 1)[:, None],
                                                           (nid_d & 1)[:, None]], axis=1).ravel()))
            if dec is None:
                continue
            nac, duid, bch_err = dec
            sync_err = int((seg[:24] != SYNC_DIBITS).sum())
            length = FRAME_DIBITS.get(duid, NID_ONLY_DIBITS)
            self.search_from = self.base + rel + 1
            return {"symbol": self.base + rel, "t": (self.base + rel) / SYMBOL_RATE,
                    "nac": nac, "duid": duid, "duid_name": DUID_NAMES.get(duid, f"0x{duid:X}"),
                    "bch_errors": bch_err, "sync_errors": sync_err, "sync_corr": float(corr[c]),
                    "sync_level": float(A) * 3.0, "_A": A, "_D": D, "_len": length}
        self.search_from = self.base + end
        return None

    def _finish(self, f, rel, length):
        A, D = f.pop("_A"), f.pop("_D")
        dib, A, D = _fit_and_slice(self.s[rel:rel + length], A, D)
        f["gain"], f["offset"] = A, D
        if f["duid"] in FRAME_DIBITS:
            f.update(decode_frame_body(f["duid"], frame_bits(dib)))


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


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------
CALL_GAP_S = 0.6             # a gap this long (or an HDU/TDU) separates transmissions


def group_calls(frames: list) -> list:
    """
    Group decoded frames into transmissions with talkgroup, source radio,
    encryption state and the voice frames. Encrypted calls keep their metadata
    (so you can see they happened) but never carry voice.
    """
    calls, cur = [], None

    def close():
        if cur and (cur["ldus"] or cur["hdu"]):
            if cur["encrypted"]:
                cur["imbe"] = []
            calls.append(cur)

    for f in frames:
        d = f["duid"]
        starts_new = (cur is None or d == 0x0 or
                      f["t"] - cur["end"] > CALL_GAP_S)
        if starts_new:
            close()
            cur = {"start": f["t"], "end": f["t"], "nac": f["nac"], "tgid": None, "src": None,
                   "dst": None, "algid": None, "encrypted": False, "hdu": False, "ldus": 0,
                   "imbe": [], "emergency": False}
        cur["end"] = f["t"] + FRAME_DIBITS.get(d, NID_ONLY_DIBITS) / SYMBOL_RATE
        if d == 0x0 and f.get("rs_errors", -1) >= 0:
            cur["hdu"] = True
            cur["tgid"] = f["tgid"]
            cur["algid"] = f["algid"]
        elif d in (0x5, 0xA):
            cur["ldus"] += 1
            if f.get("kind") == "group":
                cur["tgid"], cur["src"] = f["tgid"], f["src"]
                cur["emergency"] |= f.get("emergency", False)
            elif f.get("kind") == "unit":
                cur["dst"], cur["src"] = f["dst"], f["src"]
            if "algid" in f:
                cur["algid"] = f["algid"]
            if "imbe" in f:
                cur["imbe"].extend(f["imbe"])
        if cur["algid"] is not None:
            cur["encrypted"] = cur["algid"] != 0x80
        if d in (0x3, 0xF):                    # terminator ends the call
            close()
            cur = None
    close()
    return calls

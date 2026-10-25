"""
ysf.py - Yaesu System Fusion (C4FM, 4800 sym/s) receive: frame sync, FICH,
callsigns (header CSD1 / DN-mode DCH), DN voice (AMBE+2 49-bit params).

Frame: 480 symbols / 100 ms = 40-bit sync 0xD471C9634D | 200-bit FICH |
5 x 144-bit blocks. FICH and data channels: rate-1/2 K=5 convolutional code
(g1 = d^d3^d4, g2 = d^d1^d2^d4) with an interleaver of 2*row + 40*col, CRC-16
CCITT (init 0, xor-out 0xFFFF) over the transmitted bytes; data is whitened.
DN ("V/D mode 2") block = 40-bit DCH + 104-bit VCH; VCH = 27 bits sent 3x
(majority vote) + 22 bits, interleaved 26x4 and whitened -> 49 AMBE bits.
Verified on a 449.34 recording (VE6DBD via AA6RJ/R): 0 FEC errors, triplets
agree 100%, CRCs good.
"""

import numpy as np

SYNC = 0xD471C9634D
FRAME_SYMS = 480
_SYM = {1: 3.0, 0: 1.0, 2: -1.0, 3: -3.0}
SYNC_SYMS = np.array([_SYM[(SYNC >> (38 - 2 * i)) & 3] for i in range(20)], dtype=np.float32)
WHITEN = bytes([0x93, 0xD7, 0x51, 0x21, 0x9C, 0x2F, 0x6C, 0xD0, 0xEF, 0x0F,
                0xF8, 0x3D, 0xF1, 0x73, 0x20, 0x94, 0xED, 0x1E, 0x7C, 0xD8])
WHITEN_BITS = np.unpackbits(np.frombuffer(WHITEN, dtype=np.uint8))
FI_NAMES = {0: "header", 1: "communication", 2: "terminator", 3: "test"}
DT_NAMES = {0: "DN (V/D 1)", 1: "data FR", 2: "DN (V/D 2)", 3: "VW (voice FR)"}

_IL5 = np.array([2 * r + 40 * c for r in range(20) for c in range(5)])
_IL9 = np.array([2 * r + 40 * c for r in range(20) for c in range(9)])
_VCH_T = np.argsort(np.array([26 * (j % 4) + j // 4 for j in range(104)]))


def bits_from_syms(seg: np.ndarray) -> np.ndarray:
    lv = np.array([3.0, 1.0, -1.0, -3.0])
    idx = np.argmin(np.abs(seg[:, None] - lv[None, :]), axis=1)
    d = np.array([1, 0, 2, 3], dtype=np.uint8)[idx]
    b = np.zeros(2 * len(d), dtype=np.uint8)
    b[0::2] = d >> 1
    b[1::2] = d & 1
    return b


# --------------------------------------------------------------------------
# K=5 rate-1/2 Viterbi (hard decision)
# --------------------------------------------------------------------------
def _trellis():
    nxt = np.zeros((16, 2), dtype=np.int64)
    out = np.zeros((16, 2, 2), dtype=np.uint8)
    for st in range(16):
        d1, d2, d3, d4 = (st >> 3) & 1, (st >> 2) & 1, (st >> 1) & 1, st & 1
        for b in (0, 1):
            out[st, b] = (b ^ d3 ^ d4, b ^ d1 ^ d2 ^ d4)
            nxt[st, b] = (b << 3) | (st >> 1)
    return nxt, out


_NXT, _OUT = _trellis()


def viterbi(pairs: np.ndarray, nout: int):
    """pairs: (n, 2) received bits -> (nout decoded bits, path metric = bit errors)."""
    inf = 1 << 20
    metric = np.full(16, inf, dtype=np.int64)
    metric[0] = 0
    back = np.zeros((len(pairs), 16, 2), dtype=np.int64)       # prev state, bit
    for t, (s0, s1) in enumerate(pairs):
        cost = (_OUT[:, :, 0] != s0).astype(np.int64) + (_OUT[:, :, 1] != s1)
        cand = metric[:, None] + cost                           # (state, bit)
        new = np.full(16, inf, dtype=np.int64)
        bp = np.zeros((16, 2), dtype=np.int64)
        for st in range(16):
            for b in (0, 1):
                ns = _NXT[st, b]
                if cand[st, b] < new[ns]:
                    new[ns] = cand[st, b]
                    bp[ns] = (st, b)
        metric = new
        back[t] = bp
    st = int(np.argmin(metric))
    bits = np.zeros(len(pairs), dtype=np.uint8)
    for t in range(len(pairs) - 1, -1, -1):
        p, b = back[t, st]
        bits[t] = b
        st = p
    return bits[:nout], int(metric.min())


def crc16_ok(b: bytes) -> bool:
    crc = 0
    for x in b[:-2]:
        crc ^= x << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return (crc ^ 0xFFFF) == int.from_bytes(b[-2:], "big")


# --------------------------------------------------------------------------
# Golay(24,12) for the FICH (data in the top 12 bits)
# --------------------------------------------------------------------------
def _golay_table():
    from smashdr.decoders.p25_fec import golay24_encode
    return np.array([golay24_encode(d) for d in range(4096)], dtype=np.uint32)


_G24 = None


def _golay24(cw: int) -> int:
    global _G24
    if _G24 is None:
        _G24 = _golay_table()
    x = _G24 ^ np.uint32(cw)
    w = np.unpackbits(x.view(np.uint8)).reshape(-1, 32).sum(1)
    return int(np.argmin(w))


def decode_fich(bits: np.ndarray):
    """960 frame bits -> dict or None (CRC fail)."""
    fb = bits[40:240]
    out, _err = viterbi(np.stack([fb[_IL5], fb[_IL5 + 1]], axis=1), 96)
    words = [int("".join(map(str, out[24 * i:24 * i + 24])), 2) for i in range(4)]
    b0, b1, b2, b3 = (_golay24(w) for w in words)
    f = bytes([(b0 >> 4) & 0xFF, ((b0 << 4) & 0xF0) | ((b1 >> 8) & 0x0F), b1 & 0xFF,
               (b2 >> 4) & 0xFF, ((b2 << 4) & 0xF0) | ((b3 >> 8) & 0x0F), b3 & 0xFF])
    if not crc16_ok(f):
        return None
    return {"fi": f[0] >> 6, "cs": (f[0] >> 4) & 3, "cm": (f[0] >> 2) & 3, "bn": f[0] & 3,
            "bt": f[1] >> 6, "fn": (f[1] >> 3) & 7, "ft": f[1] & 7,
            "mr": (f[2] >> 3) & 3, "dt": f[2] & 3}


def _dewhiten(b: bytes, n: int) -> bytes:
    return bytes(x ^ WHITEN[i] if i < n else x for i, x in enumerate(b))


def header_csd1(bits: np.ndarray):
    """Header / terminator: (dest 10 chars, source 10 chars) or None."""
    pay = bits[240:]
    d = np.concatenate([pay[144 * i:144 * i + 72] for i in range(5)])
    out, _ = viterbi(np.stack([d[_IL9], d[_IL9 + 1]], axis=1), 176)
    raw = np.packbits(out).tobytes()
    if not crc16_ok(raw[:22]):
        return None
    t = _dewhiten(raw[:20], 20).decode("ascii", "replace")
    return t[:10], t[10:20]


def dn_dch(bits: np.ndarray):
    """DN mode DCH (10 bytes of callsign data by frame number) or None."""
    pay = bits[240:]
    d = np.concatenate([pay[144 * i:144 * i + 40] for i in range(5)])
    out, _ = viterbi(np.stack([d[_IL5], d[_IL5 + 1]], axis=1), 96)
    raw = np.packbits(out).tobytes()
    if not crc16_ok(raw[:12]):
        return None
    return _dewhiten(raw[:10], 10)


def dn_voice(bits: np.ndarray):
    """DN mode: five 49-bit AMBE parameter sets."""
    pay = bits[240:]
    out = []
    for i in range(5):
        v = pay[144 * i + 40:144 * i + 144][_VCH_T] ^ WHITEN_BITS[:104]
        t = v[:81].reshape(27, 3)
        maj = (t.sum(1) >= 2).astype(np.uint8)
        out.append(np.concatenate([maj, v[81:103]]))
    return out


def callsign(s: str) -> str:
    s = (s or "").strip()
    return "" if not s or set(s) <= {"*"} else s

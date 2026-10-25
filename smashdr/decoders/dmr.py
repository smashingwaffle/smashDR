"""
dmr.py - DMR (ETSI TS 102 361) Tier II receive: bursts, slot type, voice LC
header / terminator (BPTC 196,96 + RS 12,9), voice frames (AMBE+2 via mbelib).

Same C4FM-style 4FSK as P25 (4800 sym/s, dibit 01 -> +3, 00 -> +1, 10 -> -1,
11 -> -3), so the P25 C4FMDemod feeds it raw symbols. A repeater ("BS")
transmits continuously: 30 ms per slot = 12 CACH symbols + 132 burst symbols,
slots alternating. Each burst: 98 bits | 10 slot type / EMB | 48 sync |
10 | 98 bits. Voice superframes are 6 bursts (A-F); only A carries the voice
sync, B-F carry an EMB instead, so voice bursts are found by counting
from A (every 60 ms in the same slot).
"""

import numpy as np

SYMS_PER_SLOT = 144                 # 12 CACH + 132 burst
CACH_SYMS = 12
LV = np.array([3.0, 1.0, -1.0, -3.0])
DIBIT_OF = {3.0: 1, 1.0: 0, -1.0: 2, -3.0: 3}
_SYM = {1: 3.0, 0: 1.0, 2: -1.0, 3: -3.0}

SYNCS = {
    "bs_voice": 0x755FD7DF75F7, "bs_data": 0xDFF57D75DF5D,
    "ms_voice": 0x7F7D5DD57DFD, "ms_data": 0xD5D7F77FD757,
    "dm1_voice": 0x5D577F7757FF, "dm1_data": 0xF7FDD5DDFD55,
    "dm2_voice": 0x7DFFD5F55D5F, "dm2_data": 0xD7557F5FF7F5,
}
DATA_TYPES = {0: "PI header", 1: "Voice LC header", 2: "Terminator with LC", 3: "CSBK",
              4: "MBC header", 5: "MBC continuation", 6: "Data header", 7: "Rate 1/2 data",
              8: "Rate 3/4 data", 9: "Idle", 10: "Rate 1 data"}
FLCO = {0: "group", 3: "private"}


def sync_syms(word: int) -> np.ndarray:
    return np.array([_SYM[(word >> (46 - 2 * i)) & 3] for i in range(24)], dtype=np.float32)


SYNC_SYMS = {k: sync_syms(v) for k, v in SYNCS.items()}


def bits_from_syms(seg: np.ndarray) -> np.ndarray:
    idx = np.argmin(np.abs(seg[:, None] - LV[None, :]), axis=1)
    d = np.array([1, 0, 2, 3], dtype=np.uint8)[idx]
    b = np.zeros(2 * len(d), dtype=np.uint8)
    b[0::2] = d >> 1
    b[1::2] = d & 1
    return b


def _int(bits) -> int:
    v = 0
    for x in bits:
        v = (v << 1) | int(x)
    return v


# --------------------------------------------------------------------------
# Slot type (Golay 20,8: data in the top 8 bits)
# --------------------------------------------------------------------------
def slot_type(burst: np.ndarray):
    st = np.concatenate([burst[98:108], burst[156:166]])
    return _int(st[:4]), _int(st[4:8])          # colour code, data type


# --------------------------------------------------------------------------
# BPTC(196,96): 13x15 matrix, Hamming(15,11) rows, Hamming(13,9) columns
# --------------------------------------------------------------------------
def _h15_parity(d):     # d: 11 bits -> 4 parity (ETSI B.3.2)
    return [d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
            d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9],
            d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10],
            d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10]]


def _h13_parity(d):     # d: 9 bits -> 4 parity
    return [d[0] ^ d[1] ^ d[3] ^ d[5] ^ d[6],
            d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7],
            d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8],
            d[0] ^ d[2] ^ d[4] ^ d[5] ^ d[8]]


def _fix(word, n_data, parity_fn):
    """Single-bit correction by trying every flip; returns (word, fixed?, ok?)."""
    w = list(word)
    if list(parity_fn(w[:n_data])) == w[n_data:]:
        return w, False, True
    for k in range(len(w)):
        w[k] ^= 1
        if list(parity_fn(w[:n_data])) == w[n_data:]:
            return w, True, True
        w[k] ^= 1
    return list(word), False, False


def bptc196_decode(burst: np.ndarray):
    """-> (96 data bits, corrected bit count, all rows/cols consistent?)"""
    info = np.concatenate([burst[:98], burst[166:264]])
    deint = info[(np.arange(196) * 181) % 196]
    m = [list(map(int, deint[1 + 15 * r:1 + 15 * r + 15])) for r in range(13)]
    fixed, ok = 0, True
    for _ in range(3):                                # iterate rows <-> columns
        for c in range(15):
            col = [m[r][c] for r in range(13)]
            col2, f, o = _fix(col, 9, _h13_parity)
            fixed += f
            ok_c = o
            for r in range(13):
                m[r][c] = col2[r]
        for r in range(9):
            m[r], f, o = _fix(m[r], 11, _h15_parity)
            fixed += f
    ok = all(list(_h15_parity(m[r][:11])) == m[r][11:] for r in range(9))
    data = m[0][3:11] + sum((m[r][0:11] for r in range(1, 9)), [])
    return np.array(data, dtype=np.uint8), fixed, ok


# --------------------------------------------------------------------------
# RS(12,9) over GF(256) (poly 0x11D), parity masked per data type
# --------------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_syndromes(cw):
    out = []
    for j in range(1, 4):
        s = 0
        for c in cw:
            s = _gmul(s, _EXP[j]) ^ c
        out.append(s)
    return out


RS_MASK = {1: 0x96, 2: 0x99}          # voice LC header, terminator


def lc_check(lc12: bytes, data_type: int):
    """Unmask + RS(12,9) check, with a brute-force single-byte fix.
    -> (9-byte LC or None, corrected?)"""
    m = RS_MASK.get(data_type, 0)
    cw = list(lc12[:9]) + [b ^ m for b in lc12[9:12]]
    if not any(_rs_syndromes(cw)):
        return bytes(cw[:9]), False
    for pos in range(12):
        orig = cw[pos]
        for v in range(256):
            if v == orig:
                continue
            cw[pos] = v
            if not any(_rs_syndromes(cw)):
                return bytes(cw[:9]), True
        cw[pos] = orig
    return None, False


def parse_lc(lc: bytes) -> dict:
    flco = lc[0] & 0x3F
    return {"flco": FLCO.get(flco, flco), "fid": lc[1], "svc": lc[2],
            "encrypted": bool(lc[2] & 0x40),
            "dst": int.from_bytes(lc[3:6], "big"), "src": int.from_bytes(lc[6:9], "big")}


# --------------------------------------------------------------------------
# Voice: 3 AMBE 72-bit frames per burst -> ambe_fr[4][24] (DSD mapping)
# --------------------------------------------------------------------------
_rW = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 2,
       0, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0, 2]
_rX = [23, 10, 22, 9, 21, 8, 20, 7, 19, 6, 18, 5, 17, 4, 16, 3, 15, 2, 14, 1, 13, 0, 12, 10,
       11, 9, 10, 8, 9, 7, 8, 6, 7, 5, 6, 4]
_rY = [0, 2, 0, 2, 0, 2, 0, 2, 0, 3, 0, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3,
       1, 3, 1, 3, 1, 3, 1, 3, 1, 3, 1, 3]
_rZ = [5, 3, 4, 2, 3, 1, 2, 0, 1, 13, 0, 12, 22, 11, 21, 10, 20, 9, 19, 8, 18, 7, 17, 6,
       16, 5, 15, 4, 14, 3, 13, 2, 12, 1, 11, 0]


def ambe_frames(burst: np.ndarray):
    """264-bit voice burst -> three (4, 24) ambe_fr arrays."""
    payload = np.concatenate([burst[:108], burst[156:264]])      # 216 bits
    out = []
    for f in range(3):
        b = payload[72 * f:72 * f + 72]
        fr = np.zeros((4, 24), dtype=np.uint8)
        for i in range(36):
            fr[_rW[i], _rX[i]] = b[2 * i]
            fr[_rY[i], _rZ[i]] = b[2 * i + 1]
        out.append(fr)
    return out

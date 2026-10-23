"""
p25_fec.py - forward error correction used by P25 Phase 1 (TIA-102.BAAA).

    Hamming(10,6,3)   link control / encryption sync hex words in LDUs
    Golay(24,12,8)    HDU hex words (shortened to 18,6) and TDULC
    Reed-Solomon      over GF(64), x^6 + x + 1, first root alpha^1,
                      shortened from (63, k):
                          RS(24,12,13)  LDU1 link control
                          RS(24,16,9)   LDU2 encryption sync
                          RS(36,20,17)  HDU

Everything here is independent, table-free code with matching encoders, so
the synthetic transmitter in tests/ can build fully valid frames.
"""

import numpy as np

# --------------------------------------------------------------------------
# Hamming(10,6,3): 6 data bits + 4 parity, single-error correcting
# --------------------------------------------------------------------------
# parity nibble for each data bit (data bit 5 = MSB of the hex word)
_H1063_PAR = {32: 14, 16: 13, 8: 11, 4: 7, 2: 3, 1: 12}


def hamming1063_encode(data6: int) -> int:
    par = 0
    for bit, p in _H1063_PAR.items():
        if data6 & bit:
            par ^= p
    return (data6 << 4) | par


_H1063_CODES = np.array([hamming1063_encode(d) for d in range(64)], dtype=np.uint16)


def hamming1063_decode(cw10: int):
    """Nearest codeword. Returns (data6, bit_errors)."""
    dist = np.array([bin(int(c) ^ cw10).count("1") for c in _H1063_CODES])
    d = int(np.argmin(dist))
    return d, int(dist[d])


# --------------------------------------------------------------------------
# Golay(23,12,7) / extended (24,12,8)
# --------------------------------------------------------------------------
GOLAY_GEN = 0xC75        # x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1


def _pmod(a: int, m: int) -> int:
    dm = m.bit_length() - 1
    while a and a.bit_length() - 1 >= dm:
        a ^= m << (a.bit_length() - 1 - dm)
    return a


def golay24_encode(data12: int) -> int:
    """[12 data][11 parity][1 overall even parity] -> 24 bits."""
    cw23 = (data12 << 11) | _pmod(data12 << 11, GOLAY_GEN)
    return (cw23 << 1) | (bin(cw23).count("1") & 1)


_G18_CODES = np.array([golay24_encode(d) for d in range(64)], dtype=np.uint32)


def golay18_decode(cw18: int):
    """Shortened Golay(18,6,8) as used in the HDU. Returns (data6, bit_errors)."""
    dist = np.array([bin(int(c) ^ cw18).count("1") for c in _G18_CODES])
    d = int(np.argmin(dist))
    return d, int(dist[d])


# --------------------------------------------------------------------------
# Reed-Solomon over GF(64)
# --------------------------------------------------------------------------
_GF_POLY = 0x43
GF_EXP = [0] * 126
GF_LOG = [0] * 64
_x = 1
for _i in range(63):
    GF_EXP[_i] = _x
    GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x40:
        _x ^= _GF_POLY
for _i in range(63, 126):
    GF_EXP[_i] = GF_EXP[_i - 63]


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def gf_div(a: int, b: int) -> int:
    if a == 0:
        return 0
    return GF_EXP[(GF_LOG[a] - GF_LOG[b]) % 63]


def gf_pow_alpha(e: int) -> int:
    return GF_EXP[e % 63]


def _poly_eval(p, x):
    """p[0] is the highest-degree coefficient."""
    y = 0
    for c in p:
        y = gf_mul(y, x) ^ c
    return y


def _generator(nroots: int):
    g = [1]
    for i in range(1, nroots + 1):          # roots alpha^1 .. alpha^nroots
        root = gf_pow_alpha(i)
        ng = g + [0]
        for j in range(len(g)):
            ng[j + 1] ^= gf_mul(g[j], root)
        g = ng
    return g


_GEN_CACHE = {}


def rs_encode(data: list, n: int) -> list:
    """Systematic RS(n, k) shortened from (63, 63-(n-k)): returns data + parity."""
    nroots = n - len(data)
    g = _GEN_CACHE.setdefault(nroots, _generator(nroots))
    rem = [0] * nroots
    for d in data:
        fb = d ^ rem[0]
        rem = rem[1:] + [0]
        if fb:
            for j in range(nroots):
                rem[j] ^= gf_mul(fb, g[j + 1])
    return list(data) + rem


def rs_decode(word: list, nroots: int):
    """
    Decode a shortened RS codeword (data then parity, len n <= 63).
    Returns (corrected_data, n_errors) or (None, -1) if uncorrectable.
    """
    n = len(word)
    k = n - nroots
    # syndromes S_i = c(alpha^i), i = 1..nroots (c(x) highest degree first)
    synd = [_poly_eval(word, gf_pow_alpha(i)) for i in range(1, nroots + 1)]
    if not any(synd):
        return list(word[:k]), 0

    # Berlekamp-Massey
    C = [1] + [0] * nroots
    B = [1] + [0] * nroots
    L, m, b = 0, 1, 1
    for r in range(nroots):
        d = synd[r]
        for i in range(1, L + 1):
            d ^= gf_mul(C[i], synd[r - i])
        if d == 0:
            m += 1
        elif 2 * L <= r:
            T = C[:]
            coef = gf_div(d, b)
            for i in range(m, nroots + 1):
                C[i] ^= gf_mul(coef, B[i - m])
            L, B, b, m = r + 1 - L, T, d, 1
        else:
            coef = gf_div(d, b)
            for i in range(m, nroots + 1):
                C[i] ^= gf_mul(coef, B[i - m])
            m += 1
    if 2 * L > nroots:
        return None, -1

    # Chien search over the positions that exist in the shortened code.
    # Position p (0 = first symbol) has exponent e = n-1-p; locator X = alpha^e.
    err_pos = []
    for p in range(n):
        e = n - 1 - p
        xinv = gf_pow_alpha(-e)
        s = 0
        for i in range(L, -1, -1):
            s = gf_mul(s, xinv) ^ C[i]
        if s == 0:
            err_pos.append(p)
    if len(err_pos) != L:
        return None, -1

    # Forney: Omega(x) = S(x) * Lambda(x) mod x^nroots, S(x) = sum S_{i+1} x^i
    omega = [0] * nroots
    for i in range(nroots):
        for j in range(0, min(i, L) + 1):
            omega[i] ^= gf_mul(C[j], synd[i - j])
    out = list(word)
    for p in err_pos:
        e = n - 1 - p
        X = gf_pow_alpha(e)
        xinv = gf_pow_alpha(-e)
        num = 0
        for i in range(nroots - 1, -1, -1):
            num = gf_mul(num, xinv) ^ omega[i]
        den = 0                               # Lambda'(X^-1): odd terms
        for i in range(1, L + 1, 2):
            den ^= gf_mul(C[i], gf_pow_alpha(-e * (i - 1)))
        if den == 0:
            return None, -1
        # with first root alpha^1: magnitude = X * Omega(X^-1) / Lambda'(X^-1) * X^-1 ... = Omega/Lambda'
        out[p] ^= gf_div(num, den)
    # verify
    if any(_poly_eval(out, gf_pow_alpha(i)) for i in range(1, nroots + 1)):
        return None, -1
    return out[:k], len(err_pos)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def hexbits_to_bytes(hb: list) -> bytes:
    """Pack 6-bit symbols MSB-first into bytes (len(hb)*6 must be a multiple of 8)."""
    v = 0
    for h in hb:
        v = (v << 6) | (h & 63)
    nbytes = len(hb) * 6 // 8
    return v.to_bytes(nbytes, "big")


def bytes_to_hexbits(b: bytes) -> list:
    v = int.from_bytes(b, "big")
    n = len(b) * 8 // 6
    return [(v >> (6 * (n - 1 - i))) & 63 for i in range(n)]

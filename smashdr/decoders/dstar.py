"""
dstar.py - D-STAR digital voice (GMSK 4800 bit/s): radio header, voice
frames (AMBE 3600x2400, decoded by mbelib-neo), slow data (header copy,
20-character message).

Bit stream (polarity: positive deviation = 1):
  header:  bit sync 1010... + frame sync 111011001010000 + 660 coded bits
           (scrambled, interleaved, rate-1/2 K=3 convolutional, 330 bits ->
           41 bytes LSB first: 3 flags, RPT2, RPT1, UR, MY (8 each), MY suffix
           (4), FCS = CRC-16/X.25 little-endian)
  voice:   frames of 72 voice + 24 data bits, 21 per superframe; the data
           field of frame 0 is the sync 0xAAB468. Other data fields are slow
           data, XOR 70 4F 93, in 6-byte blocks (type/len + 5 bytes):
           0x5n = header copy (late entry), 0x4n = message part n (5 chars).
  end:     1010... + 000100110101111
Verified on 446.56 (KI6MGN C, KJ6ACO /DOUG): 1299 voice frames, 0 errors,
slow-data header FCS good.
"""

import numpy as np

FRAME_SYNC = "111011001010000"
VOICE_SYNC = f"{0xAAB468:024b}"
EOT = "000100110101111"
SUPERFRAME = 21 * 96                      # 2016 bits
VOICE_BITS = 72
SLOW_XOR = (0x70, 0x4F, 0x93)
HEADER_BITS = 660


def bits_of(s: str) -> np.ndarray:
    return np.array([int(c) for c in s], dtype=np.uint8)


# where each of the 72 voice bits goes in mbelib's ambe_fr[4][24]
DW = [0, 0, 3, 2, 1, 1, 0, 0, 1, 1, 0, 0, 3, 2, 1, 1, 3, 2, 1, 1, 0, 0, 3, 2,
      0, 0, 3, 2, 1, 1, 0, 0, 1, 1, 0, 0, 3, 2, 1, 1, 3, 2, 1, 1, 0, 0, 3, 2,
      0, 0, 3, 2, 1, 1, 0, 0, 1, 1, 0, 0, 3, 2, 1, 1, 3, 3, 2, 1, 0, 0, 3, 3]
DX = [10, 22, 11, 9, 10, 22, 11, 23, 8, 20, 9, 21, 10, 8, 9, 21, 8, 6, 7, 19, 8, 20, 9, 7,
      6, 18, 7, 5, 6, 18, 7, 19, 4, 16, 5, 17, 6, 4, 5, 17, 4, 2, 3, 15, 4, 16, 5, 3,
      2, 14, 3, 1, 2, 14, 3, 15, 0, 12, 1, 13, 2, 0, 1, 13, 0, 12, 10, 11, 0, 12, 1, 13]


def ambe_frame(voice72: np.ndarray) -> np.ndarray:
    fr = np.zeros((4, 24), dtype=np.uint8)
    fr[DW, DX] = voice72[:72]
    return fr


def lsb_bytes(bits) -> bytes:
    b = np.asarray(bits, dtype=np.uint8)[: len(bits) // 8 * 8].reshape(-1, 8)
    return bytes((b * (1 << np.arange(8))).sum(1).astype(np.uint8).tolist())


def fcs(data: bytes) -> int:
    """CRC-16/X.25 (reflected 0x1021, init FFFF, inverted)."""
    crc = 0xFFFF
    for x in data:
        crc ^= x
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


def header_fields(h: bytes):
    """41-byte header -> dict, or None if the FCS is wrong."""
    if len(h) < 41 or fcs(h[:39]) != (h[39] | (h[40] << 8)):
        return None

    def txt(b):
        return b.decode("ascii", "replace").strip()
    return {"flags": h[0], "rpt2": txt(h[3:11]), "rpt1": txt(h[11:19]), "ur": txt(h[19:27]),
            "my": txt(h[27:35]), "sfx": txt(h[35:39])}


# ---- radio header (660 coded bits) -----------------------------------------
def _scrambler():
    s, out = 0x7F, []
    for _ in range(HEADER_BITS):
        b = ((s >> 6) ^ (s >> 3)) & 1
        out.append(b)
        s = ((s << 1) | b) & 0x7F
    return np.array(out, dtype=np.uint8)


_SCR = _scrambler()


def _deinterleave_map():
    m, k = np.zeros(HEADER_BITS, dtype=np.int64), 0
    for i in range(HEADER_BITS):
        m[i] = k                                 # received bit i -> position k
        k += 24
        if k >= 672:
            k -= 671
        elif k >= 660:
            k -= 647
    return m


_DEINT = _deinterleave_map()


def _viterbi_k3(pairs, swap=False):
    """Rate 1/2, K=3, G1 = 1+D+D^2, G2 = 1+D^2; hard decision."""
    inf = 1 << 20
    metric = [0, inf, inf, inf]
    hist = []
    for a, b in pairs:
        if swap:
            a, b = b, a
        new = [inf] * 4
        back = [(0, 0)] * 4
        for st in range(4):
            if metric[st] >= inf:
                continue
            d1, d2 = st & 1, (st >> 1) & 1          # previous two input bits
            for x in (0, 1):
                g1, g2 = x ^ d1 ^ d2, x ^ d2
                ns = ((st << 1) | x) & 3
                c = metric[st] + (g1 != a) + (g2 != b)
                if c < new[ns]:
                    new[ns], back[ns] = c, (st, x)
        metric = new
        hist.append(back)
    st = int(np.argmin(metric))
    out = []
    for back in reversed(hist):
        p, x = back[st]
        out.append(x)
        st = p
    return out[::-1], min(metric)


def decode_header(bits660):
    """660 received header bits -> header dict (FCS checked) or None."""
    b = np.asarray(bits660, dtype=np.uint8) ^ _SCR
    d = np.zeros(HEADER_BITS, dtype=np.uint8)
    d[_DEINT] = b
    pairs = d.reshape(-1, 2)
    for swap in (False, True):
        out, _err = _viterbi_k3(pairs, swap)
        h = header_fields(lsb_bytes(out[:328]))
        if h:
            return h
    return None


def encode_header(h41: bytes) -> np.ndarray:
    """For tests: 41 bytes -> 660 transmitted bits (inverse of decode_header)."""
    data = np.unpackbits(np.frombuffer(h41, dtype=np.uint8), bitorder="little").tolist() + [0, 0]
    d1 = d2 = 0
    coded = []
    for x in data:
        coded += [x ^ d1 ^ d2, x ^ d2]
        d1, d2 = x, d1
    coded = np.array(coded, dtype=np.uint8)
    return coded[_DEINT] ^ _SCR


# ---- slow data ----------------------------------------------------------------
def slow_bytes(data24) -> bytes:
    return bytes(x ^ k for x, k in zip(lsb_bytes(data24), SLOW_XOR))


class SlowData:
    """Collects one superframe's slow data (frames 1-20 = ten 6-byte blocks)."""

    def __init__(self):
        self.hdr = bytearray()
        self.msg = {}

    def feed_superframe(self, sd: bytes):
        """sd: 60 descrambled bytes of frames 1..20 -> dict of new info."""
        out = {}
        for i in range(0, len(sd) - 5, 6):
            blk = sd[i:i + 6]
            t, n = blk[0] >> 4, blk[0] & 0xF
            if t == 5:
                self.hdr += blk[1:1 + min(5, n)]
                del self.hdr[:-82]
                if len(self.hdr) >= 41:                 # alignment unknown: try the newest 41
                    h = header_fields(bytes(self.hdr[-41:]))
                    if h:
                        out["header"] = h
                        self.hdr = bytearray()
            elif t == 4 and n < 4:
                self.msg[n] = blk[1:6].decode("ascii", "replace")
                if len(self.msg) == 4:
                    out["message"] = "".join(self.msg[k] for k in range(4)).strip()
        return out

"""
p25_tsbk.py - P25 Phase 1 trunking signalling blocks (TSBK) from a TSDU.

    tsbks_from_tsdu(bits) -> [dict, ...]     bits = TSDU data bits, status removed
    IdenTable.freq(channel16) -> Hz or None  (filled from IDEN_UP* broadcasts)

A TSBK is 196 bits: 98 dibits, interleaved, rate-1/2 trellis coded (4 states)
-> 12 bytes: LB/P/opcode, MFID, 8 argument bytes, CRC-CCITT (init 0, inverted).
Up to 3 per TSDU; the last one has LB set. Verified on 857.1625 (NAC 0x346):
36/36 CRCs good, trellis 0-2 corrections.
"""

import numpy as np

# rate-1/2 trellis: next constellation point = _T[state][input dibit]
_T = [[0, 15, 12, 3], [4, 11, 8, 7], [13, 2, 1, 14], [9, 6, 5, 10]]
# constellation point -> dibit pair (symbols +1 = 0, +3 = 1, -1 = 2, -3 = 3)
_S = {1: 0, 3: 1, -1: 2, -3: 3}
_CP = [(1, -1), (-1, -1), (3, -3), (-3, -3), (-3, -1), (3, -1), (-1, -3), (1, -3),
       (-3, 3), (3, 3), (-1, 1), (1, 1), (1, 3), (-1, 3), (3, 1), (-3, 1)]
_PAIR = [(_S[a] << 2) | _S[b] for a, b in _CP]
# cost[state][input][received 4-bit pair] = bit errors
_COST = np.array([[[bin(_PAIR[_T[s][x]] ^ r).count("1") for r in range(16)]
                   for x in range(4)] for s in range(4)], dtype=np.int32)
# received dibit i belongs at position _IL[i]
_IL = [0, 1, 8, 9, 16, 17, 24, 25, 32, 33, 40, 41, 48, 49, 56, 57, 64, 65, 72, 73, 80, 81, 88, 89,
       96, 97, 2, 3, 10, 11, 18, 19, 26, 27, 34, 35, 42, 43, 50, 51, 58, 59, 66, 67, 74, 75, 82, 83,
       90, 91, 4, 5, 12, 13, 20, 21, 28, 29, 36, 37, 44, 45, 52, 53, 60, 61, 68, 69, 76, 77, 84, 85,
       92, 93, 6, 7, 14, 15, 22, 23, 30, 31, 38, 39, 46, 47, 54, 55, 62, 63, 70, 71, 78, 79, 86, 87,
       94, 95]
_ORDER = np.argsort(_IL)            # deinterleaved[k] = received[_ORDER[k]]

TSBK_BITS = 196
TSDU_START = 112                    # after sync (48) + NID (64)


def trellis_half_decode(dibits):
    """98 dibits (deinterleaved) -> (48 data dibits, bit errors corrected)."""
    pts = [(int(dibits[2 * i]) << 2) | int(dibits[2 * i + 1]) for i in range(49)]
    inf = 1 << 30
    m = np.array([0, inf, inf, inf], dtype=np.int64)
    back = []
    for p in pts:
        c = m[:, None] + _COST[:, :, p]            # (state, input)
        prev = np.argmin(c, axis=0)                # best previous state per new state (= input)
        m = c[prev, np.arange(4)]
        back.append(prev)
    st = int(np.argmin(m))
    err = int(m[st])
    out = []
    for prev in reversed(back):
        out.append(st)
        st = int(prev[st])
    out.reverse()
    return out[:48], err


def _crc_ok(b: bytes) -> bool:
    c = 0
    for x in b[:10]:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return (c ^ 0xFFFF) == int.from_bytes(b[10:12], "big")


def decode_block(bits196):
    """196 coded bits -> 12 bytes or None (CRC)."""
    b = np.asarray(bits196, dtype=np.uint8)
    dib = (b[0::2] << 1) | b[1::2]
    out, _err = trellis_half_decode(dib[_ORDER])
    raw = bytearray()
    acc = 0
    for i, d in enumerate(out):
        acc = (acc << 2) | d
        if i % 4 == 3:
            raw.append(acc)
            acc = 0
    raw = bytes(raw)
    return raw if _crc_ok(raw) else None


def tsbks_from_tsdu(bits):
    """All TSBKs of a TSDU (stops at the one with LB set)."""
    out = []
    for k in range(3):
        st = TSDU_START + TSBK_BITS * k
        if st + TSBK_BITS > len(bits):
            break
        raw = decode_block(bits[st:st + TSBK_BITS])
        if raw is None:
            break
        out.append(parse(raw))
        if raw[0] & 0x80:
            break
    return out


def used_dibits(n_tsbk: int) -> int:
    """Frame length in dibits (status included) for a TSDU with n TSBKs."""
    data = 24 + 32 + 98 * max(1, n_tsbk)
    n = data
    while n - n // 36 < data:
        n += 1
    return n


# --------------------------------------------------------------------------
# Messages (MFID 0x00 = standard)
# --------------------------------------------------------------------------
OPCODES = {0x00: "GRP_V_CH_GRANT", 0x02: "GRP_V_CH_GRANT_UPDT", 0x03: "GRP_V_CH_GRANT_UPDT_EXP",
           0x04: "UU_V_CH_GRANT", 0x05: "UU_ANS_REQ", 0x06: "UU_V_CH_GRANT_UPDT",
           0x08: "TELE_INT_CH_GRANT", 0x14: "SN_DATA_CHN_GNT", 0x20: "ACK_RSP_FNE",
           0x24: "EXT_FNCT_CMD", 0x27: "DENY_RSP", 0x28: "GRP_AFF_RSP", 0x29: "SCCB_EXP",
           0x2A: "GRP_AFF_Q", 0x2B: "LOC_REG_RSP", 0x2C: "U_REG_RSP", 0x2D: "U_REG_CMD",
           0x2F: "U_DE_REG_ACK", 0x30: "SYNC_BCST", 0x33: "IDEN_UP_TDMA", 0x34: "IDEN_UP_VU",
           0x35: "TIME_DATE_ANN", 0x38: "SYS_SRV_BCST", 0x39: "SCCB", 0x3A: "RFSS_STS_BCST",
           0x3B: "NET_STS_BCST", 0x3C: "ADJ_STS_BCST", 0x3D: "IDEN_UP"}
# TDMA channel types -> slots per carrier (TIA-102.AABC)
_TDMA_SLOTS = {0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 2}


def _u(b: bytes, bit0: int, n: int) -> int:
    """n bits starting at bit0 of the 12-byte block (MSB first)."""
    v = int.from_bytes(b, "big") >> (96 - bit0 - n)
    return v & ((1 << n) - 1)


def parse(b: bytes) -> dict:
    op = b[0] & 0x3F
    mfid = b[1]
    m = {"lb": bool(b[0] & 0x80), "opcode": op, "mfid": mfid,
         "name": OPCODES.get(op, f"0x{op:02X}") if mfid == 0 else f"MFID{mfid:02X}/0x{op:02X}",
         "raw": b.hex()}
    if mfid != 0:
        return m
    a = 16                                            # first argument bit
    if op == 0x00:
        m.update(svc=_u(b, a, 8), channel=_u(b, a + 8, 16), tgid=_u(b, a + 24, 16),
                 src=_u(b, a + 40, 24))
        m["emergency"] = bool(m["svc"] & 0x80)
        m["encrypted"] = bool(m["svc"] & 0x40)
    elif op == 0x02:
        m.update(channel=_u(b, a, 16), tgid=_u(b, a + 16, 16),
                 channel_b=_u(b, a + 32, 16), tgid_b=_u(b, a + 48, 16))
    elif op == 0x03:
        m.update(svc=_u(b, a, 8), channel=_u(b, a + 16, 16), channel_rx=_u(b, a + 32, 16),
                 tgid=_u(b, a + 48, 16))
        m["emergency"] = bool(m["svc"] & 0x80)
        m["encrypted"] = bool(m["svc"] & 0x40)
    elif op in (0x04, 0x06):
        m.update(channel=_u(b, a, 16), dst=_u(b, a + 16, 24), src=_u(b, a + 40, 24))
    elif op == 0x3D:                                  # IDEN_UP (VHF/UHF/700/800)
        m.update(iden=_u(b, a, 4), bw=_u(b, a + 4, 9), tx_sign=_u(b, a + 13, 1),
                 tx_off=_u(b, a + 14, 8), spacing=_u(b, a + 22, 10) * 125,
                 base=_u(b, a + 32, 32) * 5, slots=1)
    elif op == 0x34:                                  # IDEN_UP_VU
        m.update(iden=_u(b, a, 4), bw=_u(b, a + 4, 4), tx_sign=_u(b, a + 8, 1),
                 tx_off=_u(b, a + 9, 13), spacing=_u(b, a + 22, 10) * 125,
                 base=_u(b, a + 32, 32) * 5, slots=1)
    elif op == 0x33:                                  # IDEN_UP_TDMA
        ct = _u(b, a + 4, 4)
        m.update(iden=_u(b, a, 4), chan_type=ct, tx_sign=_u(b, a + 8, 1),
                 tx_off=_u(b, a + 9, 13), spacing=_u(b, a + 22, 10) * 125,
                 base=_u(b, a + 32, 32) * 5, slots=_TDMA_SLOTS.get(ct, 2))
    elif op == 0x3A:                                  # RFSS status (this site)
        m.update(lra=_u(b, a, 8), sysid=_u(b, a + 12, 12), rfss=_u(b, a + 24, 8),
                 site=_u(b, a + 32, 8), channel=_u(b, a + 40, 16), svc_class=_u(b, a + 56, 8))
    elif op == 0x3B:                                  # network status
        m.update(lra=_u(b, a, 8), wacn=_u(b, a + 8, 20), sysid=_u(b, a + 28, 12),
                 channel=_u(b, a + 40, 16), svc_class=_u(b, a + 56, 8))
    elif op == 0x3C:                                  # adjacent site
        m.update(lra=_u(b, a, 8), flags=_u(b, a + 8, 4), sysid=_u(b, a + 12, 12),
                 rfss=_u(b, a + 24, 8), site=_u(b, a + 32, 8), channel=_u(b, a + 40, 16),
                 svc_class=_u(b, a + 56, 8))
    elif op == 0x39:                                  # secondary control channels
        m.update(rfss=_u(b, a, 8), site=_u(b, a + 8, 8), channel=_u(b, a + 16, 16),
                 channel_b=_u(b, a + 40, 16))
    return m


class IdenTable:
    """Channel identifier -> frequency plan, learned from IDEN_UP broadcasts."""

    def __init__(self):
        self.idens = {}

    def update(self, m: dict):
        if "iden" in m and "base" in m:
            self.idens[m["iden"]] = m

    def freq(self, channel: int):
        i = self.idens.get((channel >> 12) & 0xF)
        if i is None:
            return None
        n = channel & 0xFFF
        return i["base"] + (n // i.get("slots", 1)) * i["spacing"]

    def slot(self, channel: int):
        i = self.idens.get((channel >> 12) & 0xF)
        if i is None or i.get("slots", 1) == 1:
            return None
        return (channel & 0xFFF) % i["slots"]

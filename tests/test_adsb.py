"""
ADS-B decoder tests: published reference messages ("The 1090 MHz Riddle")
plus a synthetic 2 MS/s PPM signal through the demodulator.

    python -m tests.test_adsb
"""

import numpy as np

from smashdr.decoders import adsb

IDENT = bytes.fromhex("8D4840D6202CC371C32CE0576098")      # KLM1023
POS_EVEN = bytes.fromhex("8D40621D58C382D690C8AC2863A7")   # 38000 ft, 52.2572 N 3.9194 E
VEL = bytes.fromhex("8D485020994409940838175B284F")        # 159 kt, 182.88 deg, -832 fpm


def test_decode():
    ok = True
    for m in (IDENT, POS_EVEN, VEL):
        ok &= adsb.crc24(m) == 0
    cs = adsb.callsign(IDENT)
    alt = adsb.altitude(POS_EVEN)
    lat, lon = adsb.cpr_local(*adsb.cpr(POS_EVEN), 52.258, 3.918)
    gs, trk, vr = adsb.velocity(VEL)
    print(f"crc ok {ok}, callsign {cs!r}, alt {alt}, pos {lat:.5f} {lon:.5f}, "
          f"gs {gs:.2f} trk {trk:.2f} vr {vr}")
    ok &= cs == "KLM1023" and alt == 38000
    ok &= abs(lat - 52.25720) < 1e-4 and abs(lon - 3.91937) < 1e-4
    ok &= abs(gs - 159.20) < 0.05 and abs(trk - 182.88) < 0.05 and vr == -832
    print("decode:", "PASS" if ok else "FAIL")
    assert ok


def modulate(msgs, snr_db=15.0, gap=3000, seed=1):
    """PPM at 2 samples/us, random carrier phase per message, complex noise."""
    rng = np.random.default_rng(seed)
    parts = []
    for m in msgs:
        bits = np.unpackbits(np.frombuffer(m, dtype=np.uint8))
        amp = np.zeros(adsb.PREAMBLE + 2 * len(bits))
        amp[[0, 2, 7, 9]] = 1.0
        for i, b in enumerate(bits):
            amp[adsb.PREAMBLE + 2 * i + (0 if b else 1)] = 1.0
        ph = np.exp(1j * rng.uniform(0, 2 * np.pi))
        parts += [np.zeros(gap + int(rng.integers(0, 50))), amp * ph]
    sig = np.concatenate(parts + [np.zeros(gap)])
    npow = 10 ** (-snr_db / 10)
    sig = sig + np.sqrt(npow / 2) * (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig)))
    return sig.astype(np.complex64)


def test_demod():
    msgs = [IDENT, POS_EVEN, VEL] * 30
    for snr in (20, 12, 9):
        iq = modulate(msgs, snr_db=snr)
        rx = adsb.AdsbDemod()
        got = []
        for i in range(0, len(iq), 50_000):           # odd block size: messages straddle blocks
            got += rx.process(iq[i:i + 50_000])
        right = sum(g in (IDENT, POS_EVEN, VEL) for g in got)
        print(f"SNR {snr:2d} dB: {right}/{len(msgs)} messages, {len(got) - right} wrong")
        assert len(got) == right
        if snr >= 12:
            assert right >= 0.95 * len(msgs)


def test_tracker():
    tr = adsb.Tracker(52.258, 3.918)
    for t, m in enumerate((IDENT, POS_EVEN, VEL)):
        p = tr.update(m, float(t))
    # different ICAOs in the reference vectors; check the position one
    p = tr.planes["40621D"]
    ok = abs(p["lat"] - 52.2572) < 1e-3 and p["alt"] == 38000
    print("tracker:", "PASS" if ok else "FAIL", len(tr.planes), "aircraft")
    assert ok


def _with_parity(body: bytes, addr: int = 0) -> bytes:
    """Append CRC (xor address for DF4/5/20/21) to a message body."""
    rem = adsb.crc24(body)
    return body + (rem ^ addr).to_bytes(3, "big")


def _ac13(alt_ft):
    n = (alt_ft + 1000) // 25
    # put the 11-bit N around M (bit 7) = 0 and Q (bit 9) = 1
    hi6, b8, lo4 = (n >> 5) & 0x3F, (n >> 4) & 1, n & 0xF
    return (hi6 << 7) | (b8 << 5) | (1 << 4) | lo4


def _sq13(code: str):
    a, b, c, d = (int(x) for x in code)
    bits = [c & 1, a & 1, (c >> 1) & 1, (a >> 1) & 1, (c >> 2) & 1, (a >> 2) & 1, 0,
            b & 1, d & 1, (b >> 1) & 1, (d >> 1) & 1, (b >> 2) & 1, (d >> 2) & 1]
    v = 0
    for x in bits:
        v = (v << 1) | x
    return v


def test_modes():
    addr = 0xA12345
    df11 = _with_parity(bytes([11 << 3 | 5]) + addr.to_bytes(3, "big"))
    first = (4 << 3) | 0b000                       # DF4, FS=0
    v = (first << 24) | (0 << 19) | (_ac13(3300))  # DR/UM zero, AC13 in bits 20-32
    df4 = _with_parity(v.to_bytes(4, "big"), addr)
    v5 = ((5 << 3) << 24) | _sq13("1200")
    df5 = _with_parity(v5.to_bytes(4, "big"), addr)
    stranger = _with_parity(v.to_bytes(4, "big"), 0xABCDEF)    # never announced
    assert adsb.icao(df4) == "A12345" and adsb.ac13_altitude(df4) == 3300
    assert adsb.id13_squawk(df5) == "1200"
    iq = modulate([df11, df4, df5, stranger, df4] * 10, snr_db=18)
    rx = adsb.AdsbDemod()
    got = []
    for i in range(0, len(iq), 40_000):
        got += rx.process(iq[i:i + 40_000])
    kinds = [adsb.df_of(g) for g in got]
    print(f"mode S: DF11 {kinds.count(11)}, DF4 {kinds.count(4)}, DF5 {kinds.count(5)}, "
          f"stranger accepted {sum(g == stranger for g in got)}")
    assert sum(g == stranger for g in got) == 0 and kinds.count(4) >= 15 and kinds.count(5) >= 8
    tr = adsb.Tracker(34.0, -118.0)
    for t, g in enumerate(got):
        tr.update(g, float(t))
    p = tr.planes["A12345"]
    print("tracker mode S:", p["alt"], p["squawk"], "adsb" if p["adsb"] else "transponder only")
    assert p["alt"] == 3300 and p["squawk"] == "1200" and not p["adsb"]


def test_commb():
    # pyModeS reference vectors (DF20 Comm-B)
    want = {"A000083E202CC371C31DE0AA1CCF": ("2,0", "callsign", "KLM1017"),
            "A000029C85E42F313000007047D3": ("4,0", "sel_alt", 3000),
            "A000139381951536E024D4CCF6B5": ("5,0", "gs", 438),
            "A00004128F39F91A7E27C46ADC21": ("6,0", "ias", 252)}
    for h, (bds, key, val) in want.items():
        r = adsb.commb(bytes.fromhex(h))
        assert r and r[0] == bds and r[1][key] == val, (h, r)
    r = adsb.commb(bytes.fromhex("A000139381951536E024D4CCF6B5"))[1]
    assert abs(r["track"] - 114.258) < 0.01 and r["tas"] == 424 and abs(r["roll"] - 2.1) < 0.1
    r = adsb.commb(bytes.fromhex("A00004128F39F91A7E27C46ADC21"))[1]
    assert abs(r["heading"] - 42.715) < 0.01 and abs(r["mach"] - 0.42) < 0.005 and r["vr_baro"] == -1920
    for h in ("A0001692185BD5CF400000DFC696", "A800178D10010080F50000D5893C"):   # BDS 4,4 / 1,0
        assert adsb.commb(bytes.fromhex(h)) is None
    # a BDS 5,0 that disagrees with the plane's ADS-B track is rejected
    assert adsb.commb(bytes.fromhex("A000139381951536E024D4CCF6B5"), {"gs": 440, "track": 300}) is None
    print("comm-B: PASS")


if __name__ == "__main__":
    test_decode()
    test_demod()
    test_tracker()
    test_modes()
    test_commb()

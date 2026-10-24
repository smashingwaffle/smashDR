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


if __name__ == "__main__":
    test_decode()
    test_demod()
    test_tracker()

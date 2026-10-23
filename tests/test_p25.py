"""
Offline tests for the P25 Phase 1 chain. Run from the repo root:
    python -m tests.test_p25
"""

import time

import numpy as np

import p25
from tests.p25_synth import make_signal


def test_bch():
    rng = np.random.default_rng(0)
    for _ in range(200):
        nac, duid = int(rng.integers(0, 4096)), int(rng.choice(list(p25.DUID_NAMES)))
        nid = p25.nid_encode(nac, duid)
        nerr = int(rng.integers(0, 12))
        flips = rng.choice(np.arange(1, 64), nerr, replace=False)   # never the parity bit
        bad = nid
        for b in flips:
            bad ^= 1 << int(b)
        got = p25.nid_decode(bad)
        assert got is not None and got[:2] == (nac, duid) and got[2] == nerr, (nac, duid, nerr, got)
    print("BCH(63,16): 200 random NIDs with 0-11 bit errors all corrected")


def run_chain(snr_db, clock_ppm=80.0, n_frames=20, rate=960_000, offset=-192_000):
    sig, duids = make_signal(n_frames=n_frames, snr_db=snr_db, clock_ppm=clock_ppm,
                             rate=rate, offset_hz=offset)
    rx = p25.P25Phase1Receiver(rate, offset)
    t0 = time.time()
    block = 262144
    for i in range(0, len(sig), block):
        rx.process(sig[i:i + block])
    dt = time.time() - t0
    frames = rx.framer.frames
    ok = [f for f in frames if f["nac"] == 0x466]
    return frames, ok, duids, dt, len(sig) / rate


def test_chain():
    for snr, ppm, rate, off in ((30, 0, 960_000, -192_000), (20, 80, 960_000, -192_000),
                                (12, -120, 2_400_000, -300_000), (8, 80, 960_000, -192_000)):
        frames, ok, duids, dt, secs = run_chain(snr, ppm, rate=rate, offset=off)
        from tests.p25_synth import FRAME_DIBITS
        # match each decoded frame to the transmitted one by its position in the stream
        right = sum(1 for f in ok
                    if duids[min(len(duids) - 1, round(f["symbol"] / FRAME_DIBITS))] == f["duid"])
        bch = [f["bch_errors"] for f in ok]
        print(f"SNR {snr:2d} dB, clock {ppm:+4d} ppm, {rate / 1e6:.2f} MS/s: "
              f"{len(ok)}/{len(duids)} frames, NAC 0x466 on all: {len(ok) == len(frames)}, "
              f"DUIDs right {right}/{len(ok)}, NID bit errors max {max(bch) if bch else '-'}, "
              f"sync level {np.mean([f['sync_level'] for f in ok]) if ok else 0:.2f} (ideal 3), "
              f"CPU {dt / secs * 100:.0f}% of real time")


def test_calls():
    """Two transmissions with silence around them: a clear call and an AES call."""
    from tests import p25_synth as sy
    rng = np.random.default_rng(7)
    iq = np.concatenate([
        sy.modulate(sy.build_call(0x466, 1001, 1234567, 0x80, 3, rng), seed=1),
        sy.modulate(sy.build_call(0x466, 2002, 7654321, 0x84, 2, rng, kid=0x1234), seed=2),
    ])
    rx = p25.P25Phase1Receiver(960_000, -192_000)
    for i in range(0, len(iq), 262144):
        rx.process(iq[i:i + 262144])
    frames = rx.framer.frames
    calls = p25.group_calls(frames)
    hdus = [f for f in frames if f["duid"] == 0]
    print(f"calls: {len(calls)} (want 2), HDUs decoded {sum(f.get('rs_errors', -1) == 0 for f in hdus)}/2")
    for c in calls:
        print(f"  tg {c['tgid']} src {c['src']} {p25.algid_name(c['algid'])} "
              f"encrypted={c['encrypted']} voice frames {len(c['imbe'])} hdu={c['hdu']}")
    ok = (len(calls) == 2 and calls[0]["tgid"] == 1001 and calls[0]["src"] == 1234567
          and not calls[0]["encrypted"] and len(calls[0]["imbe"]) == 54
          and calls[1]["tgid"] == 2002 and calls[1]["encrypted"] and calls[1]["algid"] == 0x84
          and calls[1]["imbe"] == [] and all(c["hdu"] for c in calls))
    print("call round-trip:", "PASS" if ok else "FAIL")
    assert ok


if __name__ == "__main__":
    test_bch()
    test_chain()
    test_calls()

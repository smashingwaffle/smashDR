"""
Synthetic P25 Phase 1 (C4FM) signal generator for testing the receive chain.

Frames are LDU1/LDU2-sized (864 dibits incl. status), each starting with the
frame sync and a valid BCH-encoded NID; the payload is random. The signal is
FM-modulated at the given IQ rate with offset, symbol-clock error and noise.
"""

import numpy as np

import p25

FRAME_DIBITS = 864            # LDU length incl. status dibits


def frame_dibits(nac: int, duid: int, rng) -> np.ndarray:
    nid = p25.nid_encode(nac, duid)
    nid_d = [(nid >> (62 - 2 * i)) & 3 for i in range(32)]
    data = list(p25.SYNC_DIBITS) + nid_d
    data += list(rng.integers(0, 4, FRAME_DIBITS))          # payload (over-long, trimmed)
    out = []
    k = 0
    while len(out) < FRAME_DIBITS:
        if len(out) % p25.STATUS_PERIOD == p25.STATUS_PERIOD - 1:
            out.append(int(rng.integers(0, 4)))              # status dibit
        else:
            out.append(int(data[k]))
            k += 1
    return np.array(out, dtype=np.uint8)


def make_signal(n_frames=20, nac=0x466, rate=960_000, offset_hz=-192_000,
                clock_ppm=80.0, snr_db=25.0, carrier_err_hz=150.0, seed=1):
    rng = np.random.default_rng(seed)
    duids = [0x5 if i % 2 == 0 else 0xA for i in range(n_frames)]
    dib = np.concatenate([frame_dibits(nac, d, rng) for d in duids])
    syms = np.array([p25.DIBIT_TO_SYM[int(x)] for x in dib])

    sps = 10
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    h = p25.c4fm_tx_filter(sps)
    # scale so the end-to-end pulse (tx filter + receiver's 1-symbol boxcar) peaks at 1,
    # i.e. symbol instants sit at +-600 / +-1800 Hz deviation
    peak = np.convolve(h, np.full(sps, 1.0 / sps)).max()
    freq_units = np.convolve(up, h / peak, mode="same")   # symbol units at 48 kHz
    sym_rate = p25.SYMBOL_RATE * (1 + clock_ppm * 1e-6)
    t48 = np.arange(len(freq_units)) / (sym_rate * sps)
    t_rf = np.arange(0, t48[-1], 1 / rate)
    f_hz = np.interp(t_rf, t48, freq_units) * p25.DEV_PER_LEVEL + carrier_err_hz
    phase = 2 * np.pi * np.cumsum(f_hz + offset_hz) / rate
    sig = np.exp(1j * phase)
    noise_power = 10 ** (-snr_db / 10) * (rate / 12_500)   # SNR in a 12.5 kHz channel
    sig = sig + np.sqrt(noise_power / 2) * (rng.standard_normal(len(sig)) +
                                            1j * rng.standard_normal(len(sig)))
    sig *= 0.25
    return sig.astype(np.complex64), duids

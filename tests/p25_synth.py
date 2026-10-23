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


# --------------------------------------------------------------------------
# Full-frame encoders (valid FEC) for round-trip tests
# --------------------------------------------------------------------------
import p25_fec as pf  # noqa: E402


def _put(bits, start, value, nbits):
    for i in range(nbits):
        bits[start + i] = (value >> (nbits - 1 - i)) & 1


def _to_frame_dibits(bits, total_dibits):
    """Data bits -> frame dibits with (random) status dibits inserted."""
    dib = []
    k = 0
    rng = np.random.default_rng(len(bits))
    for i in range(total_dibits):
        if i % p25.STATUS_PERIOD == p25.STATUS_PERIOD - 1:
            dib.append(int(rng.integers(0, 4)))
        elif k + 1 < len(bits):
            dib.append((int(bits[k]) << 1) | int(bits[k + 1]))
            k += 2
        else:
            dib.append(0)
    return np.array(dib, dtype=np.uint8)


def _header_bits(nac, duid, nbits):
    bits = np.zeros(nbits, dtype=np.uint8)
    _put(bits, 0, p25.FRAME_SYNC, 48)
    _put(bits, 48, p25.nid_encode(nac, duid), 64)
    return bits


def build_hdu(nac, tgid, algid=0x80, kid=0, mfid=0):
    raw = bytes(9) + bytes([mfid, algid]) + kid.to_bytes(2, "big") + tgid.to_bytes(2, "big")
    hb = pf.rs_encode(pf.bytes_to_hexbits(raw), 36)
    bits = _header_bits(nac, 0x0, 2 * 385)
    for i, h in enumerate(hb):
        _put(bits, p25.HDU_START + 18 * i, pf.golay24_encode(h) & 0x3FFFF, 18)
    return _to_frame_dibits(bits, p25.FRAME_DIBITS[0x0])


def build_ldu(nac, duid, hexbits24, rng):
    bits = _header_bits(nac, duid, 1680)
    for st in p25.VOICE_STARTS:
        cw = rng.integers(0, 2, 144).astype(np.uint8)
        bits[st + p25.IMBE_PERM] = cw
    for i, h in enumerate(hexbits24):
        blk, w = divmod(i, 4)
        _put(bits, p25.LC_STARTS[blk] + 10 * w, pf.hamming1063_encode(h), 10)
    return _to_frame_dibits(bits, 864)


def build_ldu1(nac, tgid, src, rng):
    lc = bytes([0x00, 0x00, 0x00, 0x00]) + tgid.to_bytes(2, "big") + src.to_bytes(3, "big")
    return build_ldu(nac, 0x5, pf.rs_encode(pf.bytes_to_hexbits(lc), 24), rng)


def build_ldu2(nac, algid, kid, rng):
    es = bytes(9) + bytes([algid]) + kid.to_bytes(2, "big")
    return build_ldu(nac, 0xA, pf.rs_encode(pf.bytes_to_hexbits(es), 24), rng)


def build_tdu(nac):
    return _to_frame_dibits(_header_bits(nac, 0x3, 140), 72)


def build_call(nac, tgid, src, algid, n_ldu_pairs, rng, kid=0):
    parts = [build_hdu(nac, tgid, algid, kid)]
    for _ in range(n_ldu_pairs):
        parts.append(build_ldu1(nac, tgid, src, rng))
        parts.append(build_ldu2(nac, algid, kid, rng))
    parts.append(build_tdu(nac))
    return np.concatenate(parts)


def modulate(dibits, rate=960_000, offset_hz=-192_000, clock_ppm=50.0, snr_db=20.0,
             carrier_err_hz=120.0, lead_s=0.3, seed=3):
    """C4FM-modulate one transmission, with noise-only lead-in and tail."""
    rng = np.random.default_rng(seed)
    syms = np.array([p25.DIBIT_TO_SYM[int(x)] for x in dibits])
    sps = 10
    up = np.zeros(len(syms) * sps)
    up[::sps] = syms
    h = p25.c4fm_tx_filter(sps)
    peak = np.convolve(h, np.full(sps, 1.0 / sps)).max()
    fu = np.convolve(up, h / peak, mode="same")
    t48 = np.arange(len(fu)) / (p25.SYMBOL_RATE * (1 + clock_ppm * 1e-6) * sps)
    t_rf = np.arange(0, t48[-1], 1 / rate)
    f_hz = np.interp(t_rf, t48, fu) * p25.DEV_PER_LEVEL + carrier_err_hz
    sig = np.exp(1j * 2 * np.pi * np.cumsum(f_hz + offset_hz) / rate)
    lead = np.zeros(int(lead_s * rate), dtype=complex)
    sig = np.concatenate([lead, sig, lead])
    npow = 10 ** (-snr_db / 10) * (rate / 12_500)
    sig = sig + np.sqrt(npow / 2) * (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig)))
    return (0.25 * sig).astype(np.complex64)

"""
eq.py - listening equalizer for the 48 kHz audio output (no Qt, numpy only).

    eq = Equalizer()
    eq.set_gains([0, -6, 0, 3, 4, -3])     # dB at BANDS
    pcm_out = eq.process(pcm_int16_bytes)

The band gains are joined into a smooth curve (linear in dB over log
frequency, flat beyond the outer bands) and turned into a linear-phase FIR
(1023 taps, about 10 ms delay), applied with FFT overlap-add so it costs
almost nothing. Flat = bypass (no delay, bit-exact).
"""

import numpy as np

RATE = 48_000
BANDS = (100, 250, 500, 1000, 2000, 4000)          # Hz
TAPS = 1023
MAX_DB = 12.0

PRESETS = {
    "Flat":           (0, 0, 0, 0, 0, 0),
    "Voice":          (-8, -3, 0, 2, 3, 0),          # trim rumble, lift intelligibility
    "Clarity":        (-6, -2, -1, 3, 5, 2),
    "Cut hiss":       (0, 0, 0, 0, -3, -10),
    "Bass cut":       (-12, -8, -3, 0, 0, 0),
    "Warm (AM air)":  (2, 3, 1, 0, -2, -6),
}


def design(gains_db, taps=TAPS, rate=RATE):
    """Linear-phase FIR for the EQ curve."""
    n_fft = 8192
    f = np.fft.rfftfreq(n_fft, 1 / rate)
    lf = np.log10(np.clip(f, BANDS[0], BANDS[-1]))
    curve_db = np.interp(lf, np.log10(BANDS), np.clip(gains_db, -MAX_DB, MAX_DB))
    mag = 10 ** (curve_db / 20)
    h = np.fft.irfft(mag, n_fft)                    # zero phase, centred at 0
    h = np.roll(h, taps // 2)[:taps] * np.hanning(taps)
    return h.astype(np.float64)


class Equalizer:
    def __init__(self):
        self.gains = [0.0] * len(BANDS)
        self.enabled = True
        self._h = None
        self._tail = np.zeros(0)
        self._H = {}

    def set_gains(self, gains_db):
        g = [float(max(-MAX_DB, min(MAX_DB, x))) for x in gains_db]
        if g == self.gains and self._h is not None:
            return
        self.gains = g
        self._H = {}
        self._h = None if self.flat else design(g)
        self._tail = np.zeros(0)

    @property
    def flat(self):
        return all(abs(x) < 0.05 for x in self.gains)

    def active(self):
        return self.enabled and self._h is not None

    def process(self, pcm: bytes) -> bytes:
        if not self.active() or not pcm:
            self._tail = np.zeros(0)
            return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        n = len(x)
        m = n + TAPS - 1
        nfft = 1 << (m - 1).bit_length()
        H = self._H.get(nfft)
        if H is None:
            H = self._H[nfft] = np.fft.rfft(self._h, nfft)
        y = np.fft.irfft(np.fft.rfft(x, nfft) * H, nfft)[:m]
        t = self._tail
        y[:len(t)] += t[:m] if len(t) > m else t
        self._tail = y[n:].copy()
        if len(t) > m:                                  # (only with very short blocks)
            self._tail[:len(t) - m] += t[m:]
        return np.clip(np.round(y[:n]), -32768, 32767).astype(np.int16).tobytes()

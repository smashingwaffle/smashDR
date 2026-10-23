"""
dsp.py - signal processing building blocks for smashDR.

Everything here is block-based with state carried between calls, so it
works the same on live SDR blocks and on recorded files.
"""

import numpy as np

FFT_SIZE = 2048
IF_RATE = 240_000        # first decimation target (all supported SDR rates are multiples)
AUDIO_RATE = 48_000      # IF_RATE / 5

_WIN = np.hanning(FFT_SIZE).astype(np.float32)
_WIN_NORM = float(_WIN.sum() ** 2)


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------
def cu8_to_iq(raw: bytes) -> np.ndarray:
    """Interleaved unsigned 8-bit IQ -> complex64, roughly -1..+1."""
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    a -= 127.4
    a *= 1.0 / 128.0
    return a.view(np.complex64)


def power_spectrum_db(iq: np.ndarray) -> np.ndarray:
    """Averaged, fft-shifted power spectrum in dBFS."""
    k = len(iq) // FFT_SIZE
    blk = iq[: k * FFT_SIZE].reshape(k, FFT_SIZE) * _WIN
    p = np.abs(np.fft.fft(blk, axis=1)) ** 2
    p = np.fft.fftshift(p.mean(axis=0)) / _WIN_NORM
    return 10.0 * np.log10(p + 1e-20)


def lowpass_taps(num: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc lowpass. cutoff in cycles/sample (0..0.5)."""
    n = np.arange(num) - (num - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.hamming(num)
    return (h / h.sum()).astype(np.float32)


def deemphasis_taps(tau_s: float, fs: float, num: int = 256, ref_hz: float = 1000.0) -> np.ndarray:
    """
    FM de-emphasis (one-pole lowpass, corner 1/(2*pi*tau)) as a truncated FIR,
    normalised to unity gain at ref_hz so levels stay comparable.
    """
    a = 1.0 - np.exp(-1.0 / (fs * tau_s))
    h = a * (1.0 - a) ** np.arange(num)
    w = 2 * np.pi * ref_hz / fs
    g = abs(np.sum(h * np.exp(-1j * w * np.arange(num))))
    return (h / g).astype(np.float32)


class StreamFir:
    """FIR filter that keeps its history between blocks (any taps, causal)."""

    def __init__(self, taps: np.ndarray, dtype):
        self.taps = taps
        self.hist = np.zeros(len(taps) - 1, dtype=dtype)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = np.concatenate([self.hist, x])
        self.hist = y[len(y) - (len(self.taps) - 1):]
        return np.convolve(y, self.taps, mode="valid").astype(x.dtype, copy=False)


# --------------------------------------------------------------------------
# NFM audio monitor
# --------------------------------------------------------------------------
class NfmMonitor:
    """
    Pull one 12.5 kHz channel out of the wideband IQ and FM-demodulate it.

    rate (2.4/1.92/1.2/0.96 MS/s)
      -> mix channel to DC
      -> boxcar decimate to 240 kHz (nulls land on the alias bands)
      -> 201-tap channel filter, decimate by 5 -> 48 kHz
      -> FM discriminator
      -> 300-3400 Hz audio bandpass (kills DC, CTCSS tones, hiss)
      -> 750 us de-emphasis (standard land-mobile NFM)
      -> int16 PCM, 48 kHz mono
    """

    DEEMPH_TAU = 750e-6

    DEV_SCALE = 0.8 / (2 * np.pi * 3000 / AUDIO_RATE)   # 3 kHz deviation -> 0.8 FS

    def __init__(self):
        self.rate = None

    def configure(self, rate: float, offset_hz: float):
        d1 = rate / IF_RATE
        if abs(d1 - round(d1)) > 1e-6:
            raise ValueError(f"sample rate {rate:.0f} is not a multiple of {IF_RATE}")
        self.rate = rate
        self.d1 = int(round(d1))
        self.d2 = IF_RATE // AUDIO_RATE
        self.rem1 = np.zeros(0, dtype=np.complex64)
        self.chan = StreamFir(lowpass_taps(201, 7000 / IF_RATE), np.complex64)
        self.k2 = 0
        self.last = np.complex64(1)
        bp = lowpass_taps(1023, 3400 / AUDIO_RATE) - lowpass_taps(1023, 300 / AUDIO_RATE)
        self.audio_fir = StreamFir(bp.astype(np.float32), np.float32)
        self.deemph = StreamFir(deemphasis_taps(self.DEEMPH_TAU, AUDIO_RATE), np.float32)
        self.phase = 0.0
        self.set_offset(offset_hz)

    def set_offset(self, offset_hz: float):
        self.offset = offset_hz
        self.phase_inc = -2 * np.pi * offset_hz / self.rate

    def process(self, iq: np.ndarray, open_: bool = True) -> np.ndarray:
        n = len(iq)
        ph = self.phase + self.phase_inc * np.arange(n)
        self.phase = float((self.phase + self.phase_inc * n) % (2 * np.pi))
        x = iq * np.exp(1j * ph).astype(np.complex64)

        x = np.concatenate([self.rem1, x])
        m = len(x) // self.d1 * self.d1
        self.rem1 = x[m:]
        x = x[:m].reshape(-1, self.d1).mean(axis=1).astype(np.complex64)

        f = self.chan(x)
        out = f[self.k2::self.d2]
        self.k2 = (self.k2 - len(f)) % self.d2
        if len(out) == 0:
            return np.zeros(0, dtype=np.int16)

        z = np.concatenate([[self.last], out])
        self.last = out[-1]
        d = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32) * np.float32(self.DEV_SCALE)
        a = self.deemph(self.audio_fir(d))
        if not open_:
            a = np.zeros_like(a)
        return (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)

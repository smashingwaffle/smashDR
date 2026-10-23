"""
dsp.py - signal processing building blocks for smashDR.

Everything is block-based with state carried between calls, so it works the
same on live SDR blocks and on recorded files.

    Channelizer   wideband IQ -> one channel at DC, filtered and decimated
                  (NCO mix + FFT overlap-save decimating filter)
    Demod         Channelizer + NFM / AM / WFM demodulation -> 48 kHz int16
"""

import numpy as np

FFT_SIZE = 2048
AUDIO_RATE = 48_000

_WIN = np.hanning(FFT_SIZE).astype(np.float32)
_WIN_NORM = float(_WIN.sum() ** 2)

# Channel filter half-widths and channelizer output rates per mode.
# NFM is +-8 kHz so 25 kHz-spaced ham FM (5 kHz dev) fits as well as 12.5 kHz.
MODES = {
    "NFM": {"half_bw": 8_000.0, "chan_rate": 48_000},
    "AM": {"half_bw": 5_000.0, "chan_rate": 48_000},
    "WFM": {"half_bw": 100_000.0, "chan_rate": 240_000},
    # digital: handled by p25_live.P25Live, not by Demod
    "P25": {"half_bw": 6_250.0, "chan_rate": 48_000},
}
DIGITAL_MODES = {"P25"}


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
        if len(self.taps) == 1:
            return (x * self.taps[0]).astype(x.dtype, copy=False)
        y = np.concatenate([self.hist, x])
        self.hist = y[len(y) - (len(self.taps) - 1):]
        return np.convolve(y, self.taps, mode="valid").astype(x.dtype, copy=False)


class StreamDecimator:
    """Keep every d-th sample with phase continuity across blocks."""

    def __init__(self, d: int):
        self.d = d
        self.k = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        out = x[self.k::self.d]
        self.k = (self.k - len(x)) % self.d
        return out


# --------------------------------------------------------------------------
# Channelizer
# --------------------------------------------------------------------------
class Channelizer:
    """
    Pull one channel out of wideband IQ:
      NCO mix (channel -> DC)  ->  FFT overlap-save lowpass + decimation.

    The filter is designed at the input rate with N/4 taps, so it is very
    sharp (a few hundred Hz transition at 2.4 MS/s for audio modes), and the
    decimation happens by folding the spectrum before a short IFFT.
    """

    def __init__(self, rate: float, out_rate: float, half_bw: float, offset_hz: float):
        d = rate / out_rate
        if abs(d - round(d)) > 1e-6:
            raise ValueError(f"sample rate {rate:.0f} is not a multiple of {out_rate:.0f}")
        self.rate = float(rate)
        self.out_rate = float(out_rate)
        self.D = int(round(d))
        self.n_out = 1024 if self.D >= 20 else 4096
        self.N = self.D * self.n_out
        self.V = self.N // 4                      # overlap, multiple of D
        self.hop = self.N - self.V
        h = lowpass_taps(self.V + 1, half_bw / rate)
        self.H = np.fft.fft(h, self.N).astype(np.complex64)
        self.hist = np.zeros(self.V, dtype=np.complex64)
        self.pending = np.zeros(0, dtype=np.complex64)
        self.phase = 0.0
        self.set_offset(offset_hz)

    def set_offset(self, offset_hz: float):
        self.offset = float(offset_hz)
        self.phase_inc = -2 * np.pi * self.offset / self.rate
        self._rot = None                    # mixer table, rebuilt lazily

    def _mixer(self, n: int) -> np.ndarray:
        """exp(j*(phase + inc*k)) for k < n: cached per-sample rotation x one start phasor."""
        if self._rot is None or len(self._rot) < n:
            self._rot = np.exp(1j * self.phase_inc * np.arange(max(n, 262144))).astype(np.complex64)
        start = np.complex64(np.exp(1j * self.phase))
        self.phase = float((self.phase + self.phase_inc * n) % (2 * np.pi))
        return self._rot[:n] * start

    def process(self, iq: np.ndarray) -> np.ndarray:
        n = len(iq)
        x = np.concatenate([self.pending, iq * self._mixer(n)])

        outs = []
        skip = self.V // self.D
        pos = 0
        while len(x) - pos >= self.hop:
            seg = np.concatenate([self.hist, x[pos:pos + self.hop]])
            self.hist = seg[-self.V:]
            pos += self.hop
            Y = np.fft.fft(seg) * self.H
            Yd = Y.reshape(self.D, self.n_out).sum(axis=0) / self.D
            outs.append(np.fft.ifft(Yd)[skip:])
        self.pending = x[pos:]
        if not outs:
            return np.zeros(0, dtype=np.complex64)
        return np.concatenate(outs).astype(np.complex64)


# --------------------------------------------------------------------------
# Demodulator
# --------------------------------------------------------------------------
class Demod:
    """
    Channelizer + demodulation for NFM, AM and WFM, output 48 kHz int16.

    NFM: discriminator -> 300 (or 100) - 3400 Hz bandpass -> de-emphasis
    AM:  envelope -> AGC on carrier level -> 200 - 4500 Hz bandpass
    WFM: discriminator at 240 kHz -> 15 kHz mono lowpass -> de-emphasis -> /5
    All: warm-up mute, ramped squelch gate, soft limiter.
    """

    WARMUP_SEC = 0.08
    RAMP_SEC = 0.01
    LIMIT_KNEE = 0.7

    def __init__(self):
        self.mode = "NFM"
        self.nfm_deemph_us = 750
        self.wfm_deemph_us = 75
        self.ctcss_filter = True
        self.rate = None
        self.offset = 0.0
        self.chan = None
        self.gate = 0.0
        self.warm_left = 0
        self.last_out = 0.0       # last sample emitted (for click-free switches)
        self.fade_from = None

    # ---- configuration ----
    def configure(self, rate: float, offset_hz: float, mode: str = None):
        if mode:
            self.mode = mode
        if self.mode in DIGITAL_MODES:
            raise ValueError(f"{self.mode} is decoded by its own receiver, not Demod")
        self.rate = float(rate)
        self.offset = float(offset_hz)
        m = MODES[self.mode]
        self.chan = Channelizer(self.rate, m["chan_rate"], m["half_bw"], self.offset)
        self._build_post()

    def set_mode(self, mode: str):
        if mode != self.mode and self.rate:
            self.configure(self.rate, self.offset, mode)
        else:
            self.mode = mode

    def set_options(self, nfm_deemph_us=None, wfm_deemph_us=None, ctcss_filter=None):
        if nfm_deemph_us is not None:
            self.nfm_deemph_us = nfm_deemph_us
        if wfm_deemph_us is not None:
            self.wfm_deemph_us = wfm_deemph_us
        if ctcss_filter is not None:
            self.ctcss_filter = ctcss_filter
        if self.chan is not None:
            self._build_post()

    def set_offset(self, offset_hz: float):
        self.offset = float(offset_hz)
        if self.chan is not None:
            self.chan.set_offset(offset_hz)

    def mute_for(self, sec: float):
        """Silence output for a while (e.g. after a retune), then fade back in."""
        self.warm_left = max(self.warm_left, int(sec * AUDIO_RATE))

    def _build_post(self):
        fs = AUDIO_RATE
        self.last = None
        self.warm_left = int(self.WARMUP_SEC * fs)
        # New filters start cold: fade from the last emitted sample to silence,
        # stay muted through warm-up, then the gate ramps the new chain in.
        self.fade_from = self.last_out
        self.gate = 0.0
        if self.mode == "NFM":
            hp = 300 if self.ctcss_filter else 100
            bp = lowpass_taps(1023, 3400 / fs) - lowpass_taps(1023, hp / fs)
            self.audio_fir = StreamFir(bp.astype(np.float32), np.float32)
            self.deemph = (StreamFir(deemphasis_taps(self.nfm_deemph_us * 1e-6, fs), np.float32)
                           if self.nfm_deemph_us else None)
            # 3 kHz deviation at 1 kHz -> 0.3 FS (de-emphasis lifts lows ~+9 dB)
            self.scale = 0.3 / (2 * np.pi * 3000 / fs)
        elif self.mode == "AM":
            bp = lowpass_taps(1023, 4500 / fs) - lowpass_taps(1023, 200 / fs)
            self.audio_fir = StreamFir(bp.astype(np.float32), np.float32)
            self.deemph = None
            self.carrier = None
        else:  # WFM
            cr = MODES["WFM"]["chan_rate"]
            self.audio_fir = StreamFir(lowpass_taps(255, 15_000 / cr), np.float32)
            self.deemph = (StreamFir(deemphasis_taps(self.wfm_deemph_us * 1e-6, cr), np.float32)
                           if self.wfm_deemph_us else None)
            self.decim = StreamDecimator(cr // fs)
            # 50 kHz deviation -> 0.35 FS
            self.scale = 0.35 / (2 * np.pi * 50_000 / cr)

    # ---- processing ----
    def process(self, iq: np.ndarray, open_: bool = True) -> np.ndarray:
        y = self.chan.process(iq)
        if len(y) == 0:
            return np.zeros(0, dtype=np.int16)

        if self.mode in ("NFM", "WFM"):
            if self.last is None:
                self.last = y[0]
            z = np.concatenate([[self.last], y])
            self.last = y[-1]
            a = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32) * np.float32(self.scale)
            a = self.audio_fir(a)
            if self.deemph is not None:
                a = self.deemph(a)
            if self.mode == "WFM":
                a = self.decim(a)
        else:  # AM
            env = np.abs(y).astype(np.float32)
            car = float(env.mean()) + 1e-9
            prev = self.carrier if self.carrier is not None else car
            new = 0.8 * prev + 0.2 * car             # ~0.5 s AGC at ~110 ms blocks
            self.carrier = new
            agc = np.linspace(prev, new, len(env), dtype=np.float32)   # no gain steps
            a = self.audio_fir((env - agc) / agc * np.float32(0.4))

        if len(a) == 0:
            return np.zeros(0, dtype=np.int16)
        a = a * self._gate_curve(len(a), open_)
        if self.fade_from:
            r = min(len(a), int(self.RAMP_SEC * AUDIO_RATE))
            w = np.linspace(1.0, 0.0, r, dtype=np.float32)
            a[:r] += np.float32(self.fade_from) * w
        self.fade_from = None
        a = self._soft_limit(a)
        self.last_out = float(a[-1])
        return (a * 32767).astype(np.int16)

    def _ramp(self, n: int, target: float) -> np.ndarray:
        step = 1.0 / (self.RAMP_SEC * AUDIO_RATE)
        k = np.arange(1, n + 1, dtype=np.float32) * step
        g = (np.minimum(self.gate + k, target) if target > self.gate
             else np.maximum(self.gate - k, target)).astype(np.float32)
        self.gate = float(g[-1])
        return g

    def _gate_curve(self, n: int, open_: bool) -> np.ndarray:
        """Per-sample gain: fades to 0 for warm-up/mute, ramps on squelch changes."""
        mute = min(n, self.warm_left)
        self.warm_left -= mute
        parts = []
        if mute:
            parts.append(self._ramp(mute, 0.0))
        if n - mute:
            parts.append(self._ramp(n - mute, 1.0 if open_ else 0.0))
        return np.concatenate(parts)

    def _soft_limit(self, a: np.ndarray) -> np.ndarray:
        """Linear below the knee, smooth tanh approach to full scale above it."""
        k = self.LIMIT_KNEE
        mag = np.abs(a)
        over = mag > k
        if over.any():
            a = a.copy()
            a[over] = np.sign(a[over]) * (k + (1 - k) * np.tanh((mag[over] - k) / (1 - k)))
        return a

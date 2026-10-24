"""
audio.py - audio output for smashDR via QtMultimedia (ships with PyQt6).

Input is always 48 kHz mono int16. If the chosen device doesn't accept that
directly, we convert to its preferred format (channels, sample format, rate).
"""

import numpy as np
from PyQt6.QtMultimedia import QAudio, QAudioDevice, QAudioFormat, QAudioSink, QMediaDevices

SRC_RATE = 48_000
BUFFER_SEC = 0.6       # sink buffer
PREBUFFER_SEC = 0.25   # collect this much before (re)starting playback


def output_devices() -> list:
    return list(QMediaDevices.audioOutputs())


def default_device() -> QAudioDevice:
    return QMediaDevices.defaultAudioOutput()


def device_key(dev: QAudioDevice) -> str:
    return bytes(dev.id()).hex()


class AudioOut:
    def __init__(self):
        self.sink = None
        self.io = None
        self.device = None
        self.volume = 0.5
        self.fmt = None
        self._pend = bytearray()
        self._priming = True
        self._prebytes = 0
        self.underruns = 0

    # ---- lifecycle ----
    def open(self, device: QAudioDevice) -> str:
        """Open a device. Returns a short status string."""
        self.close()
        want = QAudioFormat()
        want.setSampleRate(SRC_RATE)
        want.setChannelCount(1)
        want.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        fmt = want if device.isFormatSupported(want) else device.preferredFormat()

        self.device = device
        self.fmt = fmt
        self.sink = QAudioSink(device, fmt)
        self.sink.setBufferSize(int(fmt.bytesForDuration(int(BUFFER_SEC * 1e6))))
        self.sink.setVolume(self.volume)
        self.io = self.sink.start()
        frame = fmt.bytesPerFrame()
        self._prebytes = int(fmt.bytesForDuration(int(PREBUFFER_SEC * 1e6))) // frame * frame
        self._pend = bytearray()
        self._priming = True
        note = "" if fmt is want else (
            f" (converting to {fmt.sampleRate()} Hz, {fmt.channelCount()} ch, "
            f"{fmt.sampleFormat().name})")
        return f"{device.description()}{note}"

    def close(self):
        if self.sink:
            self.sink.stop()
        self.sink = None
        self.io = None

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))
        if self.sink:
            self.sink.setVolume(self.volume)

    # ---- data ----
    def write(self, pcm16: bytes):
        if not self.io or not pcm16:
            return
        data = self._convert(np.frombuffer(pcm16, dtype=np.int16))

        # Jitter buffer: SDR audio arrives in ~110 ms bursts. If the sink ran
        # dry, collect PREBUFFER_SEC before feeding it again so it doesn't
        # stutter on every burst.
        if not self._priming and self.sink.state() == QAudio.State.IdleState:
            self._priming = True
            self.underruns += 1
        if self._priming:
            self._pend += data
            if len(self._pend) < self._prebytes:
                return
            data = bytes(self._pend)
            self._pend = bytearray()
            self._priming = False

        free = self.sink.bytesFree()
        frame = self.fmt.bytesPerFrame()
        free -= free % frame
        if free > 0:
            self.io.write(data[:free])   # drop overflow: keeps latency bounded

    def test_tone(self, freq=1000.0, sec=0.4, amp=0.3):
        t = np.arange(int(SRC_RATE * sec)) / SRC_RATE
        env = np.minimum(1, np.minimum(t, sec - t) / 0.01)   # 10 ms fade in/out
        tone = (amp * env * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
        self.write(tone.tobytes())

    def _convert(self, x: np.ndarray) -> bytes:
        fmt = self.fmt
        if fmt.sampleRate() == SRC_RATE and fmt.channelCount() == 1 and \
                fmt.sampleFormat() == QAudioFormat.SampleFormat.Int16:
            return x.tobytes()

        y = x.astype(np.float32)
        if fmt.sampleRate() != SRC_RATE and len(y) > 1:
            n_out = int(round(len(y) * fmt.sampleRate() / SRC_RATE))
            y = np.interp(np.linspace(0, len(y) - 1, n_out), np.arange(len(y)), y)

        sf = fmt.sampleFormat()
        S = QAudioFormat.SampleFormat
        if sf == S.Float:
            y = (y / 32768.0).astype(np.float32)
        elif sf == S.Int32:
            y = (y * 65536).astype(np.int32)
        elif sf == S.UInt8:
            y = (y / 256 + 128).astype(np.uint8)
        else:
            y = y.astype(np.int16)

        ch = fmt.channelCount()
        if ch > 1:
            y = np.repeat(y[:, None], ch, axis=1).ravel()
        return y.tobytes()

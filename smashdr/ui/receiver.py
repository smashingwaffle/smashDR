"""
receiver.py  -  smashDR receiver window.

Receiver tab
  * VFO frequency display (wheel per digit, arrows = step, double-click to type)
  * NFM / AM / WFM, tuning step, band-plan label ("where am I")
  * Band-plan strip, spectrum and waterfall: click = tune (snapped to step),
    wheel = step up/down, drag the yellow marker
  * Signal meter with squelch marker, volume, mute, IQ recording
Settings tab
  * Radio: device, sample rate, gain, PPM, bias tee, band-plan auto mode/step
  * Audio: output device, test tone, de-emphasis, CTCSS filter
  * Display: waterfall range, palette, averaging
  * Recording: folder

The hardware is tuned slightly ABOVE the target so the target never sits on
the dongle's DC spike; it re-centres automatically when you tune off-span.
"""

import argparse
import json
import os
import html
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from smashdr.core import bandplan
from smashdr.core import settings as cfg
from smashdr.core import codes, flightaware, notify, places, transcribe
from smashdr.core.capture import AsyncCapture
from smashdr.core.dsp import DIGITAL_MODES, FFT_SIZE, MODES, Demod, cu8_to_iq, power_spectrum_db

from smashdr.paths import RECORDINGS, ROOT, USER  # noqa: E402

HERE = ROOT
VERSION = "0.7.4"

READ_BYTES = 524288          # 262144 IQ samples per block (~110 ms at 2.4 MS/s)
WF_ROWS = 300
ANALOG_HANG_S = 1.2         # analog: squelch shut this long = end of transmission
ANALOG_MAX_S = 30.0         # analog: transcribe in chunks if the squelch stays open
WF_SPLIT = 3                # waterfall rows per USB block (~27 rows/s at 2.4 MS/s)
TAG_CHARS = 12              # preset tags on the spectrum are cut to this length
RATES_MSPS = [2.4, 1.92, 1.2, 0.96]
STEPS_HZ = [1000, 2500, 5000, 6250, 7500, 8330, 10000, 12500, 15000, 20000,
            25000, 50000, 100000, 200000]
PALETTES = ["sdr", "inferno", "viridis", "plasma", "magma"]
# classic SDR waterfall: black noise -> blue -> cyan -> yellow -> red peaks
SDR_PALETTE = [(0.0, (0, 0, 0)), (0.15, (0, 0, 70)), (0.35, (0, 40, 200)),
               (0.55, (0, 200, 255)), (0.75, (255, 255, 0)), (0.9, (255, 120, 0)),
               (1.0, (255, 0, 0))]
AVERAGING = {"Off": 1.0, "Low": 0.6, "Medium": 0.35, "High": 0.15}


# --------------------------------------------------------------------------
# Library loading
# --------------------------------------------------------------------------
def _setup_dll_path():
    for d in (HERE, HERE / "lib"):
        if d.is_dir():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass


def _pe_arch(path: Path) -> str:
    """Read the machine type from a DLL's PE header."""
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            pe_off = int.from_bytes(f.read(4), "little")
            f.seek(pe_off + 4)
            machine = int.from_bytes(f.read(2), "little")
        return {0x8664: "x64", 0x14C: "x86 (32-bit)", 0xAA64: "ARM64"}.get(machine, hex(machine))
    except OSError:
        return "unreadable"


def _diagnose_rtlsdr_dll():
    """Explain exactly why rtlsdr.dll won't load. Returns None if it loads."""
    import ctypes
    import platform

    dll = next((d / "rtlsdr.dll" for d in (HERE, HERE / "lib")
                if (d / "rtlsdr.dll").exists()), None)
    if dll is None:
        return (f"rtlsdr.dll is not in {HERE} or {HERE / 'lib'}.\n"
                "    Copy rtlsdr.dll, msvcr100.dll and pthreadVC2.dll there.")

    arch = _pe_arch(dll)
    py_arch = "x64" if platform.architecture()[0] == "64bit" else "x86 (32-bit)"
    if arch != py_arch:
        return (f"{dll} is an {arch} DLL but Python is {py_arch}.\n"
                "    Use the files from the x64 folder of the rtl-sdr-blog Release.zip.")

    missing = [n for n in ("msvcr100.dll", "pthreadVC2.dll")
               if not (dll.parent / n).exists()]
    try:
        ctypes.CDLL(str(dll))
    except OSError as e:
        msg = f"{dll} found ({arch}) but Windows refused to load it: {e}"
        if missing:
            msg += f"\n    Missing next to it: {', '.join(missing)}"
        return msg
    return None


_setup_dll_path()

try:
    from rtlsdr import RtlSdr
except Exception as e:  # missing DLL shows up as ImportError/OSError
    print("\n[!] Could not load librtlsdr:", e)
    reason = _diagnose_rtlsdr_dll() if sys.platform == "win32" else None
    if reason:
        print("    " + reason + "\n")
    else:
        print("    rtlsdr.dll itself loads fine, so pyrtlsdr can't find it.")
        print(f"    Folder searched: {HERE}\n")
    sys.exit(1)

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

from smashdr.ui.widgets import FreqDisplay, LevelMeter  # noqa: E402
from smashdr.ui.presets import PresetsWindow, load_presets  # noqa: E402
from smashdr.ui.aircraft import AircraftTab  # noqa: E402  (before QApplication: WebEngine)
from smashdr.decoders.p25_live import P25Live  # noqa: E402
from smashdr.decoders.p25_trunk import P25Trunk  # noqa: E402
from smashdr.decoders.digital_live import DmrLive, DstarLive, YsfLive  # noqa: E402
from smashdr.decoders import p25 as p25mod  # noqa: E402

try:
    from smashdr.ui import audio  # noqa: E402
except Exception as e:  # QtMultimedia missing
    print("[!] Audio disabled, QtMultimedia not available:", e)
    audio = None

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)


def list_devices() -> list:
    """Names of attached RTL-SDR devices (best effort)."""
    try:
        import ctypes
        from rtlsdr import librtlsdr as _l
        lib = _l.librtlsdr
        count = lib.rtlsdr_get_device_count()
        fn = lib.rtlsdr_get_device_name
        fn.restype = ctypes.c_char_p
        fn.argtypes = [ctypes.c_uint32]
        return [f"#{i}: {fn(i).decode(errors='replace')}" for i in range(count)]
    except Exception:
        return []


# --------------------------------------------------------------------------
# USB capture (async, gap-free)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# SDR processing thread
# --------------------------------------------------------------------------
class SdrReader(QtCore.QThread):
    # spectrum_db, center_hz, rate_hz, stats dict
    frame = QtCore.pyqtSignal(object, float, float, object)
    audio = QtCore.pyqtSignal(bytes)
    status = QtCore.pyqtSignal(str)
    p25_event = QtCore.pyqtSignal(object)
    transcript = QtCore.pyqtSignal(object)

    def __init__(self, sdr: RtlSdr, opts: dict):
        super().__init__()
        self.sdr = sdr
        self.mode = opts["mode"]
        self.p25 = None
        self._vocoder_failed = False
        self.target_hz = float(opts["freq_mhz"]) * 1e6
        self.squelch_db = float(opts["squelch_db"])
        self.demod = Demod()
        self.demod.mode = opts["mode"] if opts["mode"] not in DIGITAL_MODES else "NFM"
        self.demod.set_options(nfm_deemph_us=opts["nfm_deemph_us"],
                               wfm_deemph_us=opts["wfm_deemph_us"],
                               ctcss_filter=opts["ctcss_filter"])
        self._demod_ok = False
        self._running = True
        self._lock = threading.Lock()
        self._pending = {}
        self._flush = 0
        self._rec = None
        self._rec_bytes = 0
        self._rec_path = None
        self._q = queue.Queue(maxsize=40)   # ~4 s of headroom at 2.4 MS/s
        self._cap = None
        # speech-to-text of finished clear P25 calls (optional: pip install faster-whisper)
        self._call_pcm = None               # list of 48 kHz chunks while a call is up
        self._an_pcm = None                 # same for analog (squelch-gated)
        self._an_meta = None
        self._an_last = 0.0
        self._call_meta = None
        self.transcriber = None
        self.stt_on = bool(opts.get("stt_on", True))          # set from the UI thread
        self.stt_skip_am = bool(opts.get("stt_skip_am", True))
        if transcribe.available():
            self.transcriber = transcribe.Transcriber(self.transcript.emit,
                                                      opts.get("stt_model", transcribe.DEFAULT_MODEL))
        else:
            print("[i] Speech-to-text off: pip install faster-whisper")

    def request(self, **kw):
        with self._lock:
            self._pending.update(kw)

    def stop(self):
        self._running = False

    # -- applied inside this thread so librtlsdr is only touched here -------
    def _apply_pending(self):
        with self._lock:
            p, self._pending = self._pending, {}
        if not p:
            return

        retune = any(k in p for k in ("center", "rate", "gain", "ppm"))
        if self._rec and ("rec_stop" in p or retune):
            self._close_recording()

        try:
            if "rate" in p:
                self.sdr.sample_rate = p["rate"]
            if "center" in p:
                self.sdr.center_freq = p["center"]
            if "gain" in p:
                self.sdr.gain = p["gain"]
            if "ppm" in p and p["ppm"] != 0:
                try:
                    self.sdr.freq_correction = p["ppm"]
                except Exception:
                    pass  # librtlsdr errors if value unchanged
        except Exception as e:
            self.status.emit(f"SDR setting failed: {e}")

        if "bias_tee" in p:
            try:
                self.sdr.set_bias_tee(bool(p["bias_tee"]))
            except Exception as e:
                self.status.emit(f"Bias tee not available: {e}")

        if "target" in p:
            self.target_hz = p["target"]
        if "squelch" in p:
            self.squelch_db = p["squelch"]
        if "demod_opts" in p:
            self.demod.set_options(**p["demod_opts"])

        if "rate" in p or "mode" in p:
            if "mode" in p:
                self.mode = p["mode"]
            self._configure_demod()
        elif "center" in p or "target" in p:
            off = self.target_hz - self.sdr.center_freq
            if isinstance(self.p25, P25Trunk):
                self.p25.set_offset(off, float(self.sdr.center_freq))
            elif self.p25 is not None:
                self.p25.set_offset(off)
            elif self._demod_ok:
                self.demod.set_offset(off)

        if retune:
            # drop samples captured before the change took effect
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._flush = 2
            if self._demod_ok:
                self.demod.mute_for(0.1)

        if "rec_start" in p:
            self._open_recording(p["rec_start"])

    def _track_call(self, ev):
        """Collect each call's audio; hand clear calls to the transcriber when they end."""
        if self.transcriber is None or ev["type"] == "cc" or not self.stt_on:
            self._call_pcm = self._call_meta = None
            return
        if ev["type"] == "start":
            self._call_pcm, self._call_meta = [], dict(ev)
        elif self._call_meta is not None:
            self._call_meta.update(ev)
        if ev["type"] == "end" and self._call_pcm is not None:
            meta = dict(self._call_meta, t_end=time.time())
            meta["freq_hz"] = meta.pop("freq", None) or self.target_hz
            if self._call_pcm and meta.get("encrypted") is False:
                self.transcriber.submit(np.concatenate(self._call_pcm), meta)
            self._call_pcm = self._call_meta = None

    def _track_analog(self, pcm, sq_open):
        """Analog NFM/AM: a transmission = squelch open until it stays shut for ANALOG_HANG_S."""
        if (self.transcriber is None or not self.stt_on or self.mode not in ("NFM", "AM")
                or self.squelch_db <= 0 or (self.mode == "AM" and self.stt_skip_am)):
            self._an_pcm = None
            return
        now = time.time()
        if sq_open:
            if self._an_pcm is None:
                self._an_pcm = []
                self._an_meta = {"type": "analog", "mode": self.mode, "encrypted": False,
                                 "freq_hz": self.target_hz, "t_start": now}
            self._an_last = now
        if self._an_pcm is None:
            return
        self._an_pcm.append(pcm)
        ended = not sq_open and now - self._an_last > ANALOG_HANG_S
        too_long = now - self._an_meta["t_start"] > ANALOG_MAX_S
        if ended or too_long:
            audio = np.concatenate(self._an_pcm)
            if ended:                                  # drop the silent hang tail
                audio = audio[:max(0, len(audio) - int(ANALOG_HANG_S * 48_000))]
            self.transcriber.submit(audio, dict(self._an_meta, t_end=now))
            self._an_pcm = None

    def _configure_demod(self):
        self._an_pcm = None
        rate = float(self.sdr.sample_rate)
        off = self.target_hz - self.sdr.center_freq
        if self.mode in DIGITAL_MODES:
            self._demod_ok = False
            try:
                if self.mode == "P25":            # follows grants when it's a control channel
                    self.p25 = P25Trunk(rate, off, float(self.sdr.center_freq),
                                        lambda: self._vocoder("P25"))
                else:
                    live = {"DMR": DmrLive, "YSF": YsfLive, "DSTAR": DstarLive}[self.mode]
                    self.p25 = live(rate, off, self._vocoder(self.mode))
            except ValueError as e:
                self.p25 = None
                self.status.emit(f"P25 off: {e}")
            return
        self.p25 = None
        try:
            self.demod.configure(rate, off, self.mode)
            self._demod_ok = True
        except ValueError as e:
            self._demod_ok = False
            self.status.emit(f"Audio off: {e}")

    def _vocoder(self, mode="P25"):
        """IMBE (P25) / AMBE+2 (DMR, Fusion) decoder, or None (calls still shown)
        if mbe-neo.dll is missing."""
        if self._vocoder_failed:
            return None
        try:
            from smashdr.core import vocoder
            return vocoder.ImbeDecoder() if mode == "P25" else vocoder.AmbeDecoder()
        except OSError as e:
            self._vocoder_failed = True
            self.status.emit(f"Digital voice off: {e}. Calls are still shown.")
            return None

    def _open_recording(self, meta: dict):
        rec_dir = Path(meta.pop("dir", None) or (RECORDINGS))
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status.emit(f"Can't create recording folder {rec_dir}: {e}")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{meta['target_hz'] / 1e6:.5f}MHz_{int(self.sdr.sample_rate)}sps"
        self._rec_path = rec_dir / f"{name}.cu8"
        meta = dict(meta)
        meta.update(
            format="cu8 interleaved I/Q",
            center_hz=float(self.sdr.center_freq),
            sample_rate=float(self.sdr.sample_rate),
            mode=self.mode,
            started=stamp,
            tool=f"smashDR {VERSION}",
        )
        with open(self._rec_path.with_suffix(".json"), "w") as f:
            json.dump(meta, f, indent=2)
        self._rec = open(self._rec_path, "wb")
        self._rec_bytes = 0
        self.status.emit(f"Recording -> {self._rec_path}")

    def _close_recording(self):
        if self._rec:
            self._rec.close()
            mb = self._rec_bytes / 1e6
            self.status.emit(f"Saved {self._rec_path.name} ({mb:.1f} MB)")
        self._rec = None

    def _channel_above_floor(self, spec, center, rate):
        half = MODES[self.mode]["half_bw"]
        binw = rate / FFT_SIZE
        c = int(round((self.target_hz - center) / binw + FFT_SIZE / 2))
        h = max(1, int(np.ceil(half / binw)))
        lo, hi = max(0, c - h), min(FFT_SIZE, c + h + 1)
        if lo >= hi:
            return 0.0
        return float(spec[lo:hi].max() - np.median(spec))

    def run(self):
        self._configure_demod()
        self._cap = AsyncCapture(self.sdr, READ_BYTES, self._q)
        self._cap.start()
        while self._running:
            self._apply_pending()
            try:
                raw = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._cap.error:
                    self.status.emit(f"Capture stopped: {self._cap.error}")
                    time.sleep(0.5)
                continue

            if self._flush:
                self._flush -= 1
                continue

            if self._rec:
                self._rec.write(raw)
                self._rec_bytes += len(raw)

            u8 = np.frombuffer(raw, dtype=np.uint8)
            clip = 100.0 * np.count_nonzero((u8 == 0) | (u8 == 255)) / len(u8)
            iq = cu8_to_iq(raw)
            # waterfall rows from sub-blocks (faster scroll); spectrum = their mean
            rows = np.stack([power_spectrum_db(b) for b in np.array_split(iq, WF_SPLIT)])
            spec = 10.0 * np.log10(np.mean(10.0 ** (rows / 10.0), axis=0))
            center = float(self.sdr.center_freq)
            rate = float(self.sdr.sample_rate)

            snr = self._channel_above_floor(spec, center, rate)
            sq_open = self.squelch_db <= 0 or snr >= self.squelch_db
            if self.p25 is not None:
                pcm, events = self.p25.process(iq)
                if len(pcm):
                    self.audio.emit(pcm.tobytes())
                    if self._call_pcm is not None:
                        self._call_pcm.append(pcm)
                for ev in events:
                    self._track_call(ev)
                    self.p25_event.emit(ev)
            elif self._demod_ok:
                pcm = self.demod.process(iq, sq_open)
                self.audio.emit(pcm.tobytes())
                self._track_analog(pcm, sq_open)

            stats = {"clip": clip, "snr": snr, "sq_open": sq_open,
                     "drops": self._cap.dropped,
                     "rec_mb": self._rec_bytes / 1e6 if self._rec else None,
                     "wf_rows": rows}
            self.frame.emit(spec, center, rate, stats)

        self._cap.stop()
        self._cap.join(timeout=2.0)
        self._close_recording()


# --------------------------------------------------------------------------
# Plot helpers
# --------------------------------------------------------------------------
class TuneViewBox(pg.ViewBox):
    """ViewBox where the wheel tunes instead of zooming, and dragging does nothing."""

    def __init__(self, on_wheel, **kw):
        super().__init__(**kw)
        self._on_wheel = on_wheel
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)

    def wheelEvent(self, ev, axis=None):
        d = ev.delta() if hasattr(ev, "delta") else ev.angleDelta().y()
        if d:
            self._on_wheel(1 if d > 0 else -1)
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        ev.ignore()


class BandStrip(pg.PlotWidget):
    """Coloured band-plan segments for the visible span (x in MHz)."""

    def __init__(self, on_wheel):
        super().__init__(viewBox=TuneViewBox(on_wheel))
        self.setFixedHeight(30)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setYRange(0, 1, padding=0)
        self.plotItem.vb.disableAutoRange()          # x-linked: band segments must not move the view
        self._items = []
        self._span = None

    def redraw(self, lo_hz: float, hi_hz: float):
        if self._span == (lo_hz, hi_hz):
            return
        self._span = (lo_hz, hi_hz)
        for it in self._items:
            self.removeItem(it)
        self._items = []
        span_mhz = (hi_hz - lo_hz) / 1e6
        for lo, hi, cat, label, _m, _s in bandplan.in_span(lo_hz, hi_hz):
            r, g, b = bandplan.CATEGORIES[cat][1]
            rect = QtWidgets.QGraphicsRectItem(lo, 0.08, hi - lo, 0.84)
            rect.setBrush(pg.mkBrush(r, g, b, 150))
            rect.setPen(pg.mkPen(r, g, b, 230))
            self.addItem(rect)
            self._items.append(rect)
            vis_lo, vis_hi = max(lo, lo_hz / 1e6), min(hi, hi_hz / 1e6)
            if vis_hi - vis_lo > 0.12 * span_mhz:
                t = pg.TextItem(label, color=(255, 255, 255), anchor=(0.5, 0.5))
                t.setPos((vis_lo + vis_hi) / 2, 0.5)
                self.addItem(t)
                self._items.append(t)


class TagStrip(pg.PlotWidget):
    """Preset names under the waterfall, written horizontally (x in MHz).
    Overlapping names go to a second / third row; hover = tooltip, click = tune."""

    ROWS = 3
    ROW_PX = 16

    def __init__(self, on_wheel, tooltip_fn, on_click):
        super().__init__(viewBox=TuneViewBox(on_wheel))
        self.setFixedHeight(self.ROWS * self.ROW_PX + 6)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setYRange(0, self.ROWS, padding=0)
        self.setMouseEnabled(x=False, y=False)
        # never auto-range: this strip is x-linked to the waterfall, and fitting the
        # tags would drag the spectrum/waterfall view (and every click) off the real span
        self.plotItem.vb.disableAutoRange()
        self._tooltip_fn, self._on_click = tooltip_fn, on_click
        self._items, self._hits = [], []        # hits: (freq MHz, half label width px, row, presets)
        self._presets, self._span = [], None
        self._font = QtGui.QFont("Segoe UI", 8)
        self.setMouseTracking(True)

    def set_tags(self, presets, lo_hz, hi_hz):
        self._presets, self._span = presets, (lo_hz, hi_hz)
        if hi_hz > lo_hz:
            self.setXRange(lo_hz / 1e6, hi_hz / 1e6, padding=0)
        self.redraw()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "_items"):          # Qt resizes during __init__, before our state exists
            self.redraw()

    def redraw(self):
        if not hasattr(self, "_items"):
            return
        for it in self._items:
            self.removeItem(it)
        self._items, self._hits = [], []
        if not self._span or not self._presets:
            return
        lo, hi = self._span
        w = max(1.0, self.plotItem.vb.width() or self.width())
        px_per_hz = w / (hi - lo)
        fm = QtGui.QFontMetrics(self._font)
        groups = {}
        for p in self._presets:
            if lo < p["freq_hz"] < hi:
                groups.setdefault(p["freq_hz"], []).append(p)
        row_end = [-1e9] * self.ROWS             # right edge (px) used per row
        for f in sorted(groups):
            ps = groups[f]
            name = ps[0]["name"] if len(ps[0]["name"]) <= TAG_CHARS else ps[0]["name"][:TAG_CHARS - 1] + "…"
            if len(ps) > 1:
                name += f" +{len(ps) - 1}"
            tw = fm.horizontalAdvance(name) + 8
            x = (f - lo) * px_per_hz
            x0 = x - tw / 2
            row = next((r for r in range(self.ROWS) if x0 > row_end[r] + 3), None)
            colour = "#b4a7d6" if ps[0].get("digital") else "#bdbdbd"
            y_top = self.ROWS - (row if row is not None else 0)
            tick = QtWidgets.QGraphicsLineItem(f / 1e6, self.ROWS, f / 1e6, y_top - 0.15)
            tick.setPen(pg.mkPen(colour, width=1, style=QtCore.Qt.PenStyle.DotLine))
            self.addItem(tick)
            self._items.append(tick)
            if row is None:                       # no room: tick only, name in the tooltip
                self._hits.append((f / 1e6, 4, -1, ps))
                continue
            row_end[row] = x0 + tw
            t = pg.TextItem(name, color=colour, anchor=(0.5, 0.0))
            t.setFont(self._font)
            t.setPos(f / 1e6, y_top - 0.05)
            self.addItem(t)
            self._items.append(t)
            self._hits.append((f / 1e6, tw / 2, row, ps))

    def _at(self, pos):
        """Presets under widget position pos (QPoint/QPointF), or None."""
        vb = self.plotItem.vb
        sp = self.mapToScene(QtCore.QPoint(int(pos.x()), int(pos.y())))
        if not vb.sceneBoundingRect().contains(sp):
            return None
        (x0v, x1v), _ = vb.viewRange()
        px_per_mhz = vb.width() / max(1e-9, x1v - x0v)
        x_mhz = vb.mapSceneToView(sp).x()
        r = vb.sceneBoundingRect()
        row = int((sp.y() - r.top()) / max(1.0, r.height() / self.ROWS))
        best, best_d = None, 1e9
        for f_mhz, half_px, rw, ps in self._hits:
            d = abs(x_mhz - f_mhz) * px_per_mhz
            if d <= half_px + 2 and rw == row:
                return ps                                   # on the label itself
            if d < best_d:
                best, best_d = ps, d
        return best if best_d <= 10 else None               # near a tick

    def mouseMoveEvent(self, ev):
        super().mouseMoveEvent(ev)
        ps = self._at(ev.position())
        if ps:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), self._tooltip_fn(ps), self)
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        else:
            QtWidgets.QToolTip.hideText()
            self.unsetCursor()

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            ps = self._at(ev.position())
            if ps:
                self._on_click(ps[0])
                ev.accept()
                return
        super().mousePressEvent(ev)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, sdr: RtlSdr, opts: dict):
        super().__init__()
        self.setWindowTitle(f"smashDR {VERSION}")
        self._place_main_window(opts.get("main_geometry"))
        self.sdr = sdr
        self.opts = opts

        self.target_hz = int(round(float(opts["freq_mhz"]) * 1e6))
        self.mode = opts["mode"]
        self.step_hz = int(opts["step_hz"])
        self._band_label = None
        self.center_hz = None
        self.rate_hz = None
        self.freqs_mhz = None
        self.avg = None
        self.wf = np.full((WF_ROWS, FFT_SIZE), -120.0, dtype=np.float32)
        self._wf_rect = None
        self._tag_items = []
        try:
            self._tag_presets = load_presets()
        except Exception as e:                       # tags are cosmetic, never fatal
            print("[!] Could not load presets for spectrum tags:", e)
            self._tag_presets = []

        self.reader = SdrReader(sdr, opts)

        tabs = self.tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_receiver_tab(), "Receiver")
        self.aircraft_tab = AircraftTab(opts, list_devices(),
                                        int(opts.get("_opened_device", opts["device_index"])))
        tabs.addTab(self.aircraft_tab, "Aircraft")
        self.aircraft_tab.emergency.connect(self._aircraft_emergency)
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.setCurrentIndex(min(max(int(opts.get("tab", 0)), 0), tabs.count() - 1))
        # save settings every 30 s too, so a crash or power cut doesn't lose them
        self._autosave = QtCore.QTimer(self)
        self._autosave.timeout.connect(self._save_opts)
        self._autosave.start(30_000)
        self.setCentralWidget(tabs)

        self.audio_out = audio.AudioOut() if audio else None
        if self.audio_out:
            self.audio_out.set_volume((opts["volume"] / 100) ** 2)
            self._fill_audio_devices(select_key=opts.get("audio_device_id"))
        self.reader.audio.connect(self._on_audio)
        self.reader.frame.connect(self.on_frame)
        self.reader.status.connect(self.status_lbl.setText)
        self.reader.p25_event.connect(self.on_p25_event)
        self.reader.transcript.connect(self.on_transcript)

        self.presets_win = None
        self._apply_mode_ui()
        self._tune(self.target_hz, apply_band_defaults=False)
        self.reader.start()
        if opts.get("presets_open"):
            QtCore.QTimer.singleShot(0, self.show_presets)

    # ======================================================================
    # Receiver tab
    # ======================================================================
    def _build_receiver_tab(self):
        opts = self.opts

        # --- row 1: VFO, band label, modes ---
        self.vfo = FreqDisplay(self.target_hz)
        self.vfo.step_hz = self.step_hz
        self.vfo.changed.connect(lambda hz: self._tune(hz))
        mhz = QtWidgets.QLabel("MHz")
        self.band_lbl = QtWidgets.QLabel("")
        f = self.band_lbl.font()
        f.setPointSize(f.pointSize() + 2)
        self.band_lbl.setFont(f)

        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(2)
        self.mode_btns = {}
        for m in MODES:
            b = QtWidgets.QPushButton(m)
            b.setCheckable(True)
            b.setMinimumWidth(56)
            self.mode_group.addButton(b)
            self.mode_btns[m] = b
            mode_row.addWidget(b)
            b.clicked.connect(lambda _c, mm=m: self._set_mode(mm))

        self.presets_btn = QtWidgets.QPushButton("Open presets")
        self.presets_btn.setToolTip("Preset buttons in their own window (put it on a second screen)")
        self.presets_btn.clicked.connect(self.show_presets)

        row1 = QtWidgets.QHBoxLayout()
        row1.addWidget(self.vfo)
        row1.addWidget(mhz)
        row1.addSpacing(16)
        row1.addWidget(self.band_lbl)
        row1.addStretch(1)
        row1.addLayout(mode_row)
        row1.addSpacing(12)
        row1.addWidget(self.presets_btn)

        # --- row 2: step, squelch, meter, volume, mute, record ---
        self.step_box = QtWidgets.QComboBox()
        for s in STEPS_HZ:
            self.step_box.addItem(self._step_text(s), s)
        self._select_step(self.step_hz)
        self.step_box.currentIndexChanged.connect(
            lambda _i: self._set_step(self.step_box.currentData()))

        self.sq_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sq_slider.setRange(0, 60)
        self.sq_slider.setFixedWidth(120)
        self.sq_slider.setValue(int(opts["squelch_db"]))
        self.sq_slider.setToolTip("Squelch: dB above noise floor. 0 = always open.")
        self.sq_val = QtWidgets.QLabel()
        self.sq_val.setMinimumWidth(42)
        self.sq_slider.valueChanged.connect(self._set_squelch)
        self._set_squelch(self.sq_slider.value())

        self.meter = LevelMeter()

        self.vol_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(110)
        self.vol_slider.setValue(int(opts["volume"]))
        self.vol_slider.valueChanged.connect(self._set_volume)

        self.mute_chk = QtWidgets.QCheckBox("Mute")
        self.mute_chk.setChecked(bool(opts.get("mute", False)))

        self.rec_btn = QtWidgets.QPushButton("Record IQ")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self.toggle_record)

        row2 = QtWidgets.QHBoxLayout()
        for w in (QtWidgets.QLabel("Step"), self.step_box,
                  QtWidgets.QLabel("Squelch"), self.sq_slider, self.sq_val):
            row2.addWidget(w)
        row2.addWidget(self.meter, 1)
        for w in (QtWidgets.QLabel("Vol"), self.vol_slider, self.mute_chk, self.rec_btn):
            row2.addWidget(w)
        # waterfall look, right here so you can watch it while adjusting
        self.wf_bright = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.wf_bright.setRange(-15, 15)
        self.wf_bright.setValue(int(self.opts.get("wf_bright", 0)))
        self.wf_bright.setToolTip("Waterfall black level: right = darker background (hides noise), "
                                  "left = brighter (shows weak signals). Double-click to reset.")
        self.wf_contrast = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.wf_contrast.setRange(50, 300)
        self.wf_contrast.setValue(int(self.opts.get("wf_contrast", 100)))
        self.wf_contrast.setToolTip("Waterfall contrast: right = weak signals pop more. "
                                    "Double-click to reset.")
        for sl, default in ((self.wf_bright, 0), (self.wf_contrast, 100)):
            sl.setFixedWidth(110)
            sl.mouseDoubleClickEvent = lambda _e, s=sl, d=default: s.setValue(d)
        row2.addSpacing(16)
        for w in (QtWidgets.QLabel("WF black"), self.wf_bright,
                  QtWidgets.QLabel("contrast"), self.wf_contrast):
            row2.addWidget(w)

        # --- band strip, spectrum, waterfall ---
        self.band_strip = BandStrip(self._wheel_tune)
        self.spec_plot = pg.PlotWidget(viewBox=TuneViewBox(self._wheel_tune))
        self.spec_plot.setLabel("left", "dBFS")
        self.spec_plot.setYRange(-110, -10, padding=0)
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#4fc3f7", width=1))
        self.chan_region = pg.LinearRegionItem(movable=False,
                                               brush=pg.mkBrush(255, 213, 79, 40))
        self.spec_plot.addItem(self.chan_region)
        self.marker = pg.InfiniteLine(angle=90, movable=True,
                                      pen=pg.mkPen("#ffd54f", width=1))
        self.marker.sigPositionChangeFinished.connect(
            lambda: self._tune(self.marker.value() * 1e6, snap=True))
        self.spec_plot.addItem(self.marker)
        self.band_strip.setXLink(self.spec_plot)

        self.wf_plot = pg.PlotWidget(viewBox=TuneViewBox(self._wheel_tune))
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_plot.hideAxis("left")
        self.wf_plot.setLabel("bottom", "MHz")
        self.wf_img = pg.ImageItem()
        self._set_palette(opts["palette"])
        self.wf_plot.addItem(self.wf_img)
        self.wf_marker = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen("#ffd54f", width=1))
        self.wf_plot.addItem(self.wf_marker)
        self.spec_plot.hideAxis("bottom")

        # not x-linked: pyqtgraph links by screen position and the strip fed ranges back
        # into the waterfall/spectrum; set_tags() gives it the span explicitly
        self.tag_strip = TagStrip(self._wheel_tune, self._tag_tooltip, self._tune_preset)
        for p in (self.band_strip, self.spec_plot, self.wf_plot):
            p.scene().sigMouseClicked.connect(
                lambda ev, plot=p: self._plot_clicked(ev, plot))

        self.info = QtWidgets.QLabel("Starting...")
        self.info.setFont(QtGui.QFont("Consolas", 10))
        self.status_lbl = QtWidgets.QLabel("")

        plots = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(plots)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.band_strip)
        pl.addWidget(self.spec_plot, 1)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(plots)
        wf_box = QtWidgets.QWidget()
        wl = QtWidgets.QVBoxLayout(wf_box)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(self.wf_plot, 1)
        wl.addWidget(self.tag_strip)
        split.addWidget(wf_box)
        split.setSizes([360, 420])

        self.tx_view = QtWidgets.QTextBrowser()
        self.tx_view.setOpenLinks(False)
        self._place_links = []
        self.tx_view.anchorClicked.connect(self._open_place)
        ft = self.tx_view.font()
        ft.setPointSize(ft.pointSize() + 1)
        self.tx_view.setFont(ft)
        self.tx_view.setPlaceholderText(
            "Speech-to-text of clear P25 calls and analog NFM/AM transmissions appears here.\n\nAnalog needs the squelch set above 0."
            if transcribe.available() else
            "Speech-to-text is off.\n\nIn the VSCode terminal run:\n"
            "pip install faster-whisper\n\nthen restart (F5).")
        tx_title = QtWidgets.QLabel("Transcripts")
        tf = tx_title.font()
        tf.setBold(True)
        tx_title.setFont(tf)
        tx_clear = QtWidgets.QToolButton()
        tx_clear.setText("Clear")
        tx_clear.clicked.connect(self.tx_view.clear)
        tx_head = QtWidgets.QHBoxLayout()
        self.stt_chk = QtWidgets.QCheckBox("On")
        self.stt_chk.setToolTip("Transcribe calls (off = nothing is sent to Whisper)")
        self.stt_chk.setChecked(bool(self.opts.get("stt_on", True)))
        self.stt_am_chk = QtWidgets.QCheckBox("Skip AM (air)")
        self.stt_am_chk.setToolTip("Don't transcribe AM, i.e. aviation; P25 and NFM keep going")
        self.stt_am_chk.setChecked(bool(self.opts.get("stt_skip_am", True)))
        self.stt_am_chk.setEnabled(self.stt_chk.isChecked())
        self.stt_chk.toggled.connect(self._stt_toggled)
        self.stt_am_chk.toggled.connect(self._stt_toggled)
        tx_head.addWidget(tx_title)
        tx_head.addSpacing(10)
        tx_head.addWidget(self.stt_chk)
        tx_head.addWidget(self.stt_am_chk)
        tx_head.addStretch(1)
        tx_head.addWidget(tx_clear)
        tx_panel = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(tx_panel)
        tl.setContentsMargins(6, 0, 0, 0)
        tx_panel.setMinimumWidth(200)
        tl.addLayout(tx_head)
        tl.addWidget(self.tx_view, 1)

        hsplit = self.tx_split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        hsplit.addWidget(split)
        hsplit.addWidget(tx_panel)
        hsplit.setStretchFactor(0, 1)
        hsplit.setStretchFactor(1, 0)
        self.spec_split = split
        for sp, key, default in ((hsplit, "split_tx", [2800, 520]), (split, "split_spec", None)):
            sizes = self.opts.get(key)
            if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(x, int) for x in sizes):
                sp.setSizes(sizes)
            elif default:
                sp.setSizes(default)

        lay = QtWidgets.QVBoxLayout()
        lay.addLayout(row1)
        lay.addLayout(row2)
        self.p25_lbl = QtWidgets.QLabel("")
        self.p25_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f2 = self.p25_lbl.font()
        f2.setPointSize(f2.pointSize() + 3)
        self.p25_lbl.setFont(f2)
        self.p25_lbl.setMinimumHeight(28)
        lay.addWidget(self.p25_lbl)
        self.cc_lbl = QtWidgets.QLabel("")         # control channel + grants being followed
        self.cc_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.cc_lbl.setWordWrap(True)
        self.cc_lbl.hide()
        lay.addWidget(self.cc_lbl)
        lay.addWidget(hsplit, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.status_lbl)
        w = QtWidgets.QWidget()
        w.setLayout(lay)
        return w

    # ======================================================================
    # Settings tab
    # ======================================================================
    def _build_settings_tab(self):
        opts = self.opts

        # --- Radio ---
        self.dev_box = QtWidgets.QComboBox()
        devs = list_devices() or [f"#{opts['device_index']}"]
        self.dev_box.addItems(devs)
        self.dev_box.setCurrentIndex(min(int(opts["device_index"]), len(devs) - 1))
        self.dev_box.currentIndexChanged.connect(self._device_chosen)
        dev_note = QtWidgets.QLabel("")
        self._dev_note = dev_note

        self.rate_box = QtWidgets.QComboBox()
        for r in RATES_MSPS:
            self.rate_box.addItem(f"{r} MS/s", r * 1e6)
        self.rate_box.setCurrentIndex(
            min(range(len(RATES_MSPS)), key=lambda i: abs(RATES_MSPS[i] - float(opts["rate_msps"]))))
        self.rate_box.currentIndexChanged.connect(self._set_rate)

        self._gains = list(self.sdr.valid_gains_db)
        self.gain_auto = QtWidgets.QCheckBox("Auto")
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.gain_slider.setRange(0, len(self._gains) - 1)
        self.gain_lbl = QtWidgets.QLabel()
        self.gain_lbl.setMinimumWidth(60)
        if opts["gain"] == "auto":
            self.gain_auto.setChecked(True)
            self.gain_slider.setValue(len(self._gains) // 2)
        else:
            g = float(opts["gain"])
            self.gain_slider.setValue(min(range(len(self._gains)),
                                          key=lambda i: abs(self._gains[i] - g)))
        self.gain_slider.setEnabled(not self.gain_auto.isChecked())
        self._update_gain_label()
        self.gain_auto.toggled.connect(self._gain_changed)
        self.gain_slider.valueChanged.connect(self._gain_changed)
        gain_row = QtWidgets.QHBoxLayout()
        gain_row.addWidget(self.gain_auto)
        gain_row.addWidget(self.gain_slider, 1)
        gain_row.addWidget(self.gain_lbl)

        self.ppm_spin = QtWidgets.QSpinBox()
        self.ppm_spin.setRange(-100, 100)
        self.ppm_spin.setValue(int(opts["ppm"]))
        self.ppm_spin.valueChanged.connect(lambda v: self.reader.request(ppm=v))

        self.bias_chk = QtWidgets.QCheckBox("Power an LNA / active antenna through the coax")
        self.bias_chk.setChecked(bool(opts["bias_tee"]))
        self.bias_chk.toggled.connect(lambda on: self.reader.request(bias_tee=on))

        self.auto_band_chk = QtWidgets.QCheckBox(
            "Switch mode and step from the band plan when tuning into a new band")
        self.auto_band_chk.setChecked(bool(opts["auto_band"]))

        radio = QtWidgets.QGroupBox("Radio")
        rf = QtWidgets.QFormLayout(radio)
        dev_row = QtWidgets.QHBoxLayout()
        dev_row.addWidget(self.dev_box, 1)
        dev_row.addWidget(dev_note)
        rf.addRow("Device", dev_row)
        rf.addRow("Sample rate", self.rate_box)
        rf.addRow("Gain", gain_row)
        rf.addRow("PPM correction", self.ppm_spin)
        rf.addRow("Bias tee", self.bias_chk)
        rf.addRow("Band plan", self.auto_band_chk)

        # --- Audio ---
        self.audio_dev_box = QtWidgets.QComboBox()
        self.audio_dev_box.setMinimumWidth(380)
        self.audio_dev_box.currentIndexChanged.connect(self._audio_device_chosen)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._fill_audio_devices(
            select_key=self.opts.get("audio_device_id")))
        tone = QtWidgets.QPushButton("Test tone")
        tone.clicked.connect(lambda: self.audio_out and self.audio_out.test_tone())
        self.audio_status = QtWidgets.QLabel("")
        self.audio_status.setWordWrap(True)
        dev_row2 = QtWidgets.QHBoxLayout()
        dev_row2.addWidget(self.audio_dev_box, 1)
        dev_row2.addWidget(refresh)
        dev_row2.addWidget(tone)

        self.nfm_de_box = QtWidgets.QComboBox()
        for label, us in (("750 µs (standard)", 750), ("530 µs", 530), ("Off", 0)):
            self.nfm_de_box.addItem(label, us)
        self.nfm_de_box.setCurrentIndex(max(0, self.nfm_de_box.findData(int(opts["nfm_deemph_us"]))))
        self.wfm_de_box = QtWidgets.QComboBox()
        for label, us in (("75 µs (Americas)", 75), ("50 µs (Europe)", 50), ("Off", 0)):
            self.wfm_de_box.addItem(label, us)
        self.wfm_de_box.setCurrentIndex(max(0, self.wfm_de_box.findData(int(opts["wfm_deemph_us"]))))
        self.ctcss_chk = QtWidgets.QCheckBox("Filter out CTCSS tones (cut below 300 Hz)")
        self.ctcss_chk.setChecked(bool(opts["ctcss_filter"]))
        for w in (self.nfm_de_box, self.wfm_de_box):
            w.currentIndexChanged.connect(self._demod_opts_changed)
        self.ctcss_chk.toggled.connect(self._demod_opts_changed)

        audio_box = QtWidgets.QGroupBox("Audio")
        af = QtWidgets.QFormLayout(audio_box)
        af.addRow("Output device", dev_row2)
        af.addRow("", self.audio_status)
        af.addRow("NFM de-emphasis", self.nfm_de_box)
        af.addRow("WFM de-emphasis", self.wfm_de_box)
        af.addRow("CTCSS", self.ctcss_chk)
        if audio is None:
            self.audio_status.setText("QtMultimedia not available - audio disabled.")
            self.audio_dev_box.setEnabled(False)

        # --- Display ---
        self.wf_auto = QtWidgets.QCheckBox("Auto (follow noise floor)")
        self.wf_auto.setChecked(bool(opts["wf_auto"]))
        self.wf_min = QtWidgets.QSpinBox()
        self.wf_max = QtWidgets.QSpinBox()
        for sp, v in ((self.wf_min, opts["wf_min"]), (self.wf_max, opts["wf_max"])):
            sp.setRange(-150, 20)
            sp.setSuffix(" dB")
            sp.setValue(int(v))
            sp.setEnabled(not self.wf_auto.isChecked())
        self.wf_auto.toggled.connect(lambda on: (self.wf_min.setEnabled(not on),
                                                 self.wf_max.setEnabled(not on)))
        wf_row = QtWidgets.QHBoxLayout()
        wf_row.addWidget(self.wf_auto)
        wf_row.addWidget(QtWidgets.QLabel("min"))
        wf_row.addWidget(self.wf_min)
        wf_row.addWidget(QtWidgets.QLabel("max"))
        wf_row.addWidget(self.wf_max)
        wf_row.addStretch(1)

        self.pal_box = QtWidgets.QComboBox()
        self.pal_box.addItems(PALETTES)
        self.pal_box.setCurrentText(opts["palette"])
        self.pal_box.currentTextChanged.connect(self._set_palette)

        self.avg_box = QtWidgets.QComboBox()
        self.avg_box.addItems(list(AVERAGING))
        self.avg_box.setCurrentText(opts["averaging"])

        self.tags_chk = QtWidgets.QCheckBox("Label preset frequencies under the waterfall")
        self.tags_chk.setChecked(bool(opts["preset_tags"]))
        self.tags_chk.toggled.connect(lambda _on: self._draw_preset_tags())

        display = QtWidgets.QGroupBox("Display")
        df = QtWidgets.QFormLayout(display)
        df.addRow("Waterfall range", wf_row)
        df.addRow("Palette", self.pal_box)
        df.addRow("Spectrum averaging", self.avg_box)
        df.addRow("Preset tags", self.tags_chk)

        # --- Recording ---
        self.rec_dir_edit = QtWidgets.QLineEdit(opts["rec_dir"] or str(RECORDINGS))
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse_rec_dir)
        rec_row = QtWidgets.QHBoxLayout()
        rec_row.addWidget(self.rec_dir_edit, 1)
        rec_row.addWidget(browse)
        recording = QtWidgets.QGroupBox("Recording")
        recf = QtWidgets.QFormLayout(recording)
        recf.addRow("Folder", rec_row)

        # --- Speech-to-text ---
        self.stt_box = QtWidgets.QComboBox()
        for label, name in transcribe.MODELS.items():
            self.stt_box.addItem(label, name)
        i = self.stt_box.findData(opts.get("stt_model", transcribe.DEFAULT_MODEL))
        self.stt_box.setCurrentIndex(max(0, i))
        self.stt_box.currentIndexChanged.connect(self._set_stt_model)
        stt_note = QtWidgets.QLabel(
            "Transcribes clear P25 calls and analog NFM/AM (squelch above 0), on this PC (nothing goes online). Fast: a few seconds "
            "per call. Accurate: better with codes and street names, slower, 1.6 GB download "
            "the first time." if transcribe.available() else
            "Off: run  pip install faster-whisper  in the VSCode terminal, then restart.")
        stt_note.setWordWrap(True)
        stt_note.setMinimumWidth(420)
        stt_note.setStyleSheet("color: gray;")
        stt = QtWidgets.QGroupBox("Speech-to-text")
        sf = QtWidgets.QFormLayout(stt)
        sf.addRow("Model", self.stt_box)
        sf.addRow("", stt_note)

        # --- Maps ---
        self.carto_edit = QtWidgets.QLineEdit(str(opts.get("carto_key", "")))
        self.carto_edit.setPlaceholderText("paste your free CARTO basemaps key")
        self.carto_edit.setClearButtonEnabled(True)
        self.carto_edit.editingFinished.connect(self._set_carto_key)
        carto_note = QtWidgets.QLabel(
            "Optional. Free for personal use (5M tiles/month): request one at "
            "<a href='https://carto.com/basemaps/apikey/'>carto.com/basemaps/apikey</a>. "
            "Empty = Esri dark grey map, no key needed.")
        carto_note.setOpenExternalLinks(True)
        carto_note.setWordWrap(True)
        carto_note.setMinimumWidth(420)
        carto_note.setStyleSheet("color: gray;")
        self.fa_edit = QtWidgets.QLineEdit(str(opts.get("aeroapi_key", "")))
        self.fa_edit.setPlaceholderText("paste your FlightAware AeroAPI (Personal) key")
        self.fa_edit.setClearButtonEnabled(True)
        self.fa_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.fa_edit.editingFinished.connect(
            lambda: self.opts.__setitem__("aeroapi_key", self.fa_edit.text().strip()))
        self.fa_cap = QtWidgets.QSpinBox()
        self.fa_cap.setRange(0, 100_000)
        self.fa_cap.setSuffix(" lookups / month")
        self.fa_cap.setValue(int(opts.get("aeroapi_cap", 900)))
        self.fa_cap.valueChanged.connect(lambda v: self.opts.__setitem__("aeroapi_cap", int(v)))
        self.fa_used = QtWidgets.QLabel()
        fa_row = QtWidgets.QHBoxLayout()
        fa_row.addWidget(self.fa_cap)
        fa_row.addWidget(self.fa_used, 1)
        fa_note = QtWidgets.QLabel(
            "Optional: today's real route, times and delays when you click a plane. Personal tier "
            "at <a href='https://www.flightaware.com/aeroapi/'>flightaware.com/aeroapi</a> includes "
            "$5 free usage a month (~1,000 lookups at ~$0.005). The cap stops lookups before that; "
            "then it falls back to adsbdb + the flight-path check.")
        fa_note.setOpenExternalLinks(True)
        fa_note.setWordWrap(True)
        fa_note.setMinimumWidth(420)
        fa_note.setStyleSheet("color: gray;")
        self._fa_timer = QtCore.QTimer(self)
        self._fa_timer.timeout.connect(self._show_fa_usage)
        self._fa_timer.start(3000)

        maps = QtWidgets.QGroupBox("Maps and flight info")
        mf = QtWidgets.QFormLayout(maps)
        mf.addRow("CARTO key", self.carto_edit)
        mf.addRow("", carto_note)
        mf.addRow("FlightAware key", self.fa_edit)
        mf.addRow("Monthly cap", fa_row)
        mf.addRow("", fa_note)

        # --- Phone alerts (ntfy) ---
        self.ntfy_edit = QtWidgets.QLineEdit(str(opts.get("ntfy_topic", "")))
        self.ntfy_edit.setPlaceholderText("your private ntfy topic name")
        self.ntfy_edit.editingFinished.connect(
            lambda: self.opts.__setitem__("ntfy_topic", self.ntfy_edit.text().strip()))
        gen = QtWidgets.QPushButton("New random topic")
        gen.clicked.connect(lambda: (self.ntfy_edit.setText(notify.random_topic()),
                                     self.opts.__setitem__("ntfy_topic", self.ntfy_edit.text())))
        test = QtWidgets.QPushButton("Send test")
        test.clicked.connect(lambda: notify.send(self.ntfy_edit.text(), "smashDR test",
                                                 "Phone alerts work.", priority="default",
                                                 tags="white_check_mark"))
        nrow = QtWidgets.QHBoxLayout()
        nrow.addWidget(self.ntfy_edit, 1)
        nrow.addWidget(gen)
        nrow.addWidget(test)
        self.ntfy_air = QtWidgets.QCheckBox("Aircraft emergencies (7500 / 7600 / 7700, emergency status)")
        self.ntfy_air.setChecked(bool(opts.get("ntfy_aircraft", True)))
        self.ntfy_air.toggled.connect(lambda v: self.opts.__setitem__("ntfy_aircraft", bool(v)))
        self.ntfy_radio = QtWidgets.QCheckBox("Radio emergencies (P25 / DMR emergency flag)")
        self.ntfy_radio.setChecked(bool(opts.get("ntfy_radio", True)))
        self.ntfy_radio.toggled.connect(lambda v: self.opts.__setitem__("ntfy_radio", bool(v)))
        ntfy_note = QtWidgets.QLabel(
            "Free push alerts to your phone: install the <b>ntfy</b> app, tap + and subscribe to the "
            "same topic name as here, then press Send test. Keep the name private: anyone who "
            "knows it can read the alerts.")
        ntfy_note.setWordWrap(True)
        ntfy_note.setMinimumWidth(420)
        ntfy_note.setStyleSheet("color: gray;")
        alerts = QtWidgets.QGroupBox("Phone alerts")
        af = QtWidgets.QFormLayout(alerts)
        af.addRow("ntfy topic", nrow)
        af.addRow("Send for", self.ntfy_air)
        af.addRow("", self.ntfy_radio)
        af.addRow("", ntfy_note)

        # two compact columns instead of full-width rows across a wide screen
        cols = QtWidgets.QHBoxLayout()
        cols.setSpacing(16)
        accents = {radio: "#4fc3f7", audio_box: "#81c784", display: "#ffb74d",
                   recording: "#e57373", stt: "#ba68c8", maps: "#4db6ac", alerts: "#ff8a65"}
        for g, c in accents.items():
            g.setStyleSheet(f"""
                QGroupBox {{ background: #1f2226; border: 1px solid #33383d;
                             border-left: 4px solid {c}; border-radius: 6px;
                             margin-top: 16px; padding: 10px 8px 6px 8px; }}
                QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;
                                    color: {c}; font-weight: 600; font-size: 11pt; }}
                QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
                    background: {c}; border: 1px solid {c}; border-radius: 3px; }}
                QSlider::sub-page:horizontal {{ background: {c}; border-radius: 2px; }}
                QSlider::handle:horizontal {{ background: {c}; width: 12px; margin: -5px 0;
                                              border-radius: 6px; }}
                QSlider::groove:horizontal {{ height: 4px; background: #3a3f44; border-radius: 2px; }}
                QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
                    border: 1px solid {c}; }}
            """)
        for groups in ((radio, audio_box, display, recording), (stt, maps, alerts)):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(10)
            for g in groups:
                g.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                QtWidgets.QSizePolicy.Policy.Maximum)
                for form in g.findChildren(QtWidgets.QFormLayout):
                    form.setFieldGrowthPolicy(
                        QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
                    form.setVerticalSpacing(6)
                col.addWidget(g)
            col.addStretch(1)
            holder = QtWidgets.QWidget()
            holder.setLayout(col)
            holder.setMaximumWidth(760)
            cols.addWidget(holder, 1, QtCore.Qt.AlignmentFlag.AlignTop)
        cols.addStretch(0)
        inner = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(inner)
        outer.addLayout(cols)
        outer.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    # ======================================================================
    # Tuning
    # ======================================================================
    @staticmethod
    def _step_text(s):
        return f"{s / 1000:g} kHz" if s < 1_000_000 else f"{s / 1e6:g} MHz"

    def _select_step(self, s):
        i = self.step_box.findData(int(s))
        if i < 0:
            self.step_box.addItem(self._step_text(int(s)), int(s))
            i = self.step_box.count() - 1
        self.step_box.blockSignals(True)
        self.step_box.setCurrentIndex(i)
        self.step_box.blockSignals(False)

    def _set_step(self, s):
        self.step_hz = int(s)
        self.vfo.step_hz = self.step_hz

    def _offset_hz(self):
        rate = self.rate_box.currentData()
        return min(300e3, rate * 0.2)

    def _wheel_tune(self, direction: int):
        base = round(self.target_hz / self.step_hz) * self.step_hz
        self._tune(base + direction * self.step_hz)

    def _tune(self, hz, snap=False, apply_band_defaults=True):
        hz = float(hz)
        if snap:
            hz = round(hz / self.step_hz) * self.step_hz
        hz = int(round(min(max(hz, 24e6), 1766e6)))
        if self.opts.get("debug_tune", True):          # temporary: chasing the click-jump bug
            import inspect
            caller = inspect.stack()[1].function
            view = self.spec_plot.getViewBox().viewRange()[0] if hasattr(self, "spec_plot") else None
            print(f"[tune] {hz / 1e6:.4f} MHz from {caller}  centre "
                  f"{(self.center_hz or 0) / 1e6:.4f}  view "
                  f"{'%.4f-%.4f' % tuple(view) if view else '?'}", flush=True)
        if hasattr(self, "cc_lbl") and abs(hz - self.target_hz) > 1000:
            self.cc_lbl.clear()                     # another channel: its own system info follows
            self.cc_lbl.hide()
        self.target_hz = hz
        self.vfo.setValue(hz)

        band = bandplan.lookup(hz)
        label = band[3] if band else None
        if apply_band_defaults and label != self._band_label and band \
                and self.auto_band_chk.isChecked():
            if band[4] and band[4] != self.mode:
                self._set_mode(band[4])
            if band[5]:
                self._select_step(band[5])
                self._set_step(band[5])
        self._band_label = label
        self.band_lbl.setText(bandplan.describe(hz))

        mhz = hz / 1e6
        self.marker.blockSignals(True)
        self.marker.setValue(mhz)
        self.marker.blockSignals(False)
        self.wf_marker.setValue(mhz)
        self._update_region()
        self.reader.request(target=float(hz))
        if self.presets_win is not None:
            self.presets_win.set_active(hz, self.mode)

        # re-centre the hardware if the channel would fall off the span
        if self.center_hz is not None and self.rate_hz:
            half = MODES[self.mode]["half_bw"]
            if abs(hz - self.center_hz) + half > 0.42 * self.rate_hz:
                self.reader.request(center=hz + self._offset_hz())
                self._click_block_until = time.time() + 0.6
                self.rec_btn.setChecked(False)
        elif self.center_hz is None:
            self.reader.request(center=hz + self._offset_hz())

    def _update_region(self):
        half = MODES[self.mode]["half_bw"] / 1e6
        mhz = self.target_hz / 1e6
        self.chan_region.setRegion((mhz - half, mhz + half))

    def _set_mode(self, mode):
        self.mode = mode
        self._apply_mode_ui()
        self.reader.request(mode=mode)

    def _apply_mode_ui(self):
        btn = self.mode_btns.get(self.mode)
        if btn:
            btn.setChecked(True)
        if hasattr(self, "chan_region"):
            self._update_region()
        if hasattr(self, "cc_lbl") and self.mode not in DIGITAL_MODES:
            self.cc_lbl.hide()
        if hasattr(self, "p25_lbl"):
            self.p25_lbl.setVisible(self.mode in DIGITAL_MODES)
            if self.mode in DIGITAL_MODES and not self.p25_lbl.text():
                self.p25_lbl.setText(f"<span style='color:gray'>{self.mode}: waiting for a transmission…</span>")

    # ======================================================================
    # P25 call readout
    # ======================================================================
    def _aircraft_emergency(self, text):
        """Flash the Aircraft tab while any plane has a confirmed emergency squawk."""
        i = self.tabs.indexOf(self.aircraft_tab)
        bar = self.tabs.tabBar()
        if text:
            self.tabs.setTabText(i, "Aircraft ⚠")
            bar.setTabTextColor(i, QtGui.QColor("#ff5252"))
            if text != getattr(self, "_last_emerg", ""):
                self.status_lbl.setText(f"⚠ Aircraft emergency: {text}")
        else:
            self.tabs.setTabText(i, "Aircraft")
            bar.setTabTextColor(i, bar.palette().color(QtGui.QPalette.ColorRole.WindowText))
        self._last_emerg = text

    def _show_fa_usage(self):
        if hasattr(self, "aircraft_tab"):
            used, cap = self.aircraft_tab.fa_usage()
            cost = used * flightaware.COST_PER_CALL
            self.fa_used.setText(f"used this month: {used} (~${cost:.2f} of the $5 free credit)")

    def _set_carto_key(self):
        key = self.carto_edit.text().strip()
        if key != str(self.opts.get("carto_key", "")):
            self.opts["carto_key"] = key
            self.aircraft_tab.reload_map()
            self.status_lbl.setText("Map key " + ("saved, map reloaded" if key else "cleared"))

    def _stt_toggled(self, _on=None):
        self.reader.stt_on = self.stt_chk.isChecked()
        self.reader.stt_skip_am = self.stt_am_chk.isChecked()
        self.stt_am_chk.setEnabled(self.stt_chk.isChecked())
        self.opts["stt_on"] = self.stt_chk.isChecked()
        self.opts["stt_skip_am"] = self.stt_am_chk.isChecked()

    def _set_stt_model(self, _i):
        name = self.stt_box.currentData()
        if self.reader.transcriber is not None:
            self.reader.transcriber.set_model(name)
            self.status_lbl.setText(f"Speech-to-text model: {name} (loads on the next call)")

    def on_transcript(self, r: dict):
        stamp = time.strftime("%H:%M:%S", time.localtime(r.get("t_end", time.time())))
        name = self._preset_name(r.get("freq_hz"))
        who = []
        if r.get("proto") == "YSF":
            who.append(f"{r.get('src') or '?'} (Fusion)")
        elif r.get("proto") == "DSTAR":
            who.append(f"{r.get('my') or '?'} (D-STAR)")
        else:
            if r.get("tgid") is not None:
                who.append(f"TG {r['tgid']}")
            if r.get("src") is not None:
                who.append(f"radio {r['src']}")
            if r.get("proto") == "DMR":
                who.append("DMR")
        if r.get("type") == "analog":
            who.append(r.get("mode", ""))
        head = f"{stamp}  {name}  {'  '.join(who)}  ({r.get('audio_s', 0):.1f} s)"
        if r.get("error"):
            body = f"<span style='color:#e53935'>{html.escape(r['error'])}</span>"
            text = "[error] " + r["error"]
        else:
            text = r.get("text") or "(nothing recognised)"
            body, text = self._explain_codes(text)
        self.tx_view.append(f"<p style='margin:0 0 8px 0'><span style='color:gray'>"
                            f"{html.escape(head)}</span><br>{body}</p>")
        line = f"{head}  ->  {text}"
        print("[stt]", line, f"[{r.get('model', '?')} {r.get('took_s', 0):.1f} s]")
        try:
            with open(USER / "transcripts.txt", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass

    def _explain_codes(self, text):
        """(html with codes explained + places linked to Google Maps, plain text for the log)."""
        marks = [(s0, e0, "place", q) for s0, e0, q in places.find_places(text)]
        for match, meaning, s0, e0 in codes.annotate(text):
            if all(e0 <= a or s0 >= b for a, b, _k, _v in marks):
                marks.append((s0, e0, "code", meaning))
        marks.sort()
        parts_h, parts_t, pos = [], [], 0
        for s0, e0, kind, val in marks:
            seg = text[s0:e0]
            parts_h.append(html.escape(text[pos:s0]))
            parts_t.append(text[pos:s0])
            if kind == "place":
                # the link only carries an id; the real URL is looked up on click
                self._place_links.append(places.maps_url(val))
                parts_h.append(f"<a href='place:{len(self._place_links) - 1}' "
                               f"style='color:#4fc3f7'>{html.escape(seg)}</a>")
                parts_t.append(seg)
            else:
                parts_h.append(f"<b style='color:#ffd54f'>{html.escape(seg)}</b> "
                               f"<span style='color:#9fa8da'>[{html.escape(val)}]</span>")
                parts_t.append(f"{seg} [{val}]")
            pos = e0
        parts_h.append(html.escape(text[pos:]))
        parts_t.append(text[pos:])
        return "".join(parts_h), "".join(parts_t)

    def _open_place(self, url):
        ref = url.toString()
        if ref.startswith("place:"):
            try:
                target = self._place_links[int(ref[6:])]
            except (ValueError, IndexError):
                return
            print("[map]", target)
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(target))

    def _preset_name(self, freq_hz) -> str:
        if freq_hz is not None:
            for p in self._tag_presets:
                if abs(p["freq_hz"] - freq_hz) < 1000:
                    return p["name"]
            return f"{freq_hz / 1e6:.4f}"
        return ""

    GRANT_STYLE = {"playing": ("#43a047", "▶ playing"), "clear": ("#43a047", "clear"),
                   "encrypted": ("#e53935", "encrypted"), "checking": ("#9e9e9e", "checking…"),
                   "phase 2": ("#9e9e9e", "Phase 2, not yet"), "out of span": ("#9e9e9e", "outside the span"),
                   "busy": ("#9e9e9e", "not followed (max 3)"), "no plan": ("#9e9e9e", "waiting for the band plan")}

    def _show_cc(self, ev):
        """Control channel line: system, site and the grants being followed."""
        if ev.get("proto") == "DMR":
            head = (f"<span style='color:#8e7cc3'>■</span>  DMR control channel  ·  colour code "
                    f"{ev['cc']}  ·  {ev['csbk_per_s']:.0f} CSBK/s <span style='color:gray'>"
                    "(DMR trunk following not built yet)</span>")
            self.cc_lbl.setText(head)
            self.cc_lbl.show()
            return
        sysname = ""
        if ev.get("sysid") is not None:
            sysname = (f"system <b>{ev['wacn']:05X}-{ev['sysid']:03X}</b>" if ev.get("wacn") is not None
                       else f"system <b>{ev['sysid']:03X}</b>")
            if ev.get("site") is not None:
                sysname += f"  ·  RFSS {ev.get('rfss')} site {ev['site']}"
            sysname += "  ·  "
        grants = []
        for g in sorted(ev.get("active") or [], key=lambda g: g.get("state") != "playing")[:6]:
            f = f"{g['freq'] / 1e6:.4f}" if g.get("freq") else f"ch {g['channel']}"
            col, word = self.GRANT_STYLE.get(g.get("state"), ("#9e9e9e", g.get("state") or ""))
            tg = f"TG <b>{g['tgid']}</b> → {f} <span style='color:{col}'>{word}</span>"
            if g.get("emergency"):
                tg += " <span style='color:#e53935'><b>EMERGENCY</b></span>"
            grants.append(tg)
        now = ("<br>" + "  ·  ".join(grants)) if grants else \
            "  ·  <span style='color:gray'>no calls right now</span>"
        self.cc_lbl.setText(f"<span style='color:#8e7cc3'>■</span>  P25 control channel  ·  {sysname}"
                            f"NAC 0x{ev['nac']:03X}{now}")
        self.cc_lbl.show()
        if self.p25_lbl.text().startswith("<span style='color:gray'>P25: waiting"):
            self.p25_lbl.setText("<span style='color:gray'>following grants inside the span; "
                                 "clear calls play here</span>")

    def on_p25_event(self, ev: dict):
        if ev["type"] == "cc":
            self._show_cc(ev)
            return
        if self.presets_win is not None:
            self.presets_win.set_live(None if ev["type"] == "end" else self.target_hz)
        proto = ev.get("proto", "P25")
        tg = ev.get("tgid")
        src = ev.get("src")
        parts = []
        if proto == "DSTAR":
            me = html.escape(ev.get("my") or "?") + (f"/{html.escape(ev['sfx'])}" if ev.get("sfx") else "")
            who = f"<b>{me}</b>"
            if ev.get("ur"):
                who += f" → {html.escape(ev['ur'])}"
            parts.append(who)
            if ev.get("rpt1"):
                parts.append(f"via {html.escape(ev['rpt1'])}")
            if ev.get("msg"):
                parts.append(f"“{html.escape(ev['msg'])}”")
            parts.append("D-STAR")
            src = None
        elif proto == "YSF":
            who = f"<b>{html.escape(src or '?')}</b>"
            if ev.get("dst"):
                who += f" → {html.escape(ev['dst'])}"
            parts.append(who)
            if ev.get("via"):
                parts.append(f"via {html.escape(ev['via'])}")
            parts.append(f"Fusion {html.escape(ev.get('mode', ''))}")
        elif tg is not None:
            parts.append(f"TG <b>{tg}</b>")
        elif ev.get("dst") is not None:
            parts.append(f"to unit <b>{ev['dst']}</b>")
        if src is not None:
            parts.append(f"radio <b>{src}</b>")
        if proto == "P25":
            parts.append(f"NAC 0x{ev['nac']:03X}")
            if ev.get("freq"):
                parts.append(f"on {ev['freq'] / 1e6:.4f}")
        elif proto == "DMR":
            parts.append(f"slot {ev.get('slot')}" + (f"  ·  CC {ev['cc']}" if ev.get("cc") is not None else ""))
        if ev.get("emergency"):
            parts.append("<span style='color:#e53935'><b>EMERGENCY</b></span>")
            key = (proto, ev.get("tgid"), ev.get("src"), round(time.time() / 120))
            if self.opts.get("ntfy_radio", True) and key != getattr(self, "_radio_emerg_key", None):
                self._radio_emerg_key = key
                notify.send(self.opts.get("ntfy_topic", ""),
                            f"{proto} emergency on {self._preset_name(self.target_hz)}",
                            f"TG {ev.get('tgid')}  radio {ev.get('src')}  {self.target_hz / 1e6:.4f} MHz",
                            priority="urgent", tags="rotating_light")
        if ev.get("encrypted"):
            alg = f" ({p25mod.algid_name(ev['algid'])})" if ev.get("algid") is not None else ""
            enc = f"<span style='color:#e53935'>ENCRYPTED{alg}, not decoded</span>"
        elif ev.get("encrypted") is False:
            enc = "<span style='color:#43a047'>clear</span>"
        else:
            enc = "<span style='color:gray'>checking…</span>"
        parts.append(enc)
        body = "  ·  ".join(parts)
        if ev["type"] == "end":
            stamp = time.strftime("%H:%M:%S")
            self.p25_lbl.setText(f"<span style='color:gray'>last call {stamp} "
                                 f"({ev['duration']:.1f} s):</span>  {body}")
        else:
            self.p25_lbl.setText(f"<span style='color:#43a047'>●</span>  {body}")

    def _plot_clicked(self, ev, plot):
        vb = plot.plotItem.vb
        print(f"[click] {type(plot).__name__} double={ev.double()} "
              f"x={vb.mapSceneToView(ev.scenePos()).x():.4f}", flush=True)
        # a double click (or a second click while the span is still moving) would be
        # mapped onto the new span and tune somewhere else
        if ev.double() or time.time() < getattr(self, "_click_block_until", 0.0):
            return
        if ev.button() == QtCore.Qt.MouseButton.LeftButton and \
                vb.sceneBoundingRect().contains(ev.scenePos()):
            self._tune(vb.mapSceneToView(ev.scenePos()).x() * 1e6, snap=True)

    # ======================================================================
    # Presets window
    # ======================================================================
    def show_presets(self):
        if self.presets_win is None:
            self.presets_win = PresetsWindow(self._current_for_preset)
            self.presets_win.tune_requested.connect(self._tune_preset)
            self.presets_win.presets_changed.connect(self._on_presets_changed)
            # v0.2.5 made the window bigger: place it once more on the second screen
            if (self.opts.get("presets_layout") != 2
                    or not self._restore(self.presets_win, self.opts.get("presets_geometry"))):
                self._place_presets_window(self.presets_win)
                self.opts["presets_layout"] = 2
            self.presets_win.set_active(self.target_hz, self.mode)
        self.presets_win.show()
        self.presets_win.raise_()
        self.presets_win.activateWindow()

    @staticmethod
    def _restore(win, hex_geo) -> bool:
        if not hex_geo:
            return False
        try:
            return bool(win.restoreGeometry(QtCore.QByteArray.fromHex(hex_geo.encode())))
        except Exception:
            return False

    def _place_main_window(self, hex_geo):
        """Last position if known, else big and centred on the primary screen."""
        if self._restore(self, hex_geo):
            return
        scr = QtGui.QGuiApplication.primaryScreen()
        if scr is None:
            self.resize(1600, 1000)
            return
        g = scr.availableGeometry()
        w, h = int(g.width() * 0.85), int(g.height() * 0.88)
        self.setGeometry(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2, w, h)

    def _place_presets_window(self, win):
        """First time: fill the secondary screen if there is one."""
        primary = QtGui.QGuiApplication.primaryScreen()
        others = [s for s in QtGui.QGuiApplication.screens() if s is not primary]
        screen = others[0] if others else primary
        avail = screen.availableGeometry()
        w, h = min(1900, avail.width()), min(1040, avail.height())
        win.resize(w, h)
        win.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)

    def _current_for_preset(self) -> dict:
        return {"freq_hz": int(self.target_hz), "mode": self.mode,
                "step_hz": int(self.step_hz), "squelch_db": int(self.sq_slider.value())}

    def _tune_preset(self, p: dict):
        if p.get("mode") in MODES and p["mode"] != self.mode:
            self._set_mode(p["mode"])
        if p.get("step_hz"):
            self._select_step(p["step_hz"])
            self._set_step(p["step_hz"])
        if p.get("squelch_db") is not None:
            self.sq_slider.setValue(int(p["squelch_db"]))
        digital = (p.get("digital") or "").upper()
        want = ("P25" if digital.startswith("P25") else
                "DMR" if digital.startswith("DMR") or "MOTOTRBO" in digital else
                "YSF" if any(k in digital for k in ("YSF", "FUSION", "C4FM")) else
                "DSTAR" if digital.replace("-", "").startswith("DSTAR") else None)
        if want and self.mode != want:
            self._set_mode(want)
        self._tune(p["freq_hz"], apply_band_defaults=False)
        if digital.startswith("P25"):
            self.status_lbl.setText(f"{p['name']}  ·  P25" +
                                    (f"  ·  {p['note']}" if p.get("note") else ""))
            if "PHASE II" in digital:
                self.status_lbl.setText(self.status_lbl.text() +
                                        "  (control channel: frames show, voice is Phase II, not yet)")
        elif want:
            self.status_lbl.setText(f"{p['name']}  ·  {want}" +
                                    (f"  ·  {p['note']}" if p.get("note") else ""))
        elif digital:
            self.status_lbl.setText(f"{p['name']} is {digital}: that decoder isn't built yet, "
                                    "you'll hear digital noise in NFM.")
        else:
            self.status_lbl.setText(f"{p['name']}" + (f"  ·  {p['note']}" if p.get("note") else ""))

    # ======================================================================
    # Controls
    # ======================================================================
    def _set_squelch(self, v):
        self.sq_val.setText("open" if v == 0 else f"{v} dB")
        self.reader.request(squelch=float(v))

    def _set_volume(self, v):
        if self.audio_out:
            self.audio_out.set_volume((v / 100) ** 2)

    def _set_rate(self, _idx):
        self.reader.request(rate=self.rate_box.currentData(),
                            center=self.target_hz + self._offset_hz())
        self.rec_btn.setChecked(False)

    def _update_gain_label(self):
        self.gain_lbl.setText("auto" if self.gain_auto.isChecked()
                              else f"{self._gains[self.gain_slider.value()]:.1f} dB")

    def _gain_text(self):
        return "auto" if self.gain_auto.isChecked() else \
            f"{self._gains[self.gain_slider.value()]:.1f}"

    def _gain_changed(self, *_):
        self.gain_slider.setEnabled(not self.gain_auto.isChecked())
        self._update_gain_label()
        g = self._gain_text()
        self.reader.request(gain="auto" if g == "auto" else float(g))
        self.rec_btn.setChecked(False)

    def _device_chosen(self, idx):
        self.opts["device_index"] = idx
        self._dev_note.setText("applies after restart" if idx != self._open_device_index() else "")

    def _open_device_index(self):
        return int(self.opts.get("_opened_device", 0))

    def _demod_opts_changed(self, *_):
        self.reader.request(demod_opts={
            "nfm_deemph_us": self.nfm_de_box.currentData(),
            "wfm_deemph_us": self.wfm_de_box.currentData(),
            "ctcss_filter": self.ctcss_chk.isChecked(),
        })

    def _set_palette(self, name):
        try:
            if name == "sdr":
                pos, cols = zip(*SDR_PALETTE)
                self.wf_img.setColorMap(pg.ColorMap(list(pos), [c + (255,) for c in cols]))
            else:
                self.wf_img.setColorMap(pg.colormap.get(name))
        except Exception:
            self.wf_img.setColorMap(pg.colormap.get("inferno"))

    def _browse_rec_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                       self.rec_dir_edit.text())
        if d:
            self.rec_dir_edit.setText(d)

    def toggle_record(self, on):
        if on:
            self.reader.request(rec_start={"target_hz": float(self.target_hz),
                                           "gain": self._gain_text(),
                                           "ppm": self.ppm_spin.value(),
                                           "dir": self.rec_dir_edit.text().strip()})
            self.rec_btn.setText("Stop rec")
        else:
            self.reader.request(rec_stop=True)
            self.rec_btn.setText("Record IQ")

    # ======================================================================
    # Audio
    # ======================================================================
    def _fill_audio_devices(self, select_key=None):
        if not self.audio_out:
            return
        self._audio_devs = audio.output_devices()
        default_key = audio.device_key(audio.default_device())
        self.audio_dev_box.blockSignals(True)
        self.audio_dev_box.clear()
        idx = 0
        for i, dev in enumerate(self._audio_devs):
            key = audio.device_key(dev)
            label = dev.description() + ("  (system default)" if key == default_key else "")
            self.audio_dev_box.addItem(label)
            if key == (select_key or default_key):
                idx = i
        self.audio_dev_box.blockSignals(False)
        if self._audio_devs:
            self.audio_dev_box.setCurrentIndex(idx)
            self._audio_device_chosen(idx)
        else:
            self.audio_status.setText("No audio output devices found.")

    def _audio_device_chosen(self, idx):
        if not self.audio_out or idx < 0 or idx >= len(self._audio_devs):
            return
        dev = self._audio_devs[idx]
        try:
            desc = self.audio_out.open(dev)
            self.audio_status.setText(f"Playing on: {desc}")
            self.opts["audio_device_id"] = audio.device_key(dev)
            self.opts["audio_device_name"] = dev.description()
        except Exception as e:
            self.audio_status.setText(f"Could not open {dev.description()}: {e}")

    def _on_audio(self, pcm: bytes):
        if self.audio_out and not self.mute_chk.isChecked():
            self.audio_out.write(pcm)

    # ======================================================================
    # Frames
    # ======================================================================
    def _on_presets_changed(self, presets):
        self._tag_presets = presets
        self._draw_preset_tags()

    @staticmethod
    def _tag_tooltip(presets) -> str:
        rows = []
        for p in presets:
            kind = p.get("digital") or p["mode"]
            line = (f"<b>{html.escape(p['name'])}</b><br>"
                    f"{p['freq_hz'] / 1e6:.4f} MHz &nbsp;·&nbsp; {html.escape(kind)}"
                    f" &nbsp;·&nbsp; {html.escape(p['group'])}")
            if p.get("note"):
                line += f"<br><span style='color:gray'>{html.escape(p['note'])}</span>"
            rows.append(line)
        return "<p style='white-space:pre-wrap; max-width:420px'>" + "<hr>".join(rows) + "</p>"

    def _draw_preset_tags(self):
        """Preset names in the strip under the waterfall."""
        show = self.tags_chk.isChecked() and self.center_hz is not None
        self.tag_strip.setVisible(self.tags_chk.isChecked())
        if not show:
            self.tag_strip.set_tags([], 0, 1)
            return
        self.tag_strip.set_tags(self._tag_presets, self.center_hz - self.rate_hz / 2,
                                self.center_hz + self.rate_hz / 2)

    def on_frame(self, spec, center, rate, stats):
        if center != self.center_hz or rate != self.rate_hz:
            self.center_hz, self.rate_hz = center, rate
            bins = (np.arange(FFT_SIZE) - FFT_SIZE / 2) * (rate / FFT_SIZE)
            self.freqs_mhz = (center + bins) / 1e6
            self.avg = spec.copy()
            self.wf[:] = np.median(spec) - 10          # empty rows read as black
            self._wf_lv = None
            f0 = self.freqs_mhz[0]
            span = rate / 1e6
            self._wf_rect = QtCore.QRectF(f0, 0, span, WF_ROWS)
            self.spec_plot.setXRange(f0, f0 + span, padding=0)
            self.band_strip.redraw(center - rate / 2, center + rate / 2)
            self._draw_preset_tags()

        alpha = AVERAGING.get(self.avg_box.currentText(), 0.35)
        self.avg = (1 - alpha) * self.avg + alpha * spec
        self.spec_curve.setData(self.freqs_mhz, self.avg)

        rows = stats.get("wf_rows")
        if rows is None:
            rows = spec[None, :]
        n = min(len(rows), WF_ROWS)
        self.wf[:-n] = self.wf[n:]
        self.wf[-n:] = rows[-n:]
        floor = float(np.median(spec))
        if self.wf_auto.isChecked():
            # noise just above black, strongest recent signal near the top (smoothed)
            self._wf_n = getattr(self, "_wf_n", 0) + 1
            if self._wf_n % 10 == 1 or getattr(self, "_wf_lv", None) is None:
                recent = self.wf[-60:]
                lo = float(np.percentile(recent, 40)) - 1.5
                hi = max(float(np.percentile(recent, 99.8)) + 2.0, lo + 18.0)
                old = getattr(self, "_wf_lv", None)
                self._wf_lv = (lo, hi) if old is None else (
                    0.7 * old[0] + 0.3 * lo, 0.7 * old[1] + 0.3 * hi)
            lo, hi = self._wf_lv
            lo += self.wf_bright.value()
            hi = lo + (hi - self._wf_lv[0]) * 100.0 / max(50, self.wf_contrast.value())
            levels = (lo, max(hi, lo + 3.0))
        else:
            levels = (self.wf_min.value(), max(self.wf_max.value(), self.wf_min.value() + 1))
        self.wf_img.setImage(self.wf, autoLevels=False, levels=levels)
        if self._wf_rect is not None:
            # setRect scales by the image size, so it only works once an image is set
            self.wf_img.setRect(self._wf_rect)
            self._wf_rect = None

        self.meter.set_values(stats["snr"], self.sq_slider.value(), stats["sq_open"])

        clip_txt = f"clip {stats['clip']:.3f}%"
        if stats["clip"] > 0.1:
            clip_txt += "  << OVERLOAD, lower gain"
        extra = f"   REC {stats['rec_mb']:.0f} MB" if stats["rec_mb"] is not None else ""
        if stats["drops"]:
            extra += f"   DROPPED {stats['drops']} blocks"
        self.info.setText(f"{'SQ OPEN' if stats['sq_open'] else 'SQ shut'}   "
                          f"{stats['snr']:4.0f} dB above floor   floor {floor:6.1f} dBFS   "
                          f"{clip_txt}   {rate / 1e6:.2f} MS/s   gain {self._gain_text()}{extra}")

    # ======================================================================
    # Shutdown
    # ======================================================================
    def _collect_opts(self):
        """Current UI state -> self.opts."""
        self.opts["main_geometry"] = bytes(self.saveGeometry().toHex()).decode()
        if self.presets_win is not None:
            self.opts["presets_open"] = self.presets_win.isVisible()
            self.opts["presets_geometry"] = bytes(self.presets_win.saveGeometry().toHex()).decode()
        else:
            self.opts["presets_open"] = False
        self.aircraft_tab.collect()
        self.opts.update(
            tab=self.tabs.currentIndex(),
            split_tx=self.tx_split.sizes(),
            split_spec=self.spec_split.sizes(),
            freq_mhz=round(self.target_hz / 1e6, 6),
            mode=self.mode,
            step_hz=self.step_hz,
            gain=self._gain_text(),
            rate_msps=self.rate_box.currentData() / 1e6,
            ppm=self.ppm_spin.value(),
            bias_tee=self.bias_chk.isChecked(),
            auto_band=self.auto_band_chk.isChecked(),
            squelch_db=self.sq_slider.value(),
            volume=self.vol_slider.value(),
            mute=self.mute_chk.isChecked(),
            nfm_deemph_us=self.nfm_de_box.currentData(),
            wfm_deemph_us=self.wfm_de_box.currentData(),
            ctcss_filter=self.ctcss_chk.isChecked(),
            wf_auto=self.wf_auto.isChecked(),
            wf_min=self.wf_min.value(),
            wf_max=self.wf_max.value(),
            palette=self.pal_box.currentText(),
            averaging=self.avg_box.currentText(),
            preset_tags=self.tags_chk.isChecked(),
            rec_dir=self.rec_dir_edit.text().strip(),
            stt_model=self.stt_box.currentData(),
            stt_on=self.stt_chk.isChecked(),
            wf_bright=self.wf_bright.value(),
            wf_contrast=self.wf_contrast.value(),
            stt_skip_am=self.stt_am_chk.isChecked(),
            carto_key=self.carto_edit.text().strip(),
            aeroapi_key=self.fa_edit.text().strip(),
            aeroapi_cap=self.fa_cap.value(),
            ntfy_topic=self.ntfy_edit.text().strip(),
        )

    def _save_opts(self):
        try:
            self._collect_opts()
            data = {k: v for k, v in self.opts.items() if not k.startswith("_")}
            cfg.save(data)
        except Exception as e:                      # never let saving take the app down
            print("[!] Could not save settings:", e)

    def closeEvent(self, ev):
        self._autosave.stop()
        self._save_opts()
        self.aircraft_tab.stop()
        if self.presets_win is not None:
            self.presets_win.close()
        self.reader.stop()
        self.reader.wait(3000)
        if self.audio_out:
            self.audio_out.close()
        super().closeEvent(ev)


# --------------------------------------------------------------------------
DEFAULTS = {
    "freq_mhz": 851.0, "mode": "NFM", "step_hz": 12500,
    "gain": "auto", "rate_msps": 2.4, "ppm": 0, "bias_tee": False, "device_index": 0,
    "auto_band": True, "squelch_db": 10, "volume": 60, "mute": False,
    "nfm_deemph_us": 750, "wfm_deemph_us": 75, "ctcss_filter": True,
    "wf_auto": True, "wf_min": -90, "wf_max": -40, "palette": "sdr",
    "averaging": "Medium", "rec_dir": "", "preset_tags": True,
}


def resolve_options(args) -> dict:
    opts = dict(DEFAULTS)
    opts.update(cfg.load())
    if opts.get("mode") not in MODES:
        opts["mode"] = "NFM"
    if args.freq is not None:
        opts["freq_mhz"] = args.freq
    if args.gain is not None:
        opts["gain"] = args.gain
    if args.rate is not None:
        opts["rate_msps"] = args.rate
    if args.ppm is not None:
        opts["ppm"] = args.ppm
    if args.device is not None:
        opts["device_index"] = args.device
    return opts


def open_sdr(opts) -> RtlSdr:
    idx = int(opts["device_index"])
    try:
        sdr = RtlSdr(device_index=idx)
    except Exception as e:
        if idx != 0:
            print(f"[!] Device #{idx} not available ({e}), trying #0")
            opts["device_index"] = idx = 0
            return open_sdr(opts)
        print("\n[!] Could not open the RTL-SDR:", e)
        print("    - Is another SDR app (SDR++, SDRTrunk, ...) holding it?")
        print("    - Is the WinUSB driver installed? Zadig -> Options -> List All")
        print("      Devices -> 'Blog V4' / 'Bulk-In, Interface (Interface 0)' -> WinUSB.\n")
        sys.exit(1)
    opts["_opened_device"] = idx

    rate = float(opts["rate_msps"]) * 1e6
    sdr.sample_rate = rate
    sdr.center_freq = float(opts["freq_mhz"]) * 1e6 + min(300e3, rate * 0.2)
    sdr.gain = "auto" if opts["gain"] == "auto" else float(opts["gain"])
    if opts["ppm"]:
        sdr.freq_correction = int(opts["ppm"])
    if opts.get("bias_tee"):
        try:
            sdr.set_bias_tee(True)
        except Exception as e:
            print("[!] Bias tee not available:", e)
    print(f"[ok] RTL-SDR #{idx} open: {sdr.sample_rate / 1e6:.3f} MS/s, "
          f"centre {sdr.center_freq / 1e6:.5f} MHz, gain {sdr.gain}")
    return sdr


def main():
    ap = argparse.ArgumentParser(description="smashDR receiver")
    ap.add_argument("--freq", type=float, help="frequency in MHz")
    ap.add_argument("--rate", type=float, help="sample rate in MS/s")
    ap.add_argument("--gain", help="'auto' or dB")
    ap.add_argument("--ppm", type=int)
    ap.add_argument("--device", type=int)
    args = ap.parse_args()

    opts = resolve_options(args)
    sdr = open_sdr(opts)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(sdr, opts)
    win.show()
    code = app.exec()
    sdr.close()
    sys.exit(code)


if __name__ == "__main__":
    main()

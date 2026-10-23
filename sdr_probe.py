"""
sdr_probe.py  -  Step 1 of the P25 / DMR decoder project.

Goal of this step: prove we get clean IQ off the RTL-SDR Blog V4 and can see
a signal. Nothing is decoded yet.

What it does
  * Opens the V4 through pyrtlsdr (rtl-sdr-blog librtlsdr)
  * Live spectrum + waterfall
  * Click (or drag the yellow marker) to put a 12.5 kHz channel on a signal
  * Shows channel peak above noise floor, ADC clipping %, RMS level
  * Records raw IQ (.cu8 + .json sidecar) so the decoder can be developed
    and tested offline against real captures

The hardware is tuned slightly ABOVE the target frequency so the target
never sits on the dongle's DC spike.

Setup (Windows)
  pip install pyrtlsdr numpy pyqtgraph PyQt6
  Put rtlsdr.dll + libusb-1.0.dll (x64 folder of the rtl-sdr-blog release)
  next to this script, or in a "lib" folder next to it.
  The V4 needs the WinUSB driver (Zadig) - one-time step.

Run
  python sdr_probe.py --freq 851.0125
  python sdr_probe.py --freq 446.0 --gain 30 --rate 1.2
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
VERSION = "0.0.1"

FFT_SIZE = 2048
READ_BYTES = 524288          # 262144 IQ samples per read (~110 ms at 2.4 MS/s)
WF_ROWS = 300
CHAN_HALF_BW = 6.25e3        # 12.5 kHz channel
RATES_MSPS = [2.4, 1.92, 1.2, 0.96]


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


_setup_dll_path()

try:
    from rtlsdr import RtlSdr
except Exception as e:  # missing DLL shows up as ImportError/OSError
    print("\n[!] Could not load librtlsdr:", e)
    print("    Put rtlsdr.dll and libusb-1.0.dll (x64, from the rtl-sdr-blog")
    print("    GitHub release) next to this script or in a 'lib' folder.\n")
    sys.exit(1)

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402
import pyqtgraph as pg  # noqa: E402

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)


# --------------------------------------------------------------------------
# DSP helpers
# --------------------------------------------------------------------------
_WIN = np.hanning(FFT_SIZE).astype(np.float32)
_WIN_NORM = float(_WIN.sum() ** 2)


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


# --------------------------------------------------------------------------
# SDR reader thread
# --------------------------------------------------------------------------
class SdrReader(QtCore.QThread):
    # spectrum_db, center_hz, rate_hz, stats dict
    frame = QtCore.pyqtSignal(object, float, float, object)
    status = QtCore.pyqtSignal(str)

    def __init__(self, sdr: RtlSdr):
        super().__init__()
        self.sdr = sdr
        self._running = True
        self._lock = threading.Lock()
        self._pending = {}
        self._flush = 0
        self._rec = None
        self._rec_bytes = 0
        self._rec_path = None

    def request(self, **kw):
        with self._lock:
            self._pending.update(kw)

    def stop(self):
        self._running = False

    # -- applied inside the thread so librtlsdr is only touched here -------
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

        if retune:
            self._flush = 2  # drop stale buffered samples

        if "rec_start" in p:
            self._open_recording(p["rec_start"])

    def _open_recording(self, meta: dict):
        rec_dir = HERE / "recordings"
        rec_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{meta['target_hz'] / 1e6:.5f}MHz_{int(self.sdr.sample_rate)}sps"
        self._rec_path = rec_dir / f"{name}.cu8"
        meta = dict(meta)
        meta.update(
            format="cu8 interleaved I/Q",
            center_hz=float(self.sdr.center_freq),
            sample_rate=float(self.sdr.sample_rate),
            gain=str(self.sdr.gain),
            started=stamp,
            tool=f"sdr_probe {VERSION}",
        )
        with open(self._rec_path.with_suffix(".json"), "w") as f:
            json.dump(meta, f, indent=2)
        self._rec = open(self._rec_path, "wb")
        self._rec_bytes = 0
        self.status.emit(f"Recording -> {self._rec_path.name}")

    def _close_recording(self):
        if self._rec:
            self._rec.close()
            mb = self._rec_bytes / 1e6
            self.status.emit(f"Saved {self._rec_path.name} ({mb:.1f} MB)")
        self._rec = None

    def run(self):
        while self._running:
            self._apply_pending()
            try:
                raw = self.sdr.read_bytes(READ_BYTES)
            except Exception as e:
                self.status.emit(f"Read error: {e}")
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
            rms_db = 10.0 * np.log10(np.mean(iq.real ** 2 + iq.imag ** 2) + 1e-20)
            spec = power_spectrum_db(iq)

            stats = {"clip": clip, "rms": rms_db,
                     "rec_mb": self._rec_bytes / 1e6 if self._rec else None}
            self.frame.emit(spec, float(self.sdr.center_freq),
                            float(self.sdr.sample_rate), stats)

        self._close_recording()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class ProbeWindow(QtWidgets.QMainWindow):
    def __init__(self, sdr: RtlSdr, args):
        super().__init__()
        self.setWindowTitle(f"SDR Probe {VERSION}")
        self.resize(1200, 800)

        self.target_hz = args.freq * 1e6
        self.center_hz = None
        self.rate_hz = None
        self.freqs_mhz = None
        self.avg = None
        self.wf = np.full((WF_ROWS, FFT_SIZE), -120.0, dtype=np.float32)

        # ---- controls ----
        self.freq_edit = QtWidgets.QLineEdit(f"{args.freq:.5f}")
        self.freq_edit.setFixedWidth(110)
        self.freq_edit.returnPressed.connect(self.tune)
        tune_btn = QtWidgets.QPushButton("Tune")
        tune_btn.clicked.connect(self.tune)

        self.gain_box = QtWidgets.QComboBox()
        self.gain_box.addItem("auto")
        for g in sdr.valid_gains_db:
            self.gain_box.addItem(f"{g:.1f}")
        self.gain_box.setCurrentText("auto" if args.gain == "auto"
                                     else self._closest_gain(sdr, float(args.gain)))
        self.gain_box.currentTextChanged.connect(self.set_gain)

        self.rate_box = QtWidgets.QComboBox()
        for r in RATES_MSPS:
            self.rate_box.addItem(f"{r} MS/s", r * 1e6)
        self.rate_box.setCurrentIndex(
            min(range(len(RATES_MSPS)), key=lambda i: abs(RATES_MSPS[i] - args.rate)))
        self.rate_box.currentIndexChanged.connect(self.set_rate)

        self.ppm_spin = QtWidgets.QSpinBox()
        self.ppm_spin.setRange(-100, 100)
        self.ppm_spin.setValue(args.ppm)
        self.ppm_spin.valueChanged.connect(lambda v: self.reader.request(ppm=v))

        self.rec_btn = QtWidgets.QPushButton("Record IQ")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self.toggle_record)

        bar = QtWidgets.QHBoxLayout()
        for w in (QtWidgets.QLabel("Freq MHz"), self.freq_edit, tune_btn,
                  QtWidgets.QLabel("Gain"), self.gain_box,
                  QtWidgets.QLabel("Rate"), self.rate_box,
                  QtWidgets.QLabel("PPM"), self.ppm_spin, self.rec_btn):
            bar.addWidget(w)
        bar.addStretch(1)

        # ---- spectrum ----
        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setLabel("bottom", "MHz")
        self.spec_plot.setLabel("left", "dBFS")
        self.spec_plot.setYRange(-110, -10)
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#4fc3f7", width=1))
        self.chan_region = pg.LinearRegionItem(movable=False,
                                               brush=pg.mkBrush(255, 213, 79, 40))
        self.spec_plot.addItem(self.chan_region)
        self.marker = pg.InfiniteLine(angle=90, movable=True,
                                      pen=pg.mkPen("#ffd54f", width=1))
        self.marker.sigPositionChanged.connect(self._marker_moved)
        self.spec_plot.addItem(self.marker)

        # ---- waterfall ----
        self.wf_plot = pg.PlotWidget()
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_plot.hideAxis("left")
        self.wf_img = pg.ImageItem()
        self.wf_img.setColorMap(pg.colormap.get("inferno"))
        self.wf_plot.addItem(self.wf_img)
        self.wf_marker = pg.InfiniteLine(angle=90, movable=False,
                                         pen=pg.mkPen("#ffd54f", width=1))
        self.wf_plot.addItem(self.wf_marker)

        for p in (self.spec_plot, self.wf_plot):
            p.scene().sigMouseClicked.connect(
                lambda ev, plot=p: self._plot_clicked(ev, plot))

        # ---- readout ----
        self.info = QtWidgets.QLabel("Starting...")
        self.info.setFont(QtGui.QFont("Consolas", 11))
        self.status_lbl = QtWidgets.QLabel("")

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(self.wf_plot)
        split.setSizes([350, 450])

        lay = QtWidgets.QVBoxLayout()
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.status_lbl)
        central = QtWidgets.QWidget()
        central.setLayout(lay)
        self.setCentralWidget(central)

        # ---- reader ----
        self.reader = SdrReader(sdr)
        self.reader.frame.connect(self.on_frame)
        self.reader.status.connect(self.status_lbl.setText)
        self._set_marker(self.target_hz)
        self.reader.start()

    # ---- helpers ----
    @staticmethod
    def _closest_gain(sdr, g):
        return f"{min(sdr.valid_gains_db, key=lambda x: abs(x - g)):.1f}"

    def _offset_hz(self):
        rate = self.rate_box.currentData()
        return min(300e3, rate * 0.2)

    def _set_marker(self, hz):
        self.target_hz = hz
        mhz = hz / 1e6
        self.marker.blockSignals(True)
        self.marker.setValue(mhz)
        self.marker.blockSignals(False)
        self.wf_marker.setValue(mhz)
        self.chan_region.setRegion((mhz - CHAN_HALF_BW / 1e6, mhz + CHAN_HALF_BW / 1e6))
        self.freq_edit.setText(f"{mhz:.5f}")

    def _marker_moved(self):
        self._set_marker(self.marker.value() * 1e6)

    def _plot_clicked(self, ev, plot):
        vb = plot.plotItem.vb
        if vb.sceneBoundingRect().contains(ev.scenePos()):
            self._set_marker(vb.mapSceneToView(ev.scenePos()).x() * 1e6)

    # ---- actions ----
    def tune(self):
        try:
            hz = float(self.freq_edit.text()) * 1e6
        except ValueError:
            return
        self._set_marker(hz)
        self.reader.request(center=hz + self._offset_hz())
        self.rec_btn.setChecked(False)

    def set_gain(self, text):
        self.reader.request(gain="auto" if text == "auto" else float(text))
        self.rec_btn.setChecked(False)

    def set_rate(self, _idx):
        self.reader.request(rate=self.rate_box.currentData(),
                            center=self.target_hz + self._offset_hz())
        self.rec_btn.setChecked(False)

    def toggle_record(self, on):
        if on:
            self.reader.request(rec_start={"target_hz": self.target_hz,
                                           "ppm": self.ppm_spin.value()})
            self.rec_btn.setText("Stop rec")
        else:
            self.reader.request(rec_stop=True)
            self.rec_btn.setText("Record IQ")

    # ---- frame handling ----
    def on_frame(self, spec, center, rate, stats):
        if center != self.center_hz or rate != self.rate_hz:
            self.center_hz, self.rate_hz = center, rate
            bins = (np.arange(FFT_SIZE) - FFT_SIZE / 2) * (rate / FFT_SIZE)
            self.freqs_mhz = (center + bins) / 1e6
            self.avg = spec.copy()
            self.wf[:] = np.median(spec)
            f0 = self.freqs_mhz[0]
            span = rate / 1e6
            self.wf_img.setRect(QtCore.QRectF(f0, 0, span, WF_ROWS))
            self.spec_plot.setXRange(f0, f0 + span, padding=0)

        self.avg = 0.5 * self.avg + 0.5 * spec
        self.spec_curve.setData(self.freqs_mhz, self.avg)

        self.wf[:-1] = self.wf[1:]
        self.wf[-1] = spec
        floor = float(np.median(spec))
        self.wf_img.setImage(self.wf, autoLevels=False, levels=(floor - 5, floor + 45))

        # channel readout
        mask = np.abs(self.freqs_mhz * 1e6 - self.target_hz) <= CHAN_HALF_BW
        if mask.any():
            peak = float(self.avg[mask].max())
            chan_pwr = 10 * np.log10(np.sum(10 ** (self.avg[mask] / 10)))
            chan_txt = (f"chan pwr {chan_pwr:6.1f} dBFS   peak {peak:6.1f}   "
                        f"above floor {peak - floor:5.1f} dB")
        else:
            chan_txt = "marker outside tuned span - press Tune"

        clip_txt = f"clip {stats['clip']:.3f}%"
        if stats["clip"] > 0.1:
            clip_txt += "  << OVERLOAD, lower gain"
        rec_txt = f"   REC {stats['rec_mb']:.0f} MB" if stats["rec_mb"] is not None else ""
        self.info.setText(f"{self.target_hz / 1e6:.5f} MHz   {chan_txt}   "
                          f"floor {floor:6.1f}   rms {stats['rms']:5.1f} dBFS   "
                          f"{clip_txt}{rec_txt}")

    def closeEvent(self, ev):
        self.reader.stop()
        self.reader.wait(2000)
        super().closeEvent(ev)


# --------------------------------------------------------------------------
def open_sdr(args) -> RtlSdr:
    try:
        sdr = RtlSdr(device_index=args.device)
    except Exception as e:
        print("\n[!] Could not open the RTL-SDR:", e)
        print("    - Is another SDR app (SDR++, SDRTrunk, ...) holding it?")
        print("    - Is the WinUSB driver installed? Zadig -> Options -> List All")
        print("      Devices -> 'Bulk-In, Interface (Interface 0)' -> WinUSB.\n")
        sys.exit(1)

    rate = args.rate * 1e6
    sdr.sample_rate = rate
    sdr.center_freq = args.freq * 1e6 + min(300e3, rate * 0.2)
    sdr.gain = "auto" if args.gain == "auto" else float(args.gain)
    if args.ppm:
        sdr.freq_correction = args.ppm
    print(f"[ok] RTL-SDR open: {sdr.sample_rate / 1e6:.3f} MS/s, "
          f"centre {sdr.center_freq / 1e6:.5f} MHz, gain {sdr.gain}")
    return sdr


def main():
    ap = argparse.ArgumentParser(description="RTL-SDR probe: spectrum, waterfall, IQ recording")
    ap.add_argument("--freq", type=float, default=851.0, help="target frequency in MHz")
    ap.add_argument("--rate", type=float, default=2.4, help="sample rate in MS/s")
    ap.add_argument("--gain", default="auto", help="'auto' or dB")
    ap.add_argument("--ppm", type=int, default=0)
    ap.add_argument("--device", type=int, default=0)
    args = ap.parse_args()

    sdr = open_sdr(args)
    app = QtWidgets.QApplication(sys.argv)
    win = ProbeWindow(sdr, args)
    win.show()
    code = app.exec()
    sdr.close()
    sys.exit(code)


if __name__ == "__main__":
    main()

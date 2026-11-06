"""eq_panel.py - small floating equalizer window (opened from the EQ button next to Vol)."""

from PyQt6 import QtCore, QtWidgets

from smashdr.core.eq import BANDS, MAX_DB, PRESETS


def _hz(f):
    return f"{f // 1000}k" if f >= 1000 else str(f)


class EqPanel(QtWidgets.QDialog):
    changed = QtCore.pyqtSignal(list, bool)          # gains (dB), enabled

    def __init__(self, gains, enabled, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio equalizer")
        self.setWindowFlag(QtCore.Qt.WindowType.Tool)
        self.on_chk = QtWidgets.QCheckBox("Equalizer on")
        self.on_chk.setChecked(enabled)
        self.preset = QtWidgets.QComboBox()
        self.preset.addItems(list(PRESETS) + ["Custom"])
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.on_chk)
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("Preset"))
        top.addWidget(self.preset)

        grid = QtWidgets.QGridLayout()
        self.sliders, self.vals = [], []
        for i, f in enumerate(BANDS):
            s = QtWidgets.QSlider(QtCore.Qt.Orientation.Vertical)
            s.setRange(int(-MAX_DB), int(MAX_DB))
            s.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBothSides)
            s.setTickInterval(3)
            s.setMinimumHeight(160)
            s.setValue(int(round(gains[i])) if i < len(gains) else 0)
            s.setToolTip(f"{_hz(f)} Hz: drag, or double-click the number for 0 dB")
            v = QtWidgets.QPushButton("")
            v.setFlat(True)
            v.clicked.connect(lambda _c=False, sl=s: sl.setValue(0))
            v.setToolTip("Reset to 0 dB")
            grid.addWidget(v, 0, i, QtCore.Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(s, 1, i, QtCore.Qt.AlignmentFlag.AlignHCenter)
            lab = QtWidgets.QLabel(f"{_hz(f)} Hz")
            lab.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(lab, 2, i)
            s.valueChanged.connect(self._slid)
            self.sliders.append(s)
            self.vals.append(v)
        hint = QtWidgets.QLabel("Radio voice lives between about 300 Hz and 3 kHz: cutting 100-250 Hz "
                                "removes hum/rumble, lifting 1-2 kHz helps words, cutting 4 kHz and up "
                                "removes hiss.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(grid)
        lay.addWidget(hint)
        self.on_chk.toggled.connect(lambda _on: self._emit())
        self.preset.activated.connect(self._preset)
        self._labels()
        self._match_preset()

    def gains(self):
        return [float(s.value()) for s in self.sliders]

    def _labels(self):
        for s, v in zip(self.sliders, self.vals):
            v.setText(f"{s.value():+d}" if s.value() else "0")

    def _match_preset(self):
        g = tuple(int(x) for x in self.gains())
        name = next((n for n, p in PRESETS.items() if tuple(p) == g), "Custom")
        self.preset.blockSignals(True)
        self.preset.setCurrentText(name)
        self.preset.blockSignals(False)

    def _preset(self, _i):
        p = PRESETS.get(self.preset.currentText())
        if p is None:
            return
        for s, g in zip(self.sliders, p):
            s.blockSignals(True)
            s.setValue(int(g))
            s.blockSignals(False)
        if not self.on_chk.isChecked() and self.preset.currentText() != "Flat":
            self.on_chk.setChecked(True)
        self._labels()
        self._emit()

    def _slid(self, _v):
        self._labels()
        self._match_preset()
        self._emit()

    def _emit(self):
        self.changed.emit(self.gains(), self.on_chk.isChecked())

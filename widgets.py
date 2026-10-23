"""
widgets.py - custom Qt widgets for smashDR.

    FreqDisplay   VFO-style frequency readout: wheel over a digit tunes that
                  digit, Up/Down arrows tune by the step, double-click to type
    LevelMeter    signal-above-floor bar with a squelch threshold marker
"""

from PyQt6 import QtCore, QtGui, QtWidgets

F_MIN_HZ = 24_000_000
F_MAX_HZ = 1_766_000_000
NDIGITS = 10                           # up to 9.999 999 999 GHz


def format_digits(hz: int) -> str:
    """1234567890 -> '1.234.567.890' (always 10 digits, grouped by 3)."""
    s = f"{int(hz):0{NDIGITS}d}"
    return f"{s[0]}.{s[1:4]}.{s[4:7]}.{s[7:10]}"


def char_power(index: int):
    """Power of ten for character `index` of format_digits(), None for a dot."""
    text_len = NDIGITS + 3
    if index < 0 or index >= text_len or index in (1, 5, 9):
        return None
    digits_before = index - sum(1 for d in (1, 5, 9) if d < index)
    return NDIGITS - 1 - digits_before


class FreqDisplay(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(int)        # emitted on user changes only, Hz

    def __init__(self, hz: int = 100_000_000, parent=None):
        super().__init__(parent)
        self._hz = int(hz)
        self._hover = None
        self.step_hz = 12_500
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._font = QtGui.QFont("Consolas", 26)
        self._font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        self.setToolTip("Wheel over a digit to tune it · Up/Down = step · double-click to type")

    # ---- value ----
    def value(self) -> int:
        return self._hz

    def setValue(self, hz: int):
        self._hz = max(F_MIN_HZ, min(F_MAX_HZ, int(round(hz))))
        self.update()

    def _user_set(self, hz: int):
        old = self._hz
        self.setValue(hz)
        if self._hz != old:
            self.changed.emit(self._hz)

    # ---- geometry ----
    def _char_w(self) -> int:
        return QtGui.QFontMetrics(self._font).horizontalAdvance("0")

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self._font)
        return QtCore.QSize(self._char_w() * (NDIGITS + 3) + 12, fm.height() + 8)

    def minimumSizeHint(self):
        return self.sizeHint()

    def _index_at(self, x: float):
        i = int((x - 6) // self._char_w())
        return i if 0 <= i < NDIGITS + 3 else None

    # ---- painting ----
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setFont(self._font)
        pal = self.palette()
        fg = pal.color(QtGui.QPalette.ColorRole.WindowText)
        dim = QtGui.QColor(fg)
        dim.setAlpha(70)
        hl = pal.color(QtGui.QPalette.ColorRole.Highlight)
        cw = self._char_w()
        fm = QtGui.QFontMetrics(self._font)
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        text = format_digits(self._hz)
        leading = True
        for i, ch in enumerate(text):
            if ch.isdigit() and ch != "0":
                leading = False
            x = 6 + i * cw
            if i == self._hover and char_power(i) is not None:
                p.fillRect(x, 2, cw, self.height() - 4, QtGui.QColor(hl.red(), hl.green(), hl.blue(), 50))
            p.setPen(dim if (leading or ch == ".") else fg)
            p.drawText(x, y, ch)
        if self.hasFocus():
            p.setPen(QtGui.QPen(hl, 1))
            p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        p.end()

    # ---- interaction ----
    def mouseMoveEvent(self, ev):
        idx = self._index_at(ev.position().x())
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, _ev):
        self._hover = None
        self.update()

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y() // 120 or (1 if ev.angleDelta().y() > 0 else -1)
        idx = self._index_at(ev.position().x())
        power = char_power(idx) if idx is not None else None
        delta = 10 ** power if power is not None else self.step_hz
        self._user_set(self._hz + steps * delta)
        ev.accept()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Right):
            self._user_set(self._hz + self.step_hz)
        elif k in (QtCore.Qt.Key.Key_Down, QtCore.Qt.Key.Key_Left):
            self._user_set(self._hz - self.step_hz)
        elif k in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) or ev.text().isdigit():
            self.prompt(ev.text() if ev.text().isdigit() else "")
        else:
            super().keyPressEvent(ev)

    def mouseDoubleClickEvent(self, _ev):
        self.prompt()

    def prompt(self, first_char: str = ""):
        dlg = QtWidgets.QInputDialog(self)
        dlg.setWindowTitle("Frequency")
        dlg.setLabelText("MHz")
        dlg.setTextValue(first_char or f"{self._hz / 1e6:.6f}".rstrip("0").rstrip("."))
        if dlg.exec():
            try:
                self._user_set(float(dlg.textValue().replace(",", ".")) * 1e6)
            except ValueError:
                pass


class LevelMeter(QtWidgets.QWidget):
    """Horizontal bar: signal dB above noise floor, with squelch marker."""

    def __init__(self, max_db: float = 60.0, parent=None):
        super().__init__(parent)
        self.max_db = max_db
        self.level = 0.0
        self.squelch = 0.0
        self.open = False
        self.setMinimumSize(160, 22)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)

    def set_values(self, level_db: float, squelch_db: float, sq_open: bool):
        self.level = max(0.0, level_db)
        self.squelch = squelch_db
        self.open = sq_open
        self.update()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        bar_w = w - 70
        pal = self.palette()
        p.fillRect(0, 6, bar_w, h - 12, pal.color(QtGui.QPalette.ColorRole.AlternateBase))
        frac = min(1.0, self.level / self.max_db)
        col = QtGui.QColor(60, 180, 90) if self.open else QtGui.QColor(110, 140, 120)
        p.fillRect(0, 6, int(bar_w * frac), h - 12, col)
        if self.squelch > 0:
            sx = int(bar_w * min(1.0, self.squelch / self.max_db))
            p.fillRect(sx - 1, 2, 2, h - 4, QtGui.QColor(230, 160, 40))
        p.setPen(pal.color(QtGui.QPalette.ColorRole.WindowText))
        p.drawText(QtCore.QRect(bar_w + 6, 0, 64, h),
                   QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft,
                   f"{self.level:4.0f} dB")
        p.end()

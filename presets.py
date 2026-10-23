"""
presets.py - preset store and the Presets window (meant for a second screen).

Store
  presets.json next to the app (local, not in git). If it doesn't exist yet,
  presets_default.json (in git) seeds it.
  Each preset: {"name", "freq_hz", "mode", "group", "step_hz"?, "squelch_db"?, "note"?}

Window
  Groups of small preset buttons. Click = tune the receiver, right-click =
  edit / duplicate / delete. "Add current" saves what the receiver is on.
  The preset matching the receiver's frequency is highlighted.
"""

import json
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

HERE = Path(__file__).resolve().parent
STORE = HERE / "presets.json"
DEFAULT_STORE = HERE / "presets_default.json"
MODE_NAMES = ["NFM", "AM", "WFM"]
STEP_CHOICES = [0, 1000, 2500, 5000, 6250, 7500, 8330, 10000, 12500, 15000,
                20000, 25000, 50000, 100000, 200000]


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
def _clean(p: dict):
    try:
        out = {
            "name": str(p.get("name", "")).strip() or "Preset",
            "freq_hz": int(round(float(p["freq_hz"]))),
            "mode": p.get("mode") if p.get("mode") in MODE_NAMES else "NFM",
            "group": str(p.get("group", "")).strip() or "General",
        }
    except (KeyError, TypeError, ValueError):
        return None
    for k in ("step_hz", "squelch_db"):
        if p.get(k) not in (None, "", 0):
            try:
                out[k] = int(p[k])
            except (TypeError, ValueError):
                pass
    if p.get("note"):
        out["note"] = str(p["note"])
    return out


def load_presets() -> list:
    for path in (STORE, DEFAULT_STORE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("presets", data) if isinstance(data, dict) else data
        return [c for c in (_clean(p) for p in items if isinstance(p, dict)) if c]
    return []


def save_presets(presets: list):
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"presets": presets}, indent=2), encoding="utf-8")
    tmp.replace(STORE)


def groups_in_order(presets: list) -> list:
    seen = []
    for p in presets:
        if p["group"] not in seen:
            seen.append(p["group"])
    return seen


def preset_subtitle(p: dict) -> str:
    return f"{p['freq_hz'] / 1e6:.4f} {p['mode']}"


# --------------------------------------------------------------------------
# Flow layout (wraps buttons like text)
# --------------------------------------------------------------------------
class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, dry_run=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QtCore.QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _arrange(self, rect, dry_run):
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            sz = it.sizeHint()
            if x + sz.width() > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + sp
                line_h = 0
            if not dry_run:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), sz))
            x += sz.width() + sp
            line_h = max(line_h, sz.height())
        return y + line_h - rect.y()


# --------------------------------------------------------------------------
# Edit dialog
# --------------------------------------------------------------------------
class PresetDialog(QtWidgets.QDialog):
    def __init__(self, preset: dict, groups: list, parent=None, title="Edit preset"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.name = QtWidgets.QLineEdit(preset.get("name", ""))
        self.freq = QtWidgets.QDoubleSpinBox()
        self.freq.setRange(24.0, 1766.0)
        self.freq.setDecimals(5)
        self.freq.setSuffix(" MHz")
        self.freq.setValue(preset.get("freq_hz", 100_000_000) / 1e6)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(MODE_NAMES)
        self.mode.setCurrentText(preset.get("mode", "NFM"))
        self.step = QtWidgets.QComboBox()
        for s in STEP_CHOICES:
            self.step.addItem("keep current" if s == 0 else f"{s / 1000:g} kHz", s)
        i = self.step.findData(int(preset.get("step_hz", 0) or 0))
        self.step.setCurrentIndex(max(0, i))
        self.squelch = QtWidgets.QSpinBox()
        self.squelch.setRange(-1, 60)
        self.squelch.setSpecialValueText("keep current")
        self.squelch.setSuffix(" dB")
        self.squelch.setValue(int(preset.get("squelch_db", -1)))
        self.group = QtWidgets.QComboBox()
        self.group.setEditable(True)
        self.group.addItems(groups)
        self.group.setCurrentText(preset.get("group", groups[0] if groups else "General"))
        self.note = QtWidgets.QLineEdit(preset.get("note", ""))

        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Frequency", self.freq)
        form.addRow("Mode", self.mode)
        form.addRow("Step", self.step)
        form.addRow("Squelch", self.squelch)
        form.addRow("Group", self.group)
        form.addRow("Note", self.note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def result_preset(self) -> dict:
        p = {"name": self.name.text().strip() or "Preset",
             "freq_hz": int(round(self.freq.value() * 1e6)),
             "mode": self.mode.currentText(),
             "group": self.group.currentText().strip() or "General"}
        if self.step.currentData():
            p["step_hz"] = int(self.step.currentData())
        if self.squelch.value() >= 0:
            p["squelch_db"] = self.squelch.value()
        if self.note.text().strip():
            p["note"] = self.note.text().strip()
        return p


# --------------------------------------------------------------------------
# Presets window
# --------------------------------------------------------------------------
class PresetButton(QtWidgets.QPushButton):
    def __init__(self, preset: dict, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setCheckable(True)
        self.setFixedSize(132, 46)
        self.setText(f"{preset['name']}\n{preset_subtitle(preset)}")
        tip = f"{preset['name']}  ·  {preset_subtitle(preset)}"
        if preset.get("note"):
            tip += f"\n{preset['note']}"
        self.setToolTip(tip)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)


class PresetsWindow(QtWidgets.QWidget):
    """Top-level window of preset buttons. Emits tune_requested(preset dict)."""

    tune_requested = QtCore.pyqtSignal(dict)

    STYLE = """
        QPushButton { text-align: left; padding: 3px 7px; font-size: 9pt; }
        QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }
    """

    def __init__(self, get_current, parent=None):
        """get_current() -> dict with freq_hz, mode, step_hz, squelch_db of the receiver."""
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        self.setWindowTitle("smashDR presets")
        self.resize(560, 640)
        self.get_current = get_current
        self.presets = load_presets()
        self._buttons = []
        self._active = None          # (freq_hz, mode)

        add_btn = QtWidgets.QPushButton("Add current")
        add_btn.clicked.connect(self.add_current)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter by name or frequency")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.count_lbl = QtWidgets.QLabel()

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(add_btn)
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.count_lbl)

        self.body = QtWidgets.QWidget()
        self.body_lay = QtWidgets.QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(4, 4, 4, 4)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.body)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(scroll, 1)
        self.setStyleSheet(self.STYLE)
        self.rebuild()

    # ---- building ----
    def rebuild(self):
        while self.body_lay.count():
            it = self.body_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._buttons = []
        for group in groups_in_order(self.presets):
            hdr = QtWidgets.QLabel(group)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            holder = QtWidgets.QWidget()
            flow = FlowLayout(holder)
            for p in self.presets:
                if p["group"] != group:
                    continue
                b = PresetButton(p)
                b.clicked.connect(lambda _c, pp=p: self._clicked(pp))
                b.customContextMenuRequested.connect(
                    lambda pos, btn=b: self._context_menu(btn, pos))
                flow.addWidget(b)
                self._buttons.append(b)
            self.body_lay.addWidget(hdr)
            self.body_lay.addWidget(holder)
        if not self.presets:
            empty = QtWidgets.QLabel("No presets yet. Tune somewhere and click Add current.")
            empty.setWordWrap(True)
            self.body_lay.addWidget(empty)
        self.body_lay.addStretch(1)
        self._apply_filter(self.filter.text())
        self._refresh_active()

    def _apply_filter(self, text):
        t = text.strip().lower()
        shown = 0
        for b in self._buttons:
            p = b.preset
            hay = f"{p['name']} {p['freq_hz'] / 1e6:.4f} {p['mode']} {p['group']} {p.get('note', '')}".lower()
            vis = not t or t in hay
            b.setVisible(vis)
            shown += vis
        self.count_lbl.setText(f"{shown} / {len(self._buttons)}")

    # ---- active highlight ----
    def set_active(self, freq_hz: int, mode: str):
        self._active = (int(freq_hz), mode)
        self._refresh_active()

    def _refresh_active(self):
        for b in self._buttons:
            b.setChecked(self._active is not None and b.preset["freq_hz"] == self._active[0])

    # ---- actions ----
    def _clicked(self, p):
        self.tune_requested.emit(dict(p))
        self._refresh_active()

    def _save(self):
        try:
            save_presets(self.presets)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Presets", f"Couldn't save presets: {e}")

    def add_current(self):
        cur = self.get_current()
        groups = groups_in_order(self.presets) or ["General"]
        dlg = PresetDialog({"name": f"{cur['freq_hz'] / 1e6:.4f}", **cur}, groups, self,
                           title="Add preset")
        if dlg.exec():
            self.presets.append(dlg.result_preset())
            self._save()
            self.rebuild()

    def _context_menu(self, btn, pos):
        p = btn.preset
        menu = QtWidgets.QMenu(self)
        a_edit = menu.addAction("Edit…")
        a_dup = menu.addAction("Duplicate")
        a_upd = menu.addAction("Update to current frequency")
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen is None:
            return
        i = self.presets.index(p)
        if chosen == a_edit:
            dlg = PresetDialog(p, groups_in_order(self.presets), self)
            if dlg.exec():
                self.presets[i] = dlg.result_preset()
        elif chosen == a_dup:
            self.presets.insert(i + 1, dict(p, name=p["name"] + " copy"))
        elif chosen == a_upd:
            cur = self.get_current()
            self.presets[i] = dict(p, freq_hz=cur["freq_hz"], mode=cur["mode"])
        elif chosen == a_del:
            if QtWidgets.QMessageBox.question(self, "Delete preset",
                                              f"Delete “{p['name']}”?") != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.presets.pop(i)
        self._save()
        self.rebuild()

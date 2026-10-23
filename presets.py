"""
presets.py - preset store and the Presets window (meant for a second screen).

Store
  presets.json next to the app (local, not in git). If it doesn't exist yet,
  presets_default.json (in git) seeds it.
  Each preset: {"name", "freq_hz", "mode", "group", "step_hz"?, "squelch_db"?, "note"?}

Window
  Collapsible groups of small preset buttons. Click = tune the receiver,
  right-click = edit / duplicate / delete. "Add current" saves what the
  receiver is on, "Import" takes rows pasted from RadioReference.
  The preset matching the receiver's frequency is highlighted.

New entries added to presets_default.json later still show up for existing
users: presets.json remembers which defaults it has already seen, so a
default you deleted stays deleted.
"""

import csv
import io
import json
import re
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

HERE = Path(__file__).resolve().parent
STORE = HERE / "presets.json"
DEFAULT_STORE = HERE / "presets_default.json"
MODE_NAMES = ["NFM", "AM", "WFM"]
# preset button sizes: (width, height, font pt)
BUTTON_SIZES = {"Small": (132, 46, 9), "Medium": (170, 58, 10), "Large": (210, 70, 12)}
DEFAULT_BUTTON_SIZE = "Large"
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


def preset_key(p: dict) -> str:
    return f"{p['freq_hz']}|{p['name']}"


def _read(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, list):
        data = {"presets": data}
    if not isinstance(data, dict):
        return None
    items = [c for c in (_clean(p) for p in data.get("presets", []) if isinstance(p, dict)) if c]
    size = data.get("button_size")
    return {"presets": items,
            "collapsed": [str(g) for g in data.get("collapsed", [])],
            "defaults_seen": [str(k) for k in data.get("defaults_seen", [])],
            "button_size": size if size in BUTTON_SIZES else DEFAULT_BUTTON_SIZE}


def load_store() -> dict:
    """User presets merged with any defaults they haven't seen yet."""
    defaults = _read(DEFAULT_STORE) or {"presets": [], "collapsed": [], "defaults_seen": [],
                                        "button_size": DEFAULT_BUTTON_SIZE}
    user = _read(STORE)
    if user is None:
        return {"presets": list(defaults["presets"]),
                "collapsed": list(defaults["collapsed"]),
                "defaults_seen": [preset_key(p) for p in defaults["presets"]],
                "button_size": DEFAULT_BUTTON_SIZE}
    seen = set(user["defaults_seen"]) or {preset_key(p) for p in user["presets"]}
    have = {preset_key(p) for p in user["presets"]}
    new_groups = []
    for d in defaults["presets"]:
        k = preset_key(d)
        if k not in seen and k not in have:
            user["presets"].append(d)
            if d["group"] not in new_groups:
                new_groups.append(d["group"])
        seen.add(k)
    # groups that arrived with new defaults inherit the default collapsed state
    user["collapsed"] += [g for g in new_groups
                          if g in defaults["collapsed"] and g not in user["collapsed"]]
    user["defaults_seen"] = sorted(seen)
    return user


def save_store(store: dict):
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(STORE)


def load_presets() -> list:
    return load_store()["presets"]


# --------------------------------------------------------------------------
# RadioReference paste / CSV import
# --------------------------------------------------------------------------
DIGITAL_MARKERS = ("P25", "DMR", "NXDN", "DSTAR", "D-STAR", "TDMA", "YSF", "FUSION",
                   "DPMR", "TETRA", "PROVOICE", "EDACS", "MOTOTRBO")
MODE_MAP = {"FM": "NFM", "FMN": "NFM", "NFM": "NFM", "AM": "AM", "FMW": "WFM", "WFM": "WFM"}
_TONE_RE = re.compile(r"^(\d{2,3}(\.\d)?\s*PL|\d{3}\s*N?DPL|CSQ|NAC\s*\w+|CC\s*\d+|RAN\s*\d+)$", re.I)


def _freq_mhz(cell: str):
    try:
        f = float(cell.strip().replace(",", "."))
    except ValueError:
        return None
    return f if 24.0 <= f <= 1766.0 else None


def _default_step(freq_hz: int, mode: str) -> int:
    if mode == "WFM":
        return 200000
    if mode == "AM" and 108e6 <= freq_hz <= 400e6:
        return 25000
    for step in (12500, 6250, 5000, 2500):
        if freq_hz % step == 0:
            return step
    return 1000


def parse_radioreference(text: str, group: str = "Imported", group_by_tag: bool = False):
    """
    Parse rows copied from a RadioReference table (tab separated) or an RR CSV
    export. Returns (presets, skipped_digital) where skipped_digital is a list
    of 'freq alpha (mode)' strings.
    Column order without a header row: Frequency, License, Type, Tone,
    Alpha Tag, Description, Mode, Tag.
    """
    text = text.strip()
    if not text:
        return [], []
    if "\t" in text:
        rows = [line.split("\t") for line in text.splitlines()]
    else:
        rows = list(csv.reader(io.StringIO(text)))
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]

    cols = None
    out, skipped = [], []
    for r in rows:
        low = [c.lower() for c in r]
        if "frequency" in low or "freq" in low:          # header row
            cols = {name: low.index(name) for name in
                    ("frequency", "freq", "tone", "alpha tag", "description", "mode", "tag")
                    if name in low}
            continue

        def col(name, fallback_idx=None):
            i = cols.get(name) if cols else fallback_idx
            return r[i] if i is not None and i < len(r) else ""

        if cols:
            f = _freq_mhz(col("frequency") or col("freq"))
            tone, alpha, desc = col("tone"), col("alpha tag"), col("description")
            rr_mode, tag = col("mode"), col("tag")
        else:
            f = _freq_mhz(r[0]) if r else None
            if f is None:
                f = next((x for x in (_freq_mhz(c) for c in r) if x), None)
            if len(r) >= 8:
                tone, alpha, desc, rr_mode, tag = r[3], r[4], r[5], r[6], r[7]
            else:
                tone = next((c for c in r if _TONE_RE.match(c)), "")
                rr_mode = next((c for c in r if c.upper() in MODE_MAP or
                                any(m in c.upper() for m in DIGITAL_MARKERS)), "")
                words = [c for c in r if c and _freq_mhz(c) is None and c not in (tone, rr_mode)]
                alpha = words[0] if words else ""
                desc = max(words, key=len) if words else ""
                tag = ""
        if f is None:
            continue

        mode_u = rr_mode.upper().replace(" ", "")
        if any(m in mode_u for m in DIGITAL_MARKERS):
            skipped.append(f"{f:.4f} {alpha or desc} ({rr_mode})")
            continue
        mode = MODE_MAP.get(mode_u, "NFM")
        freq_hz = int(round(f * 1e6))
        name = (alpha or desc or f"{f:.4f}")[:18]
        note = " · ".join(x for x in (desc, tone, tag) if x)
        p = {"name": name, "freq_hz": freq_hz, "mode": mode,
             "group": (tag if group_by_tag and tag else group) or "Imported",
             "step_hz": _default_step(freq_hz, mode)}
        if note:
            p["note"] = note
        out.append(p)
    return out, skipped


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


class ImportDialog(QtWidgets.QDialog):
    """Paste rows from a RadioReference table (or an RR CSV) and add them as presets."""

    def __init__(self, groups: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from RadioReference")
        self.resize(720, 480)
        help_lbl = QtWidgets.QLabel(
            "On a RadioReference page, select the table rows you want (or the whole "
            "table including its header), copy, and paste here. A CSV export works too. "
            "Digital channels (P25, DMR, ...) are skipped until smashDR can decode them.")
        help_lbl.setWordWrap(True)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setPlaceholderText("483.750\t\tRM\t186.2 PL\tLAC U-1\tCountywide Dispatch\tFMN\tFire Dispatch")
        self.group = QtWidgets.QComboBox()
        self.group.setEditable(True)
        self.group.addItems(groups)
        self.group.setCurrentText("Imported")
        self.by_tag = QtWidgets.QCheckBox("Use RadioReference's Tag column as the group")
        self.preview = QtWidgets.QLabel("")
        self.preview.setWordWrap(True)
        self.text.textChanged.connect(self._update_preview)
        self.group.currentTextChanged.connect(self._update_preview)
        self.by_tag.toggled.connect(self._update_preview)

        form = QtWidgets.QFormLayout()
        form.addRow("Group", self.group)
        form.addRow("", self.by_tag)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Add presets")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(help_lbl)
        lay.addWidget(self.text, 1)
        lay.addLayout(form)
        lay.addWidget(self.preview)
        lay.addWidget(self.buttons)
        self._update_preview()

    def parsed(self):
        return parse_radioreference(self.text.toPlainText(),
                                    self.group.currentText().strip() or "Imported",
                                    self.by_tag.isChecked())

    def _update_preview(self, *_):
        ps, skipped = self.parsed()
        msg = f"{len(ps)} channel(s) ready to add"
        if ps:
            msg += ": " + ", ".join(f"{p['name']} {p['freq_hz'] / 1e6:.4f}" for p in ps[:4])
            msg += " ..." if len(ps) > 4 else ""
        if skipped:
            msg += f"\nSkipping {len(skipped)} digital: " + ", ".join(skipped[:3])
            msg += " ..." if len(skipped) > 3 else ""
        self.preview.setText(msg)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(bool(ps))


# --------------------------------------------------------------------------
# Presets window
# --------------------------------------------------------------------------
class PresetButton(QtWidgets.QPushButton):
    def __init__(self, preset: dict, size: str = DEFAULT_BUTTON_SIZE, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setCheckable(True)
        w, h, pt = BUTTON_SIZES.get(size, BUTTON_SIZES[DEFAULT_BUTTON_SIZE])
        self.setFixedSize(w, h)
        f = self.font()
        f.setPointSize(pt)
        self.setFont(f)
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
        QPushButton { text-align: left; padding: 3px 8px; }
        QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }
        QToolButton#groupHeader { border: none; text-align: left; padding: 4px 2px; }
    """

    def __init__(self, get_current, parent=None):
        """get_current() -> dict with freq_hz, mode, step_hz, squelch_db of the receiver."""
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        self.setWindowTitle("smashDR presets")
        self.resize(1000, 900)
        self.get_current = get_current
        store = load_store()
        self.presets = store["presets"]
        self.collapsed = set(store["collapsed"])
        self.defaults_seen = store["defaults_seen"]
        self.button_size = store["button_size"]
        self._buttons = []
        self._sections = {}           # group -> (header button, holder widget)
        self._active = None          # (freq_hz, mode)

        add_btn = QtWidgets.QPushButton("Add current")
        add_btn.clicked.connect(self.add_current)
        imp_btn = QtWidgets.QPushButton("Import…")
        imp_btn.setToolTip("Paste rows copied from RadioReference")
        imp_btn.clicked.connect(self.import_rr)
        exp_btn = QtWidgets.QToolButton()
        exp_btn.setText("Expand all")
        exp_btn.clicked.connect(lambda: self._set_all_collapsed(False))
        col_btn = QtWidgets.QToolButton()
        col_btn.setText("Collapse all")
        col_btn.clicked.connect(lambda: self._set_all_collapsed(True))
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter by name or frequency")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.count_lbl = QtWidgets.QLabel()

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(add_btn)
        bar.addWidget(imp_btn)
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.count_lbl)
        self.size_box = QtWidgets.QComboBox()
        self.size_box.addItems(list(BUTTON_SIZES))
        self.size_box.setCurrentText(self.button_size)
        self.size_box.currentTextChanged.connect(self._set_button_size)
        bar2 = QtWidgets.QHBoxLayout()
        bar2.addWidget(QtWidgets.QLabel("Button size"))
        bar2.addWidget(self.size_box)
        bar2.addStretch(1)
        bar2.addWidget(exp_btn)
        bar2.addWidget(col_btn)

        self.body = QtWidgets.QWidget()
        self.body_lay = QtWidgets.QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(4, 4, 4, 4)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.body)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addLayout(bar2)
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
        self._sections = {}
        for group in groups_in_order(self.presets):
            n = sum(1 for p in self.presets if p["group"] == group)
            hdr = QtWidgets.QToolButton()
            hdr.setObjectName("groupHeader")
            hdr.setText(f"{group}  ({n})")
            hdr.setCheckable(True)
            hdr.setChecked(group not in self.collapsed)
            hdr.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            hdr.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                              QtWidgets.QSizePolicy.Policy.Fixed)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            holder = QtWidgets.QWidget()
            flow = FlowLayout(holder)
            for p in self.presets:
                if p["group"] != group:
                    continue
                b = PresetButton(p, self.button_size)
                b.clicked.connect(lambda _c, pp=p: self._clicked(pp))
                b.customContextMenuRequested.connect(
                    lambda pos, btn=b: self._context_menu(btn, pos))
                flow.addWidget(b)
                self._buttons.append(b)
            self.body_lay.addWidget(hdr)
            self.body_lay.addWidget(holder)
            self._sections[group] = (hdr, holder)
            self._style_section(group)
            hdr.toggled.connect(lambda on, g=group: self._toggle_group(g, on))
        if not self.presets:
            empty = QtWidgets.QLabel("No presets yet. Tune somewhere and click Add current.")
            empty.setWordWrap(True)
            self.body_lay.addWidget(empty)
        self.body_lay.addStretch(1)
        self._apply_filter(self.filter.text())
        self._refresh_active()

    def _set_button_size(self, size):
        self.button_size = size
        self._save()
        self.rebuild()

    # ---- collapsing ----
    def _style_section(self, group):
        hdr, holder = self._sections[group]
        is_open = hdr.isChecked()
        hdr.setArrowType(QtCore.Qt.ArrowType.DownArrow if is_open else QtCore.Qt.ArrowType.RightArrow)
        holder.setVisible(is_open or bool(self.filter.text().strip()))

    def _toggle_group(self, group, is_open):
        if is_open:
            self.collapsed.discard(group)
        else:
            self.collapsed.add(group)
        self._style_section(group)
        self._save()

    def _set_all_collapsed(self, collapsed: bool):
        for g, (hdr, _h) in self._sections.items():
            hdr.blockSignals(True)
            hdr.setChecked(not collapsed)
            hdr.blockSignals(False)
            (self.collapsed.add if collapsed else self.collapsed.discard)(g)
            self._style_section(g)
        self._save()

    def _apply_filter(self, text):
        t = text.strip().lower()
        shown = 0
        for b in self._buttons:
            p = b.preset
            hay = f"{p['name']} {p['freq_hz'] / 1e6:.4f} {p['mode']} {p['group']} {p.get('note', '')}".lower()
            vis = not t or t in hay
            b.setVisible(vis)
            shown += vis
        for g, (hdr, holder) in self._sections.items():
            any_vis = any(b.preset["group"] == g and (not t or b.isVisibleTo(holder))
                          for b in self._buttons)
            hdr.setVisible(any_vis or not t)
            self._style_section(g)
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
            save_store({"presets": self.presets, "collapsed": sorted(self.collapsed),
                        "defaults_seen": self.defaults_seen,
                        "button_size": self.button_size})
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

    def import_rr(self):
        dlg = ImportDialog(groups_in_order(self.presets), self)
        if not dlg.exec():
            return
        ps, skipped = dlg.parsed()
        have = {(p["freq_hz"], p["group"]) for p in self.presets}
        added = [p for p in ps if (p["freq_hz"], p["group"]) not in have]
        self.presets.extend(added)
        for g in groups_in_order(added):
            self.collapsed.discard(g)
        self._save()
        self.rebuild()
        dupes = len(ps) - len(added)
        QtWidgets.QMessageBox.information(
            self, "Import",
            f"Added {len(added)} preset(s)." +
            (f" {dupes} already existed in that group." if dupes else "") +
            (f" Skipped {len(skipped)} digital channel(s)." if skipped else ""))

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

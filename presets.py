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
BUTTON_SIZES = {"Small": (140, 50, 9), "Medium": (180, 60, 10), "Large": (220, 72, 12)}
DEFAULT_BUTTON_SIZE = "Large"
PINNED = "__pinned__"        # key of the virtual Pinned section
PINNED_COLOR = "#ef9f27"
GROUP_PALETTE = [("Red", "#e24b4a"), ("Purple", "#7f77dd"), ("Green", "#1d9e75"),
                 ("Blue", "#378add"), ("Amber", "#ef9f27"), ("Pink", "#d4537e"),
                 ("Teal", "#2bb3c0"), ("Coral", "#d85a30"), ("Lime", "#7cb342"),
                 ("Gray", "#888780")]
# first match wins: group name (lower case) contains the keyword -> colour
GROUP_HINTS = (("fire", "#e24b4a"), ("lafd", "#e24b4a"), ("ems", "#e24b4a"),
               ("p25", "#7f77dd"), ("lapd", "#7f77dd"), ("police", "#7f77dd"),
               ("sheriff", "#7f77dd"), ("wilson", "#1d9e75"), ("repeater", "#1d9e75"),
               ("ham", "#d4537e"), ("air", "#378add"), ("weather", "#888780"),
               ("noaa", "#888780"), ("marine", "#2bb3c0"), ("fm", "#ef9f27"))
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
    if p.get("digital"):
        out["digital"] = str(p["digital"])
    if p.get("pinned"):
        out["pinned"] = True
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
            "button_size": size if size in BUTTON_SIZES else DEFAULT_BUTTON_SIZE,
            "groups": [str(g) for g in data.get("groups", []) if str(g).strip()],
            "group_colors": {str(k): str(v) for k, v in (data.get("group_colors") or {}).items()
                             if QtGui.QColor(str(v)).isValid()}}


def load_store() -> dict:
    """User presets merged with any defaults they haven't seen yet."""
    defaults = _read(DEFAULT_STORE) or {"presets": [], "collapsed": [], "defaults_seen": [],
                                        "button_size": DEFAULT_BUTTON_SIZE, "groups": [], "group_colors": {}}
    user = _read(STORE)
    if user is None:
        return {"presets": list(defaults["presets"]),
                "collapsed": list(defaults["collapsed"]),
                "defaults_seen": [preset_key(p) for p in defaults["presets"]],
                "button_size": DEFAULT_BUTTON_SIZE, "groups": [], "group_colors": {}}
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

        mode_raw = rr_mode.replace(" ", "")
        mode_u = mode_raw.upper()
        digital = None
        if any(m in mode_u for m in DIGITAL_MARKERS):
            # RadioReference: trailing uppercase E = encrypted, lowercase e = partly encrypted
            if mode_raw.endswith("E") and mode_u.rstrip("E") != mode_u:
                skipped.append(f"{f:.4f} {alpha or desc} ({rr_mode}, encrypted)")
                continue
            digital = mode_u.rstrip("E")
            if mode_raw.endswith("e"):
                digital += " (part enc)"
        mode = MODE_MAP.get(mode_u, "NFM")
        freq_hz = int(round(f * 1e6))
        name = (alpha or desc or f"{f:.4f}")[:18]
        note = " · ".join(x for x in (desc, tone, tag) if x)
        p = {"name": name, "freq_hz": freq_hz, "mode": mode,
             "group": (tag if group_by_tag and tag else group) or "Imported",
             "step_hz": _default_step(freq_hz, mode)}
        if note:
            p["note"] = note
        if digital:
            p["digital"] = digital
        out.append(p)
    return out, skipped


def groups_in_order(presets: list, order: list = ()) -> list:
    """Groups in the saved order (empty groups included), then any others in the
    order their first preset appears."""
    seen = [g for g in dict.fromkeys(order)]
    for p in presets:
        if p["group"] not in seen:
            seen.append(p["group"])
    return seen


def auto_group_color(group: str, index: int) -> str:
    g = group.lower()
    for key, col in GROUP_HINTS:
        if key in g:
            return col
    return GROUP_PALETTE[index % len(GROUP_PALETTE)][1]


def _rgba(hex_color: str, alpha: float) -> str:
    c = QtGui.QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha * 255)})"


def _lighter(hex_color: str, amount: float = 0.45) -> str:
    """Mix towards white (readable accent text on a dark background)."""
    c = QtGui.QColor(hex_color)
    mix = [int(v + (255 - v) * amount) for v in (c.red(), c.green(), c.blue())]
    return "#%02x%02x%02x" % tuple(mix)


def _solid(hex_color: str, alpha: float, base: str = "#262626") -> str:
    """hex_color at alpha over a dark base, as an opaque #rrggbb (for rich text)."""
    c, b = QtGui.QColor(hex_color), QtGui.QColor(base)
    mix = [int(bv + (cv - bv) * alpha) for cv, bv in ((c.red(), b.red()), (c.green(), b.green()),
                                                      (c.blue(), b.blue()))]
    return "#%02x%02x%02x" % tuple(mix)


def dot_icon(hex_color: str, size: int = 12) -> QtGui.QIcon:
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    qp = QtGui.QPainter(pm)
    qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    qp.setBrush(QtGui.QColor(hex_color))
    qp.setPen(QtCore.Qt.PenStyle.NoPen)
    qp.drawEllipse(1, 1, size - 2, size - 2)
    qp.end()
    return QtGui.QIcon(pm)


def preset_subtitle(p: dict) -> str:
    return f"{p['freq_hz'] / 1e6:.4f} {p.get('digital') or p['mode']}"


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
        self.digital = QtWidgets.QComboBox()
        self.digital.setEditable(True)
        self.digital.addItems(["", "P25", "P25 CC", "P25 Phase II CC", "DMR"])
        self.digital.setCurrentText(preset.get("digital", ""))
        self.digital.setToolTip("Digital mode label (for the upcoming decoders). Empty = analog")

        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Frequency", self.freq)
        form.addRow("Mode", self.mode)
        form.addRow("Step", self.step)
        form.addRow("Squelch", self.squelch)
        form.addRow("Group", self.group)
        form.addRow("Digital", self.digital)
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
        if self.digital.currentText().strip():
            p["digital"] = self.digital.currentText().strip()
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
            "Clear digital channels (P25, DMR, ...) are kept and labelled; encrypted ones "
            "(P25E etc.) are skipped.")
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
            msg += f"\nSkipping {len(skipped)} encrypted: " + ", ".join(skipped[:3])
            msg += " ..." if len(skipped) > 3 else ""
        self.preview.setText(msg)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(bool(ps))


# --------------------------------------------------------------------------
# Presets window
# --------------------------------------------------------------------------
class PresetButton(QtWidgets.QPushButton):
    """Name on top, frequency + mode badge underneath, tinted with its group colour."""

    def __init__(self, preset: dict, size: str = DEFAULT_BUTTON_SIZE, color: str = "#888780",
                 section: str = "", parent=None):
        super().__init__(parent)
        self.preset = preset
        self.section = section              # the section this button sits in (group or PINNED)
        self.color = color
        self._live = False
        self.setObjectName("pb")
        self.setCheckable(True)
        w, h, pt = BUTTON_SIZES.get(size, BUTTON_SIZES[DEFAULT_BUTTON_SIZE])
        self.setFixedSize(w, h)
        self.name_lbl = QtWidgets.QLabel(preset["name"])
        self.name_lbl.setObjectName("nm")
        f = self.font()
        f.setPointSize(pt)
        f.setBold(True)
        self.name_lbl.setFont(f)
        self.sub_lbl = QtWidgets.QLabel()
        self.sub_lbl.setObjectName("sub")
        self.sub_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f2 = self.font()
        f2.setPointSize(max(8, pt - 2))
        self.sub_lbl.setFont(f2)
        for lbl in (self.name_lbl, self.sub_lbl):
            lbl.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 8, 4)
        lay.setSpacing(1)
        lay.addWidget(self.name_lbl)
        lay.addWidget(self.sub_lbl)
        tip = f"{preset['name']}  ·  {preset_subtitle(preset)}  ·  {preset['group']}"
        if preset.get("note"):
            tip += f"\n{preset['note']}"
        self.setToolTip(tip)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        c = color
        self.setStyleSheet(f"""
            QPushButton#pb {{ background: {_rgba(c, 0.16)}; border: 1px solid {_rgba(c, 0.55)};
                              border-radius: 6px; }}
            QPushButton#pb:hover {{ background: {_rgba(c, 0.30)}; }}
            QPushButton#pb:checked {{ background: {_rgba(c, 0.42)}; border: 2px solid {_lighter(c, 0.3)}; }}
            QLabel#nm {{ background: transparent; color: #f2f2f2; }}
            QLabel#sub {{ background: transparent; color: {_lighter(c)}; }}
        """)
        self._update_sub()

    def set_live(self, live: bool):
        if live != self._live:
            self._live = live
            self._update_sub()

    def _update_sub(self):
        p = self.preset
        badge = p.get("digital") or p["mode"]
        bc = "#7f77dd" if p.get("digital") else self.color
        html = (f"<span style='font-family:Consolas,monospace'>{p['freq_hz'] / 1e6:.4f}</span>"
                f"&nbsp; <span style='background-color:{_solid(bc, 0.75)}; color:#ffffff'>&nbsp;{badge}&nbsp;</span>")
        if self._live:
            html += "&nbsp; <span style='color:#5ee08a'>● live</span>"
        self.sub_lbl.setText(html)


class PresetsWindow(QtWidgets.QWidget):
    """Top-level window of preset buttons. Emits tune_requested(preset dict)."""

    tune_requested = QtCore.pyqtSignal(dict)
    presets_changed = QtCore.pyqtSignal(list)

    STYLE = """
        QListWidget#groupList { border: none; font-size: 11pt; }
        QListWidget#groupList::item { padding: 6px 4px; }
    """

    def __init__(self, get_current, parent=None):
        """get_current() -> dict with freq_hz, mode, step_hz, squelch_db of the receiver."""
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        self.setWindowTitle("smashDR presets")
        self.resize(1900, 1040)
        self.get_current = get_current
        store = load_store()
        self.presets = store["presets"]
        self.collapsed = set(store["collapsed"])
        self.defaults_seen = store["defaults_seen"]
        self.button_size = store["button_size"]
        self.group_order = list(store.get("groups", []))      # all groups, may include empty ones
        self.group_colors = dict(store.get("group_colors", {}))
        self._live_freq = None
        self._buttons = []
        self._sections = {}           # group -> (header button, holder widget)
        self._active = None          # (freq_hz, mode)

        add_btn = QtWidgets.QPushButton("Add current")
        add_btn.clicked.connect(self.add_current)
        imp_btn = QtWidgets.QPushButton("Import…")
        imp_btn.setToolTip("Paste rows copied from RadioReference")
        imp_btn.clicked.connect(self.import_rr)
        grp_btn = QtWidgets.QPushButton("New group…")
        grp_btn.clicked.connect(lambda: self.new_group())
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
        bar.addWidget(grp_btn)
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
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.body)

        # group list on the left: click = jump to that group
        self.group_list = QtWidgets.QListWidget()
        self.group_list.setObjectName("groupList")
        self.group_list.setFixedWidth(230)
        self.group_list.setIconSize(QtCore.QSize(12, 12))
        self.group_list.itemClicked.connect(self._jump_to)

        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(bar)
        rl.addLayout(bar2)
        rl.addWidget(self.scroll, 1)
        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel("Groups")
        cap.setStyleSheet("color: gray; padding: 4px;")
        sl.addWidget(cap)
        sl.addWidget(self.group_list, 1)

        lay = QtWidgets.QHBoxLayout(self)
        lay.addWidget(side)
        lay.addWidget(right, 1)
        self.setStyleSheet(self.STYLE)
        self.rebuild()

    # ---- building ----
    def color_of(self, group: str) -> str:
        if group == PINNED:
            return PINNED_COLOR
        if group not in self.group_colors:
            groups = self.groups()
            self.group_colors[group] = auto_group_color(
                group, groups.index(group) if group in groups else len(groups))
        return self.group_colors[group]

    def _make_section(self, key, title, presets):
        color = self.color_of(key)
        hdr = QtWidgets.QToolButton()
        hdr.setObjectName("groupHeader")
        hdr.setText(f"{title}   {len(presets)}")
        hdr.setCheckable(True)
        hdr.setChecked(key not in self.collapsed)
        hdr.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        hdr.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                          QtWidgets.QSizePolicy.Policy.Fixed)
        f = hdr.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        hdr.setFont(f)
        hdr.setStyleSheet(f"QToolButton#groupHeader {{ border: none; border-left: 5px solid {color};"
                          f" background: {_rgba(color, 0.10)}; text-align: left;"
                          f" padding: 5px 8px; margin-top: 8px; }}")
        if key != PINNED:
            hdr.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            hdr.customContextMenuRequested.connect(
                lambda pos, g=key, h=hdr: self._group_menu(g, h, pos))
            hdr.setToolTip("Click to collapse / expand, right-click for colour, rename, move, delete")
        holder = QtWidgets.QWidget()
        flow = FlowLayout(holder)
        if not presets:
            hint = QtWidgets.QLabel("Empty group: right-click a preset > Move to group, "
                                    "or Add current")
            hint.setStyleSheet("color: gray; padding: 4px;")
            flow.addWidget(hint)
        for p in presets:
            b = PresetButton(p, self.button_size, self.color_of(p["group"]), section=key)
            b.clicked.connect(lambda _c, pp=p: self._clicked(pp))
            b.customContextMenuRequested.connect(
                lambda pos, btn=b: self._context_menu(btn, pos))
            flow.addWidget(b)
            self._buttons.append(b)
        self.body_lay.addWidget(hdr)
        self.body_lay.addWidget(holder)
        self._sections[key] = (hdr, holder)
        self._style_section(key)
        hdr.toggled.connect(lambda on, g=key: self._toggle_group(g, on))
        item = QtWidgets.QListWidgetItem(dot_icon(color), f"{title}   ({len(presets)})")
        item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
        self.group_list.addItem(item)

    def rebuild(self):
        while self.body_lay.count():
            it = self.body_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.group_list.clear()
        self._buttons = []
        self._sections = {}
        pinned = [p for p in self.presets if p.get("pinned")]
        if pinned:
            self._make_section(PINNED, "Pinned", pinned)
        for group in self.groups():
            self._make_section(group, group, [p for p in self.presets if p["group"] == group])
        if not self.presets:
            empty = QtWidgets.QLabel("No presets yet. Tune somewhere and click Add current.")
            empty.setWordWrap(True)
            self.body_lay.addWidget(empty)
        self.body_lay.addStretch(1)
        self._apply_filter(self.filter.text())
        self._refresh_active()

    def _jump_to(self, item):
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if key not in self._sections:
            return
        hdr, _holder = self._sections[key]
        if not hdr.isChecked():
            hdr.setChecked(True)                     # expands (and saves) via toggled
        QtCore.QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(hdr.y()))

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
            any_vis = any(b.section == g and (not t or b.isVisibleTo(holder))
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
            b.set_live(self._live_freq is not None and b.preset["freq_hz"] == self._live_freq)

    def set_live(self, freq_hz):
        """Mark the preset(s) on freq_hz as having a call in progress (None = no call)."""
        self._live_freq = None if freq_hz is None else int(freq_hz)
        self._refresh_active()

    # ---- actions ----
    def _clicked(self, p):
        self.tune_requested.emit(dict(p))
        self._refresh_active()

    def _save(self):
        try:
            self.group_order = self.groups()
            save_store({"presets": self.presets, "collapsed": sorted(self.collapsed),
                        "defaults_seen": self.defaults_seen,
                        "button_size": self.button_size,
                        "groups": self.group_order,
                        "group_colors": {g: c for g, c in self.group_colors.items()
                                         if g in self.group_order}})
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Presets", f"Couldn't save presets: {e}")
        self.presets_changed.emit(list(self.presets))

    def add_current(self):
        cur = self.get_current()
        groups = self.groups() or ["General"]
        dlg = PresetDialog({"name": f"{cur['freq_hz'] / 1e6:.4f}", **cur}, groups, self,
                           title="Add preset")
        if dlg.exec():
            self.presets.append(dlg.result_preset())
            self._save()
            self.rebuild()

    def import_rr(self):
        dlg = ImportDialog(self.groups(), self)
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
            (f" Skipped {len(skipped)} encrypted channel(s)." if skipped else ""))

    def _context_menu(self, btn, pos):
        p = btn.preset
        menu = QtWidgets.QMenu(self)
        a_edit = menu.addAction("Edit…")
        a_dup = menu.addAction("Duplicate")
        a_upd = menu.addAction("Update to current frequency")
        a_pin = menu.addAction("Unpin" if p.get("pinned") else "Pin to top")
        move = menu.addMenu("Move to group")
        moves = {}
        for g in self.groups():
            a = move.addAction(g)
            a.setEnabled(g != p["group"])
            moves[a] = g
        move.addSeparator()
        a_newg = move.addAction("New group…")
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen is None:
            return
        i = self.presets.index(p)
        if chosen == a_edit:
            dlg = PresetDialog(p, self.groups(), self)
            if dlg.exec():
                self.presets[i] = dlg.result_preset()
        elif chosen == a_dup:
            self.presets.insert(i + 1, dict(p, name=p["name"] + " copy"))
        elif chosen == a_upd:
            cur = self.get_current()
            self.presets[i] = dict(p, freq_hz=cur["freq_hz"], mode=cur["mode"])
        elif chosen == a_pin:
            q = dict(p)
            if p.get("pinned"):
                q.pop("pinned", None)
            else:
                q["pinned"] = True
            self.presets[i] = q
        elif chosen in moves:
            self.presets[i] = dict(p, group=moves[chosen])
        elif chosen == a_newg:
            g = self.new_group(rebuild=False)
            if not g:
                return
            self.presets[i] = dict(p, group=g)
        elif chosen == a_del:
            if QtWidgets.QMessageBox.question(self, "Delete preset",
                                              f"Delete “{p['name']}”?") != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.presets.pop(i)
        self._save()
        self.rebuild()

    # ---- groups ----
    def groups(self) -> list:
        return groups_in_order(self.presets, self.group_order)

    def _ask_group_name(self, title, text=""):
        name, ok = QtWidgets.QInputDialog.getText(self, title, "Group name:", text=text)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self.groups() and name != text:
            QtWidgets.QMessageBox.information(self, title, f"There's already a group “{name}”.")
            return None
        return name

    def new_group(self, rebuild=True):
        name = self._ask_group_name("New group")
        if name:
            self.group_order = self.groups() + [name]
            self.collapsed.discard(name)
            if rebuild:
                self._save()
                self.rebuild()
        return name

    def _group_menu(self, group, hdr, pos):
        groups = self.groups()
        idx = groups.index(group)
        n = sum(1 for p in self.presets if p["group"] == group)
        menu = QtWidgets.QMenu(self)
        a_ren = menu.addAction("Rename…")
        cmenu = menu.addMenu("Colour")
        colours = {}
        for cname, chex in GROUP_PALETTE:
            colours[cmenu.addAction(dot_icon(chex), cname)] = chex
        cmenu.addSeparator()
        a_custom = cmenu.addAction("Custom…")
        a_up = menu.addAction("Move up")
        a_up.setEnabled(idx > 0)
        a_down = menu.addAction("Move down")
        a_down.setEnabled(idx < len(groups) - 1)
        menu.addSeparator()
        a_new = menu.addAction("New group…")
        menu.addSeparator()
        a_del = menu.addAction("Delete group" + (f" and its {n} preset(s)…" if n else ""))
        chosen = menu.exec(hdr.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen in colours:
            self.group_colors[group] = colours[chosen]
        elif chosen == a_custom:
            c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.color_of(group)), self,
                                                f"Colour for {group}")
            if not c.isValid():
                return
            self.group_colors[group] = c.name()
        elif chosen == a_ren:
            name = self._ask_group_name("Rename group", group)
            if not name or name == group:
                return
            for i, p in enumerate(self.presets):
                if p["group"] == group:
                    self.presets[i] = dict(p, group=name)
            self.group_order = [name if g == group else g for g in groups]
            if group in self.group_colors:
                self.group_colors[name] = self.group_colors.pop(group)
            if group in self.collapsed:
                self.collapsed.discard(group)
                self.collapsed.add(name)
        elif chosen in (a_up, a_down):
            order = list(groups)
            j = idx - 1 if chosen == a_up else idx + 1
            order[idx], order[j] = order[j], order[idx]
            self.group_order = order
        elif chosen == a_new:
            if not self.new_group(rebuild=False):
                return
        elif chosen == a_del:
            if n and QtWidgets.QMessageBox.question(
                    self, "Delete group",
                    f"Delete “{group}” and the {n} preset(s) in it?") != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self.presets = [p for p in self.presets if p["group"] != group]
            self.group_order = [g for g in groups if g != group]
            self.collapsed.discard(group)
        self._save()
        self.rebuild()

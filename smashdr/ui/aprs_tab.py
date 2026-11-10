"""
aprs_tab.py - "APRS" tab: every APRS packet the scanner decodes (144.390 and the ISS
on 145.825), stations on a map with their symbol and track, a station list and a
filterable log. Receive only. Packets come from SdrReader's `aprs` signal; the log
file user/aprs.txt is written here (and the last packets are reloaded at start).
"""

import json
import math
import time

from PyQt6 import QtCore, QtGui, QtWidgets

from smashdr.decoders import aprs as A
from smashdr.paths import USER
from smashdr.ui.aircraft import DEFAULT_HOME, HAVE_WEB, QWebEngineView

if HAVE_WEB:
    from PyQt6.QtWebEngineCore import QWebEnginePage

    class _Page(QWebEnginePage):
        selected = QtCore.pyqtSignal(str)

        def javaScriptConsoleMessage(self, level, message, line, source):
            if message.startswith("SELECT:"):
                self.selected.emit(message[7:])

LOG = USER / "aprs.txt"
HISTORY = 500
ORANGE, GREEN = "#ff8a65", "#81c784"

MAP_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;background:#1e1e1e}
.st{font:16px "Segoe UI Emoji","Apple Color Emoji",sans-serif;line-height:22px;text-align:center;
    width:22px;height:22px;border-radius:11px;background:rgba(20,28,40,.85);border:2px solid #81c784}
.st.iss{border-color:#ff8a65}.st.sel{border-color:#fff;box-shadow:0 0 0 2px #00d9e8}
.cl{font:600 10px Segoe UI,Arial,sans-serif;color:#e0f2f1;background:rgba(10,16,24,.75);border:none;
    border-radius:6px;padding:0 4px;box-shadow:none}.cl:before{display:none}</style>
</head><body><div id="map"></div><script>
var map = L.map('map').setView([__LAT__, __LON__], 9);
var key = __CARTO_KEY__, esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
if (key) {
  L.tileLayer('https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key=' +
    encodeURIComponent(key), {maxZoom: 18, attribution: '&copy; OpenStreetMap &copy; CARTO'}).addTo(map);
} else {
  L.tileLayer(esri + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16,
    attribution: 'Tiles &copy; Esri'}).addTo(map);
  L.tileLayer(esri + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16}).addTo(map);
}
L.circleMarker([__LAT__, __LON__], {radius: 5, color: '#ffd54f', fillOpacity: 1}).addTo(map).bindTooltip('home');
var objs = {}, sel = null;
function esc(t){ return String(t == null ? '' : t).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function icon(s){
  return L.divIcon({className: '', iconSize: [26, 26], iconAnchor: [13, 13],
    html: '<div class="st' + (s.iss ? ' iss' : '') + (s.call === sel ? ' sel' : '') + '">' + s.glyph + '</div>'});
}
function labels(){
  var show = map.getZoom() >= 11;
  Object.keys(objs).forEach(function(k){ var m = objs[k].m;
    if (show && !m.isTooltipOpen()) m.openTooltip(); if (!show && m.isTooltipOpen()) m.closeTooltip(); });
}
map.on('zoomend', labels);
function stations(list){
  list.forEach(function(s){
    var o = objs[s.call];
    var pop = '<b>' + esc(s.call) + '</b> ' + esc(s.what) + (s.iss ? ' (via the ISS)' : '') +
              '<br>' + esc(s.when) + (s.extra ? '<br>' + esc(s.extra) : '') +
              '<br><span style="font-family:Consolas,monospace;font-size:11px">' + esc(s.text) + '</span>';
    if (!o) {
      o = objs[s.call] = {
        m: L.marker([s.lat, s.lon], {icon: icon(s)}).addTo(map),
        t: L.polyline(s.trail, {color: s.iss ? '#ff8a65' : '#81c784', weight: 1.5, opacity: 0.6,
                                interactive: false}).addTo(map)};
      o.m.bindTooltip(esc(s.call), {permanent: true, direction: 'right', offset: [12, 0], className: 'cl'});
      o.m.bindPopup(pop);
      o.m.on('click', function(){ console.log('SELECT:' + s.call); });
    } else {
      o.m.setLatLng([s.lat, s.lon]); o.m.setIcon(icon(s)); o.m.setPopupContent(pop); o.t.setLatLngs(s.trail);
    }
    o.s = s;
  });
  labels();
}
function select(call, pan){
  var prev = sel; sel = call;
  [prev, call].forEach(function(c){ if (c && objs[c]) objs[c].m.setIcon(icon(objs[c].s)); });
  if (pan && objs[call]) { map.setView(objs[call].m.getLatLng(), Math.max(map.getZoom(), 11)); objs[call].m.openPopup(); }
}
function clearAll(){ Object.keys(objs).forEach(function(k){ map.removeLayer(objs[k].m); map.removeLayer(objs[k].t); });
  objs = {}; sel = null; }
</script></body></html>"""

FILTERS = ["All packets", "Positions", "Messages", "Weather", "Status", "Via the ISS", "ISS channel"]


def _miles(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(min(1.0, h)))


def _ago(s):
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 86400}d"


class AprsTab(QtWidgets.QWidget):
    tune_request = QtCore.pyqtSignal(int)          # Hz (NFM)

    def __init__(self, opts: dict, parent=None):
        super().__init__(parent)
        self.opts = opts
        self.packets = []                          # newest last, at most HISTORY
        self.stations = {}                         # call -> dict
        self._map_ready = False
        self._dirty = set()

        listen = QtWidgets.QPushButton("📡 Listen on 144.390")
        listen.setToolTip("Tune the scanner to 144.390 MHz NFM. Packets are decoded whenever 144.390 or\n"
                          "145.825 (ISS) is inside the scanner's span, whatever mode you are listening in.")
        listen.clicked.connect(lambda: self.tune_request.emit(144_390_000))
        self.stat_lbl = QtWidgets.QLabel("No packets yet")
        self.stat_lbl.setStyleSheet("color: gray")
        top = QtWidgets.QHBoxLayout()
        top.addWidget(listen)
        top.addWidget(self.stat_lbl, 1)

        # map + station list
        if HAVE_WEB:
            self.map = QWebEngineView()
            page = _Page(self.map)
            page.selected.connect(lambda c: self._select(c, from_map=True))
            self.map.setPage(page)
            self.map.loadFinished.connect(self._map_loaded)
            map_w = self.map
        else:
            self.map = None
            map_w = QtWidgets.QLabel("Install PyQt6-WebEngine for the map (pip install PyQt6-WebEngine).")
            map_w.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.st_table = QtWidgets.QTableWidget(0, 6)
        self.st_table.setHorizontalHeaderLabels(["", "Call", "Type", "Last", "Pkts", "Dist"])
        self._table_style(self.st_table, stretch=2)
        self.st_table.setSortingEnabled(True)
        self.st_table.itemSelectionChanged.connect(self._st_selected)
        self.st_table.itemDoubleClicked.connect(lambda _i: self._select(self._sel_call(), pan=True))

        self.hsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.hsplit.addWidget(map_w)
        self.hsplit.addWidget(self.st_table)
        hs = opts.get("aprs_hsplit")
        self.hsplit.setSizes(hs if isinstance(hs, list) and len(hs) == 2 else [760, 420])

        # log
        self.filter_box = QtWidgets.QComboBox()
        self.filter_box.addItems(FILTERS)
        self.filter_box.setCurrentText(opts.get("aprs_filter", "All packets"))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Callsign or text…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        open_btn = QtWidgets.QPushButton("Open aprs.txt")
        open_btn.clicked.connect(self._open_log)
        self.log = QtWidgets.QTableWidget(0, 5)
        self.log.setHorizontalHeaderLabels(["Time", "Ch", "From", "Type", "Packet"])
        self._table_style(self.log, stretch=4)
        self.log.itemDoubleClicked.connect(self._log_clicked)
        frow = QtWidgets.QHBoxLayout()
        frow.addWidget(QtWidgets.QLabel("<b>Log</b>"))
        frow.addWidget(self.filter_box)
        frow.addWidget(self.search)
        frow.addStretch(1)
        frow.addWidget(open_btn)
        lv = QtWidgets.QVBoxLayout()
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addLayout(frow)
        lv.addWidget(self.log, 1)
        log_w = QtWidgets.QWidget()
        log_w.setLayout(lv)

        self.vsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.vsplit.addWidget(self.hsplit)
        self.vsplit.addWidget(log_w)
        vs = opts.get("aprs_vsplit")
        self.vsplit.setSizes(vs if isinstance(vs, list) and len(vs) == 2 else [520, 300])

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.vsplit, 1)

        self.filter_box.currentTextChanged.connect(lambda _t: self._refill_log())
        self.search.textChanged.connect(lambda _t: self._refill_log())
        self._load_history()
        self._build_map()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(5000)

    @staticmethod
    def _table_style(t, stretch):
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        t.setWordWrap(False)
        hh = t.horizontalHeader()
        for c in range(t.columnCount()):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.Stretch if c == stretch
                                    else QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

    def home(self):
        return (float(self.opts.get("home_lat", DEFAULT_HOME[0])),
                float(self.opts.get("home_lon", DEFAULT_HOME[1])))

    # ---- packets ----------------------------------------------------------------
    def on_aprs(self, pkts, log=True):
        for p in pkts:
            self.packets.append(p)
            self._station(p)
            if log:
                try:
                    USER.mkdir(exist_ok=True)
                    with open(LOG, "a", encoding="utf-8") as f:
                        f.write(A.log_line(p))
                except OSError:
                    pass
            if self._match(p):
                self._log_row(p, 0)
        del self.packets[:-HISTORY]
        while self.log.rowCount() > HISTORY:
            self.log.removeRow(self.log.rowCount() - 1)
        if log:
            self._push()
            self._fill_stations()
            self._status()

    def _station(self, p):
        call = p["src"]
        s = self.stations.setdefault(call, {"call": call, "n": 0, "lat": None, "lon": None, "trail": [],
                                            "sym": "", "iss": False, "t": 0, "text": "", "extra": ""})
        s["n"] += 1
        s["t"] = max(s["t"], p["t"])
        s["text"] = p["text"][:220]
        s["iss"] = s["iss"] or p["via_iss"]
        s["direct"] = not any(c.endswith("*") for c in p.get("path", "").split(","))
        if p.get("lat") is not None:
            if s["lat"] is None or abs(s["lat"] - p["lat"]) + abs(s["lon"] - p["lon"]) > 1e-4:
                s["trail"] = (s["trail"] + [[round(p["lat"], 5), round(p["lon"], 5)]])[-80:]
            s["lat"], s["lon"] = p["lat"], p["lon"]
            s["sym"] = p.get("sym", s["sym"])
            extra = []
            if p.get("speed_kn"):
                extra.append(f"{p['speed_kn'] * 1.15078:.0f} mph, course {p.get('course', 0)}°")
            if p.get("temp_f") is not None:
                extra.append(f"{p['temp_f']} °F")
            if p.get("wind"):
                extra.append(f"wind {p['wind'][1]} mph from {p['wind'][0]}°")
            if p.get("comment"):
                extra.append(p["comment"][:80])
            s["extra"] = " · ".join(extra)
            self._dirty.add(call)

    def _kind(self, p):
        k = p.get("kind", "other")
        return f"msg → {p.get('to', '')}" if k == "message" else k

    def _match(self, p):
        f = self.filter_box.currentText()
        k = p.get("kind")
        ok = (f == "All packets" or (f == "Positions" and k in ("position", "weather"))
              or (f == "Messages" and k == "message") or (f == "Weather" and k == "weather")
              or (f == "Status" and k == "status") or (f == "Via the ISS" and p["via_iss"])
              or (f == "ISS channel" and p["band"] == "ISS"))
        q = self.search.text().strip().upper()
        return ok and (not q or q in p["text"].upper())

    def _log_row(self, p, row):
        t = self.log
        t.insertRow(row)
        lt = time.localtime(p["t"])
        when = time.strftime("%H:%M:%S" if time.time() - p["t"] < 20 * 3600 else "%b %d %H:%M", lt)
        ch = "ISS" if p["band"] == "ISS" else "144.39"
        for c, v in enumerate((when, ch, p["src"], self._kind(p), p["text"])):
            it = QtWidgets.QTableWidgetItem(v)
            if p["via_iss"]:
                it.setForeground(QtGui.QColor(ORANGE))
            if c == 4:
                it.setToolTip(p["text"])
            elif c == 1:
                it.setToolTip(f"{p['freq'] / 1e6:.3f} MHz" + (" · digipeated by the ISS" if p["via_iss"] else ""))
            t.setItem(row, c, it)
        t.item(row, 0).setData(QtCore.Qt.ItemDataRole.UserRole, p["src"])

    def _refill_log(self):
        self.log.setRowCount(0)
        for p in reversed(self.packets):
            if self._match(p):
                self._log_row(p, self.log.rowCount())

    def _fill_stations(self):
        t = self.st_table
        sel = self._sel_call()
        hl, ho = self.home()
        now = time.time()
        t.setSortingEnabled(False)
        t.blockSignals(True)
        t.setRowCount(len(self.stations))
        for r, s in enumerate(sorted(self.stations.values(), key=lambda x: -x["t"])):
            glyph, name = A.symbol(s["sym"])
            dist = _miles(hl, ho, s["lat"], s["lon"]) if s["lat"] is not None else None
            vals = (glyph, s["call"], name or ("no position" if s["lat"] is None else ""),
                    _ago(now - s["t"]), s["n"], dist)
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem()
                if c in (4, 5):
                    if v is not None:
                        it.setData(QtCore.Qt.ItemDataRole.DisplayRole, round(v, 1) if c == 5 else v)
                else:
                    it.setText(str(v))
                if c == 3:
                    it.setData(QtCore.Qt.ItemDataRole.UserRole, s["t"])
                if s["iss"]:
                    it.setForeground(QtGui.QColor(ORANGE))
                it.setToolTip(s["text"] if c != 5 else "miles from home")
                t.setItem(r, c, it)
            if s["call"] == sel:
                t.selectRow(r)
        t.blockSignals(False)
        t.setSortingEnabled(True)

    def _status(self):
        n_pos = sum(1 for s in self.stations.values() if s["lat"] is not None)
        last = max((p["t"] for p in self.packets), default=None)
        iss = sum(1 for p in self.packets if p["via_iss"])
        txt = (f"{len(self.stations)} stations ({n_pos} on the map) · {len(self.packets)} packets"
               + (f" · {iss} via the ISS" if iss else "")
               + (f" · last {_ago(time.time() - last)} ago" if last else ""))
        self.stat_lbl.setText(txt if self.packets else "No packets yet: tune near 144.390 (or press Listen)")

    def _tick(self):
        if self.stations:
            self._fill_stations()
        self._status()

    # ---- selection ----------------------------------------------------------------
    def _sel_call(self):
        rows = self.st_table.selectionModel().selectedRows() if self.st_table.selectionModel() else []
        if not rows:
            return None
        it = self.st_table.item(rows[0].row(), 1)
        return it.text() if it else None

    def _st_selected(self):
        c = self._sel_call()
        if c:
            self._select(c)

    def _select(self, call, pan=False, from_map=False):
        if from_map:
            for r in range(self.st_table.rowCount()):
                it = self.st_table.item(r, 1)
                if it and it.text() == call:
                    self.st_table.blockSignals(True)
                    self.st_table.selectRow(r)
                    self.st_table.blockSignals(False)
                    self.st_table.scrollToItem(it)
                    break
        self._js(f"select({json.dumps(call)}, {'true' if pan else 'false'})")

    def _log_clicked(self, item):
        call = self.log.item(item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if call:
            self._select(call, pan=True, from_map=True)

    # ---- map -------------------------------------------------------------------------
    def _build_map(self):
        if self.map is None:
            return
        lat, lon = self.home()
        html = (MAP_HTML.replace("__LAT__", f"{lat:.4f}").replace("__LON__", f"{lon:.4f}")
                .replace("__CARTO_KEY__", json.dumps(str(self.opts.get("carto_key", "")).strip())))
        self._map_ready = False
        self.map.setHtml(html, QtCore.QUrl("https://smashdr.local/aprs"))

    def _map_loaded(self, ok):
        self._map_ready = bool(ok)
        if ok:
            self._dirty = {c for c, s in self.stations.items() if s["lat"] is not None}
            self._push()

    def _js(self, code):
        if self.map is not None and self._map_ready:
            self.map.page().runJavaScript(code)

    def _push(self):
        if not self._dirty or not self._map_ready:
            return
        out = []
        for c in self._dirty:
            s = self.stations.get(c)
            if not s or s["lat"] is None:
                continue
            glyph, name = A.symbol(s["sym"])
            out.append({"call": c, "lat": s["lat"], "lon": s["lon"], "glyph": glyph, "what": name,
                        "iss": s["iss"], "trail": s["trail"], "text": s["text"], "extra": s["extra"],
                        "when": time.strftime("last heard %b %d %H:%M:%S", time.localtime(s["t"]))})
        self._dirty.clear()
        if out:
            self._js(f"stations({json.dumps(out)})")

    # ---- file ------------------------------------------------------------------------
    def _load_history(self):
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-HISTORY:]
        except OSError:
            lines = []
        pkts = [p for p in (A.parse_log_line(ln) for ln in lines) if p]
        if pkts:
            self.on_aprs(pkts, log=False)
            self._fill_stations()
        self._status()

    def _open_log(self):
        if not LOG.exists():
            QtWidgets.QMessageBox.information(self, "APRS log", "Nothing logged yet.")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(LOG)))

    def collect(self):
        self.opts.update(aprs_hsplit=self.hsplit.sizes(), aprs_vsplit=self.vsplit.sizes(),
                         aprs_filter=self.filter_box.currentText())

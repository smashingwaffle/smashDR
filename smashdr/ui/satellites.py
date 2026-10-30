"""
satellites.py - "ISS" tab: where the station is, the next passes over home,
a sky plot of a pass, phone alerts before good passes, and (optionally) the
receiver tuned to the downlink with Doppler correction during a pass.
"""

import json
import math
import threading
import time

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

from smashdr.core import notify
from smashdr.core import satellites as S
from smashdr.ui.aircraft import DEFAULT_HOME, HAVE_WEB, QWebEngineView

GOOD_EL = 30.0                       # passes at least this high are highlighted

MAP_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;background:#1e1e1e}
.lbl{font:600 11px Segoe UI,Arial,sans-serif;color:#111;background:#4fc3f7;border:none;
     border-radius:8px;padding:1px 6px;box-shadow:none}.lbl:before{display:none}</style>
</head><body><div id="map"></div><script>
var map = L.map('map', {worldCopyJump: true}).setView([__LAT__, __LON__], 2);
var esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
var key = __CARTO_KEY__;
if (key) {
  L.tileLayer('https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key=' +
    encodeURIComponent(key), {maxZoom: 18, attribution: '&copy; OpenStreetMap &copy; CARTO'}).addTo(map);
} else {
  L.tileLayer(esri + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16,
    attribution: 'Tiles &copy; Esri'}).addTo(map);
  L.tileLayer(esri + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16}).addTo(map);
}
L.circleMarker([__LAT__, __LON__], {radius: 5, color: '#ffd54f', fillOpacity: 1}).addTo(map)
  .bindTooltip('home');
var past = L.layerGroup().addTo(map), fut = L.layerGroup().addTo(map);
var foot = L.circle([0, 0], {radius: 1, color: '#4fc3f7', weight: 1, fillOpacity: 0.06}).addTo(map);
var iss = L.marker([0, 0], {icon: L.divIcon({className: '', iconSize: [26, 26], iconAnchor: [13, 13],
  html: '<svg width="26" height="26" viewBox="-13 -13 26 26"><rect x="-11" y="-3" width="7" height="6" fill="#4fc3f7"/>' +
        '<rect x="4" y="-3" width="7" height="6" fill="#4fc3f7"/><rect x="-4" y="-5" width="8" height="10" rx="2" fill="#fff"/></svg>'})})
  .addTo(map).bindTooltip('__NAME__', {permanent: true, direction: 'right', className: 'lbl', offset: [12, 0]});
var follow = true;
map.on('dragstart', function(){ follow = false; });
function segs(pts) {            // split at the date line
  var out = [], cur = [];
  for (var i = 0; i < pts.length; i++) {
    if (cur.length && Math.abs(pts[i][1] - cur[cur.length - 1][1]) > 180) { out.push(cur); cur = []; }
    cur.push(pts[i]);
  }
  if (cur.length) out.push(cur);
  return out;
}
function update(lat, lon, radius, back, fwd) {
  iss.setLatLng([lat, lon]); foot.setLatLng([lat, lon]); foot.setRadius(radius);
  past.clearLayers(); fut.clearLayers();
  segs(back).forEach(function(s){ L.polyline(s, {color: '#90a4ae', weight: 1.5, dashArray: '4 4'}).addTo(past); });
  segs(fwd).forEach(function(s){ L.polyline(s, {color: '#4fc3f7', weight: 2}).addTo(fut); });
  if (follow) map.panTo([lat, lon], {animate: false});
}
function followIss() { follow = true; }
</script></body></html>"""


def _hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def _dur(s):
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class SkyPlot(pg.PlotWidget):
    """Polar sky plot: centre = overhead, edge = horizon, north up."""

    def __init__(self):
        super().__init__()
        self.setAspectLocked(True)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self.setRange(xRange=(-1.15, 1.15), yRange=(-1.15, 1.15), padding=0)
        grid = pg.mkPen("#455a64", width=1)
        for el in (0, 30, 60):
            r = (90 - el) / 90
            c = QtWidgets.QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            c.setPen(grid)
            self.addItem(c)
            if el:
                t = pg.TextItem(f"{el}°", color="#78909c", anchor=(0, 1))
                t.setPos(0.02, r)
                self.addItem(t)
        for a in (0, 45, 90, 135):
            x, y = math.sin(math.radians(a)), math.cos(math.radians(a))
            self.addItem(pg.PlotCurveItem([-x, x], [-y, y], pen=grid))
        for name, a in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            x, y = 1.08 * math.sin(math.radians(a)), 1.08 * math.cos(math.radians(a))
            t = pg.TextItem(name, color="#cfd8dc", anchor=(0.5, 0.5))
            t.setPos(x, y)
            self.addItem(t)
        self.track = pg.PlotCurveItem(pen=pg.mkPen("#4fc3f7", width=2))
        self.addItem(self.track)
        self.ends = pg.ScatterPlotItem(size=7)
        self.addItem(self.ends)
        self.now = pg.ScatterPlotItem(size=12, brush=pg.mkBrush("#ffd54f"), pen=pg.mkPen("#111"))
        self.addItem(self.now)

    @staticmethod
    def xy(az, el):
        r = (90 - max(0.0, el)) / 90
        return r * math.sin(math.radians(az)), r * math.cos(math.radians(az))

    def show_pass(self, p):
        if p is None:
            self.track.setData([], [])
            self.ends.setData([])
            return
        pts = [self.xy(a, e) for _t, a, e in p.track]
        self.track.setData([x for x, _ in pts], [y for _, y in pts])
        self.ends.setData([{"pos": pts[0], "brush": pg.mkBrush("#66bb6a")},
                           {"pos": pts[-1], "brush": pg.mkBrush("#e57373")}])

    def show_now(self, az, el):
        self.now.setData([self.xy(az, el)] if el > 0 else [])


class SatTab(QtWidgets.QWidget):
    tune_request = QtCore.pyqtSignal(int)          # Hz: tune the receiver (NFM)

    def __init__(self, opts: dict, parent=None):
        super().__init__(parent)
        self.opts = opts
        self.sat = None
        self.passes = []
        self._tle_age = None
        self._loading = False
        self._result = None
        self._alerted = set()
        self._map_ready = False
        self._last_map = 0.0
        self._doppler_on = False
        self._last_tune = 0.0

        self.sat_box = QtWidgets.QComboBox()
        self.sat_box.addItems(list(S.SATS))
        self.down_box = QtWidgets.QComboBox()
        self.tune_btn = QtWidgets.QPushButton("Tune receiver")
        self.tune_btn.setToolTip("Tune the Receiver tab to this downlink (NFM)")
        self.dopp_chk = QtWidgets.QCheckBox("Follow Doppler during passes")
        self.dopp_chk.setToolTip("While the satellite is up, keep the receiver on the downlink\n"
                                 "corrected for Doppler (up to about ±3.5 kHz on 2 m)")
        self.dopp_chk.setChecked(bool(opts.get("sat_doppler", False)))
        self.refresh_btn = QtWidgets.QPushButton("Update orbit")
        self.refresh_btn.setToolTip("Download a fresh orbit (TLE) from CelesTrak")
        top = QtWidgets.QHBoxLayout()
        for w in (QtWidgets.QLabel("Satellite"), self.sat_box, QtWidgets.QLabel("Downlink"),
                  self.down_box, self.tune_btn, self.dopp_chk):
            top.addWidget(w)
        top.addStretch(1)
        top.addWidget(self.refresh_btn)

        self.now_lbl = QtWidgets.QLabel("Loading orbit…")
        self.now_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f = self.now_lbl.font()
        f.setPointSize(f.pointSize() + 3)
        self.now_lbl.setFont(f)
        self.next_lbl = QtWidgets.QLabel("")
        self.next_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f2 = self.next_lbl.font()
        f2.setPointSize(f2.pointSize() + 2)
        self.next_lbl.setFont(f2)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Day", "Start", "Max el", "Length", "Direction", "Visible"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._pass_selected)

        self.sky = SkyPlot()
        self.sky.setMinimumSize(260, 260)
        self.sky_lbl = QtWidgets.QLabel("")
        self.sky_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.alert_chk = QtWidgets.QCheckBox("Phone alert before passes above")
        self.alert_chk.setChecked(bool(opts.get("sat_alert", True)))
        self.alert_el = QtWidgets.QSpinBox()
        self.alert_el.setRange(0, 90)
        self.alert_el.setSuffix("°")
        self.alert_el.setValue(int(opts.get("sat_alert_el", 30)))
        self.alert_min = QtWidgets.QSpinBox()
        self.alert_min.setRange(1, 120)
        self.alert_min.setSuffix(" min before")
        self.alert_min.setValue(int(opts.get("sat_alert_min", 10)))
        arow = QtWidgets.QHBoxLayout()
        for w in (self.alert_chk, self.alert_el, self.alert_min):
            arow.addWidget(w)
        arow.addStretch(1)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("<b>Passes over home (next 2 days)</b>"))
        left.addWidget(self.table, 1)
        left.addLayout(arow)
        sky_box = QtWidgets.QVBoxLayout()
        sky_box.addWidget(self.sky, 1)
        sky_box.addWidget(self.sky_lbl)
        lower = QtWidgets.QHBoxLayout()
        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        sw = QtWidgets.QWidget()
        sw.setLayout(sky_box)
        lower.addWidget(lw, 3)
        lower.addWidget(sw, 2)
        lower_w = QtWidgets.QWidget()
        lower_w.setLayout(lower)

        self.split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        if HAVE_WEB:
            self.map = QWebEngineView()
            self.map.loadFinished.connect(lambda ok: setattr(self, "_map_ready", bool(ok)))
            self.split.addWidget(self.map)
        else:
            self.map = None
            note = QtWidgets.QLabel("Install PyQt6-WebEngine for the map (pip install PyQt6-WebEngine).")
            note.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.split.addWidget(note)
        self.split.addWidget(lower_w)
        sizes = opts.get("sat_split")
        self.split.setSizes(sizes if isinstance(sizes, list) and len(sizes) == 2 else [420, 360])

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.now_lbl)
        lay.addWidget(self.next_lbl)
        lay.addWidget(self.split, 1)

        self.sat_box.currentTextChanged.connect(lambda _n: self._load(force=False))
        self.refresh_btn.clicked.connect(lambda: self._load(force=True))
        self.tune_btn.clicked.connect(lambda: self._tune(doppler=self._in_pass() is not None))
        self.dopp_chk.toggled.connect(lambda on: self.opts.__setitem__("sat_doppler", on))
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._load(force=False)

    # ---- data -------------------------------------------------------------
    def home(self):
        return (float(self.opts.get("home_lat", DEFAULT_HOME[0])),
                float(self.opts.get("home_lon", DEFAULT_HOME[1])), 0.15)

    def _load(self, force):
        name = self.sat_box.currentText()
        norad, downs = S.SATS[name]
        self.down_box.clear()
        for label, hz in downs.items():
            self.down_box.addItem(label, hz)
        if self._loading:
            return
        self._loading = True
        self.now_lbl.setText(f"Loading {name} orbit…")
        if force:
            try:                                   # drop the cache entry so it's downloaded again
                path = S._cache_path()
                cache = json.loads(path.read_text(encoding="utf-8"))
                cache.pop(str(norad), None)
                path.write_text(json.dumps(cache), encoding="utf-8")
            except (OSError, ValueError):
                pass

        def work():
            tle = S.fetch_tle(norad)
            res = None
            if tle:
                sat = S.Sat(name, tle[0], tle[1])
                res = (sat, tle[2], S.passes(sat, self.home(), hours=48, min_el=0))
            self._result = ("ok", res) if res else ("fail", None)
        threading.Thread(target=work, daemon=True).start()

    def _apply_result(self):
        kind, res = self._result
        self._result = None
        self._loading = False
        if kind != "ok":
            self.now_lbl.setText("<span style='color:#e57373'>Couldn't get the orbit (no internet?). "
                                 "Press Update orbit to try again.</span>")
            return
        self.sat, self._tle_age, self.passes = res
        self._fill_table()
        self._build_map()

    def _fill_table(self):
        self.table.setRowCount(0)
        bold = QtGui.QFont()
        bold.setBold(True)
        for p in self.passes:
            r = self.table.rowCount()
            self.table.insertRow(r)
            good = p.max_el >= GOOD_EL
            cells = [time.strftime("%a %d", time.localtime(p.aos)), _hm(p.aos), f"{p.max_el:.0f}°",
                     _dur(p.duration), f"{S.compass(p.aos_az)} → {S.compass(p.los_az)}",
                     "yes" if p.visible else ""]
            for c, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if good:
                    it.setFont(bold)
                    it.setForeground(QtGui.QColor("#81c784"))
                elif p.max_el < 10:
                    it.setForeground(QtGui.QColor("#78909c"))
                if c == 5 and p.visible:
                    it.setToolTip("Sunlit station in a dark sky: you can see it with your eyes")
                self.table.setItem(r, c, it)
        if self.passes:
            self.table.selectRow(0)

    def _pass_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.passes):
            return
        p = self.passes[rows[0].row()]
        self.sky.show_pass(p)
        self.sky_lbl.setText(f"{time.strftime('%a %d %b', time.localtime(p.aos))}  {_hm(p.aos)}–{_hm(p.los)}"
                             f"  ·  max {p.max_el:.0f}° at {_hm(p.tmax)}"
                             f"  ·  {S.compass(p.aos_az)} → {S.compass(p.los_az)}")

    def _in_pass(self, t=None):
        t = time.time() if t is None else t
        for p in self.passes:
            if p.aos <= t <= p.los:
                return p
        return None

    # ---- map --------------------------------------------------------------
    def _build_map(self):
        if self.map is None:
            return
        lat, lon, _h = self.home()
        html = (MAP_HTML.replace("__LAT__", f"{lat:.4f}").replace("__LON__", f"{lon:.4f}")
                .replace("__NAME__", self.sat.name)
                .replace("__CARTO_KEY__", json.dumps(str(self.opts.get("carto_key", "")).strip())))
        self._map_ready = False
        self.map.setHtml(html, QtCore.QUrl("https://smashdr.local/sat"))

    def _update_map(self, t):
        if self.map is None or not self._map_ready or t - self._last_map < 5:
            return
        self._last_map = t
        lat, lon, alt, _v = self.sat.subpoint(t)
        trk = S.ground_track(self.sat, t)
        back = [[a, b] for a, b, f in trk if not f]
        fwd = [[a, b] for a, b, f in trk if f]
        r = S.footprint_radius_km(alt) * 1000
        self.map.page().runJavaScript(
            f"update({lat:.4f},{lon:.4f},{r:.0f},{json.dumps(back)},{json.dumps(fwd)})")

    # ---- every second ----------------------------------------------------------
    def _tick(self):
        if self._result is not None:
            self._apply_result()
        if self.sat is None:
            return
        t = time.time()
        try:
            lat, lon, alt, v = self.sat.subpoint(t)
            az, el, rng, rr = self.sat.look(t, self.home())
        except ValueError:
            return
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        age = ""
        if self._tle_age is not None and self._tle_age > 3 * 86400:
            age = f"  <span style='color:#ffb74d'>(orbit data {self._tle_age / 86400:.0f} days old)</span>"
        self.now_lbl.setText(f"<b>{self.sat.name}</b>  ·  {alt:,.0f} km up  ·  {v:,.0f} km/h  ·  over "
                             f"{abs(lat):.1f}°{ns} {abs(lon):.1f}°{ew}{age}")
        self.sky.show_now(az, el)
        p = self._in_pass(t)
        f0 = self.down_box.currentData() or 0
        if p is not None and el > 0:
            dop = S.doppler(f0, rr)
            self.next_lbl.setText(
                f"<span style='color:#66bb6a'><b>OVERHEAD NOW</b></span>  ·  az {az:.0f}° {S.compass(az)}  ·  "
                f"el {el:.0f}°  ·  {rng:,.0f} km  ·  Doppler {dop / 1000:+.1f} kHz → "
                f"<b>{(f0 + dop) / 1e6:.4f} MHz</b>  ·  ends in {_dur(p.los - t)}")
            if self.dopp_chk.isChecked() and t - self._last_tune >= 2:
                self._tune(doppler=True)
        else:
            nxt = next((q for q in self.passes if q.aos > t), None)
            good = next((q for q in self.passes if q.aos > t and q.max_el >= GOOD_EL), None)
            txt = ""
            if nxt:
                txt = (f"Next pass in <b>{_dur(nxt.aos - t)}</b>  ·  {_hm(nxt.aos)}, max {nxt.max_el:.0f}°, "
                       f"{S.compass(nxt.aos_az)} → {S.compass(nxt.los_az)}")
            if good and good is not nxt:
                txt += (f"  ·  <span style='color:#81c784'>next good one {_hm(good.aos)} "
                        f"({good.max_el:.0f}°)</span>")
            self.next_lbl.setText(txt)
            if self.passes and self.passes[-1].aos < t + 6 * 3600:
                self._load(force=False)             # keep 2 days ahead
        self._alerts(t)
        self._update_map(t)

    def _alerts(self, t):
        if not self.alert_chk.isChecked():
            return
        lead = self.alert_min.value() * 60
        for p in self.passes:
            key = (self.sat.name, round(p.aos))
            if p.max_el >= self.alert_el.value() and 0 <= p.aos - t <= lead and key not in self._alerted:
                self._alerted.add(key)
                vis = " · visible to the eye!" if p.visible else ""
                notify.send(self.opts.get("ntfy_topic", ""), f"{self.sat.name} pass in {int((p.aos - t) / 60)} min",
                            f"{_hm(p.aos)}–{_hm(p.los)} · max {p.max_el:.0f}° · "
                            f"{S.compass(p.aos_az)} → {S.compass(p.los_az)}{vis}",
                            priority="default", tags="satellite")

    def _tune(self, doppler):
        f0 = self.down_box.currentData()
        if not f0:
            return
        hz = f0
        if doppler and self.sat is not None:
            _az, el, _r, rr = self.sat.look(time.time(), self.home())
            if el > 0:
                hz = f0 + S.doppler(f0, rr)
        self._last_tune = time.time()
        self.tune_request.emit(int(round(hz / 100) * 100))

    def collect(self):
        self.opts.update(sat_split=self.split.sizes(), sat_alert=self.alert_chk.isChecked(),
                         sat_alert_el=self.alert_el.value(), sat_alert_min=self.alert_min.value(),
                         sat_doppler=self.dopp_chk.isChecked())

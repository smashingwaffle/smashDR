"""
aircraft.py - the Aircraft tab: ADS-B from a second RTL-SDR (1090 MHz).

AdsbReader (QThread) owns its own dongle, demodulates and tracks; once a
second it emits a snapshot. AircraftTab shows a sortable table and, when
PyQt6-WebEngine is installed, a Leaflet map (Esri dark grey canvas, or OSM).
"""

import json
import queue
import time

from PyQt6 import QtCore, QtGui, QtWidgets

from smashdr.core.capture import AsyncCapture
from smashdr.core.dsp import cu8_to_iq
from smashdr.decoders import adsb

try:                                    # must be imported before QApplication exists
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except ImportError:
    QWebEngineView = None
    HAVE_WEB = False

READ_BYTES = 262144                     # 65 ms at 2 MS/s
DEFAULT_HOME = (34.087, -118.183)       # roughly 90032; set yours on the tab
KM_PER_MI = 1.609344
EMERGENCY = {"7500": "HIJACK", "7600": "RADIO FAIL", "7700": "EMERGENCY"}


# --------------------------------------------------------------------------
class AdsbReader(QtCore.QThread):
    snapshot = QtCore.pyqtSignal(object, object)        # planes list, stats dict
    status = QtCore.pyqtSignal(str)

    def __init__(self, device_index: int, home, bias_tee=False):
        super().__init__()
        self.device_index = device_index
        self.tracker = adsb.Tracker(*home)
        self.bias_tee = bias_tee
        self._running = True

    def set_home(self, lat, lon):
        self.tracker.home = (lat, lon)

    def stop(self):
        self._running = False

    def run(self):
        from rtlsdr import RtlSdr
        try:
            sdr = RtlSdr(device_index=self.device_index)
        except Exception as e:
            self.status.emit(f"Could not open device #{self.device_index}: {e}")
            return
        cap = None
        try:
            sdr.sample_rate = adsb.RATE
            sdr.center_freq = adsb.FREQ
            gains = list(sdr.valid_gains_db)
            sdr.gain = max(gains) if gains else 49.6
            if self.bias_tee:
                try:
                    sdr.set_bias_tee(True)
                except Exception as e:
                    self.status.emit(f"Bias tee not available: {e}")
            q = queue.Queue(maxsize=60)
            cap = AsyncCapture(sdr, READ_BYTES, q)
            cap.start()
            demod = adsb.AdsbDemod()
            self.status.emit(f"Listening on 1090 MHz with device #{self.device_index}, "
                             f"gain {sdr.gain} dB")
            t_emit, n_last = time.time(), 0
            while self._running:
                try:
                    raw = q.get(timeout=0.5)
                except queue.Empty:
                    if cap.error:
                        self.status.emit(f"Capture stopped: {cap.error}")
                        break
                    continue
                now = time.time()
                for msg in demod.process(cu8_to_iq(raw)):
                    self.tracker.update(msg, now)
                if now - t_emit >= 1.0:
                    self.tracker.expire(now)
                    planes = []
                    for p in self.tracker.planes.values():
                        d = dict(p, trail=list(p["trail"]), age=now - p["last"],
                                 dist_km=self.tracker.distance(p))
                        planes.append(d)
                    stats = {"msgs_per_s": (demod.messages - n_last) / (now - t_emit),
                             "dropped": cap.dropped, "total": demod.messages}
                    n_last, t_emit = demod.messages, now
                    self.snapshot.emit(planes, stats)
        finally:
            if cap is not None:
                cap.stop()
                cap.join(timeout=2)
            try:
                sdr.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
MAP_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
html,body,#map{height:100%;margin:0;background:#1e1e1e}
.pl{font:11px Consolas,monospace;color:#e0e0e0;background:rgba(20,20,20,.75);
    border:none;box-shadow:none;padding:1px 4px}
.pl:before{display:none}
.osm-dark{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9)}
</style></head><body><div id="map"></div><script>
var map = L.map('map', {zoomControl:true}).setView([__LAT__, __LON__], 9);
var esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
var dark = L.layerGroup([
  L.tileLayer(esri + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16,
    attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors'}),
  L.tileLayer(esri + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16})
]).addTo(map);
var osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
  className: 'osm-dark', attribution: '&copy; OpenStreetMap contributors'});
L.control.layers({'Dark (Esri)': dark, 'OpenStreetMap': osm}, null,
  {position: 'topright'}).addTo(map);
var home = L.circleMarker([__LAT__, __LON__], {radius:5, color:'#ffd54f', weight:2,
  fillOpacity:0.6}).addTo(map).bindTooltip('home');
[50, 100, 150].forEach(function(mi){ L.circle([__LAT__, __LON__], {radius: mi*1609.344,
  color:'#555', weight:1, fill:false, dashArray:'4 6', interactive:false}).addTo(map); });
var planes = {};
function icon(p){
  var c = p.emergency ? '#ff5252' : (p.rotor ? '#ffb74d' : '#4fc3f7');
  var r = p.track == null ? 0 : p.track;
  var svg = p.rotor
    ? '<svg width="22" height="22" viewBox="-11 -11 22 22"><circle r="5" fill="'+c+'"/>' +
      '<line x1="-10" y1="-10" x2="10" y2="10" stroke="'+c+'" stroke-width="2"/>' +
      '<line x1="10" y1="-10" x2="-10" y2="10" stroke="'+c+'" stroke-width="2"/></svg>'
    : '<svg width="24" height="24" viewBox="-12 -12 24 24" style="transform:rotate('+r+'deg)">' +
      '<path d="M0,-11 L2,-3 L10,2 L10,4 L2,2 L1.5,8 L4,10 L4,11 L0,10 L-4,11 L-4,10 ' +
      'L-1.5,8 L-2,2 L-10,4 L-10,2 L-2,-3 Z" fill="'+c+'" stroke="#000" stroke-width="0.6"/></svg>';
  return L.divIcon({html: svg, className: '', iconSize: [24, 24], iconAnchor: [12, 12]});
}
function update(list){
  var seen = {};
  list.forEach(function(p){
    if (p.lat == null) return;
    seen[p.icao] = true;
    var label = (p.callsign || p.icao) + (p.alt != null ? ' ' + p.alt + 'ft' : '');
    var o = planes[p.icao];
    if (!o){
      o = planes[p.icao] = {
        m: L.marker([p.lat, p.lon], {icon: icon(p)}).addTo(map),
        t: L.polyline(p.trail, {color: '#4fc3f7', weight: 1.5, opacity: 0.5}).addTo(map)};
      o.m.bindTooltip(label, {permanent: true, direction: 'right', offset: [10, 0],
                              className: 'pl'});
    } else {
      o.m.setLatLng([p.lat, p.lon]); o.m.setIcon(icon(p));
      o.m.setTooltipContent(label); o.t.setLatLngs(p.trail);
    }
    o.t.setStyle({color: p.emergency ? '#ff5252' : (p.rotor ? '#ffb74d' : '#4fc3f7')});
  });
  Object.keys(planes).forEach(function(k){
    if (!seen[k]){ map.removeLayer(planes[k].m); map.removeLayer(planes[k].t); delete planes[k]; }
  });
}
function focusPlane(icao){
  var o = planes[icao];
  if (o){ map.setView(o.m.getLatLng(), Math.max(map.getZoom(), 11)); }
}
</script></body></html>"""

COLS = ["Callsign", "ICAO", "Alt ft", "Speed kt", "Track", "V/S fpm", "Dist mi",
        "Squawk", "Msgs", "Seen"]


class _Num(QtWidgets.QTableWidgetItem):
    """Sorts by number, shows text."""

    def __init__(self, text, value):
        super().__init__(text)
        self.value = value if value is not None else float("inf")
        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                              QtCore.Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _Num):
            return self.value < other.value
        return super().__lt__(other)


class AircraftTab(QtWidgets.QWidget):
    def __init__(self, opts: dict, devices: list, busy_index: int, parent=None):
        super().__init__(parent)
        self.opts = opts
        self.busy_index = busy_index            # the scanner's dongle
        self.reader = None
        self._map_ready = False
        self._last = []

        self.dev_box = QtWidgets.QComboBox()
        for i, name in enumerate(devices or ["#0", "#1"]):
            label = name + ("   (scanner)" if i == busy_index else "")
            self.dev_box.addItem(label, i)
        want = int(opts.get("adsb_device", 1 if busy_index == 0 else 0))
        self.dev_box.setCurrentIndex(max(0, self.dev_box.findData(want)))
        self.bias_chk = QtWidgets.QCheckBox("Bias tee")
        self.bias_chk.setToolTip("Power an LNA / active ADS-B antenna through the coax")
        self.bias_chk.setChecked(bool(opts.get("adsb_bias_tee", False)))
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setCheckable(True)
        self.start_btn.toggled.connect(self._toggle)
        self.lat_box = QtWidgets.QDoubleSpinBox()
        self.lon_box = QtWidgets.QDoubleSpinBox()
        for box, rng, val in ((self.lat_box, 90, opts.get("home_lat", DEFAULT_HOME[0])),
                              (self.lon_box, 180, opts.get("home_lon", DEFAULT_HOME[1]))):
            box.setRange(-rng, rng)
            box.setDecimals(4)
            box.setValue(float(val))
            box.valueChanged.connect(self._home_changed)
        self.stat_lbl = QtWidgets.QLabel("Stopped")

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Device"))
        bar.addWidget(self.dev_box)
        bar.addWidget(self.bias_chk)
        bar.addWidget(self.start_btn)
        bar.addSpacing(20)
        bar.addWidget(QtWidgets.QLabel("Home lat"))
        bar.addWidget(self.lat_box)
        bar.addWidget(QtWidgets.QLabel("lon"))
        bar.addWidget(self.lon_box)
        bar.addSpacing(20)
        bar.addWidget(self.stat_lbl, 1)

        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COLS.index("Dist mi"), QtCore.Qt.SortOrder.AscendingOrder)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellClicked.connect(self._row_clicked)
        f = self.table.font()
        f.setPointSize(f.pointSize() + 1)
        self.table.setFont(f)

        if HAVE_WEB:
            self.map = QWebEngineView()
            lat, lon = self.lat_box.value(), self.lon_box.value()
            html = MAP_HTML.replace("__LAT__", f"{lat:.5f}").replace("__LON__", f"{lon:.5f}")
            self.map.loadFinished.connect(self._map_loaded)
            self.map.setHtml(html, QtCore.QUrl("https://smashdr.local/"))
        else:
            self.map = QtWidgets.QLabel(
                "Map needs PyQt6-WebEngine.\n\nIn the VSCode terminal run:\n"
                "pip install PyQt6-WebEngine\n\nthen restart (F5). The table works without it.")
            self.map.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.map.setStyleSheet("color: gray;")

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(self.table)
        split.addWidget(self.map)
        split.setSizes([900, 2400])

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        if opts.get("adsb_autostart"):
            QtCore.QTimer.singleShot(500, lambda: self.start_btn.setChecked(True))

    # ------------------------------------------------------------------
    def _toggle(self, on):
        if on:
            idx = self.dev_box.currentData()
            if idx == self.busy_index:
                self.stat_lbl.setText("That dongle is the scanner's. Pick the other one.")
                self.start_btn.setChecked(False)
                return
            self.opts["adsb_device"] = idx
            self.opts["adsb_bias_tee"] = self.bias_chk.isChecked()
            self.reader = AdsbReader(idx, (self.lat_box.value(), self.lon_box.value()),
                                     self.bias_chk.isChecked())
            self.reader.snapshot.connect(self._on_snapshot)
            self.reader.status.connect(self.stat_lbl.setText)
            self.reader.finished.connect(self._reader_done)
            self.reader.start()
            self.start_btn.setText("Stop")
            self.dev_box.setEnabled(False)
            self.bias_chk.setEnabled(False)
        else:
            self.stop()
        self.opts["adsb_autostart"] = on

    def _reader_done(self):
        self.reader = None
        self.dev_box.setEnabled(True)
        self.bias_chk.setEnabled(True)
        self.start_btn.blockSignals(True)
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        self.start_btn.blockSignals(False)

    def stop(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(3000)

    def _home_changed(self, _v):
        lat, lon = self.lat_box.value(), self.lon_box.value()
        self.opts["home_lat"], self.opts["home_lon"] = lat, lon
        if self.reader is not None:
            self.reader.set_home(lat, lon)

    def _map_loaded(self, ok):
        self._map_ready = bool(ok)
        if not ok:
            self.stat_lbl.setText("Map failed to load (no internet?). The table still works.")

    def _row_clicked(self, row, _col):
        it = self.table.item(row, COLS.index("ICAO"))
        if it and HAVE_WEB and self._map_ready:
            self.map.page().runJavaScript(f"focusPlane({json.dumps(it.text())})")

    # ------------------------------------------------------------------
    def _on_snapshot(self, planes, stats):
        with_pos = sum(1 for p in planes if p["lat"] is not None)
        self.stat_lbl.setText(f"{len(planes)} aircraft ({with_pos} with position)  ·  "
                              f"{stats['msgs_per_s']:.0f} msg/s  ·  {stats['total']} total"
                              + (f"  ·  {stats['dropped']} blocks dropped" if stats["dropped"] else ""))
        self._fill_table(planes)
        if HAVE_WEB and self._map_ready:
            js = [{"icao": p["icao"], "callsign": p["callsign"], "lat": p["lat"], "lon": p["lon"],
                   "alt": p["alt"], "track": p["track"], "rotor": p["rotor"],
                   "emergency": p["squawk"] in EMERGENCY, "trail": p["trail"]}
                  for p in planes]
            self.map.page().runJavaScript(f"update({json.dumps(js)})")

    def _fill_table(self, planes):
        sel = None
        r = self.table.currentRow()
        if r >= 0 and self.table.item(r, 1):
            sel = self.table.item(r, 1).text()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(planes))
        for row, p in enumerate(planes):
            dist_mi = p["dist_km"] / KM_PER_MI if p["dist_km"] is not None else None
            sq = p["squawk"] or ""
            cells = [
                QtWidgets.QTableWidgetItem(p["callsign"] or "—"),
                QtWidgets.QTableWidgetItem(p["icao"]),
                _Num(f"{p['alt']:,}" if p["alt"] is not None else "", p["alt"]),
                _Num(f"{p['gs']:.0f}" if p["gs"] is not None else "", p["gs"]),
                _Num(f"{p['track']:.0f}°" if p["track"] is not None else "", p["track"]),
                _Num(f"{p['vrate']:+,}" if p["vrate"] is not None else "", p["vrate"]),
                _Num(f"{dist_mi:.1f}" if dist_mi is not None else "", dist_mi),
                QtWidgets.QTableWidgetItem(sq + (f" {EMERGENCY[sq]}" if sq in EMERGENCY else "")),
                _Num(str(p["msgs"]), p["msgs"]),
                _Num(f"{p['age']:.0f}s", p["age"]),
            ]
            color = None
            if sq in EMERGENCY:
                color = QtGui.QColor("#ff5252")
            elif p["rotor"]:
                color = QtGui.QColor("#ffb74d")
            for c, it in enumerate(cells):
                if color is not None:
                    it.setForeground(color)
                if p["rotor"] and c == 0:
                    it.setToolTip("Rotorcraft (helicopter)")
                self.table.setItem(row, c, it)
        self.table.setSortingEnabled(True)
        if sel:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 1).text() == sel:
                    self.table.selectRow(row)
                    break

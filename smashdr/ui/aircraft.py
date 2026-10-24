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

from smashdr.core import airlines, airports, flightaware
from smashdr.core.capture import AsyncCapture
from smashdr.core.dsp import cu8_to_iq
from smashdr.decoders import adsb

from PyQt6 import QtNetwork

try:                                    # must be imported before QApplication exists
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except ImportError:
    QWebEngineView = QWebEnginePage = None
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
.pl{font:600 11px Segoe UI,Arial,sans-serif;color:#fff;background:rgba(18,18,18,.82);
    border:1px solid rgba(255,255,255,.12);border-radius:10px;box-shadow:none;padding:1px 6px}
.pl:before{display:none}
.pl.sel{background:#ffd54f;color:#111;border-color:#ffd54f}
.osm-dark{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9)}
.leaflet-container{font-family:Segoe UI,Arial,sans-serif}
</style></head><body><div id="map"></div><script>
var VIEW = __VIEW__;
var map = L.map('map', {zoomControl:true}).setView(VIEW ? [VIEW[0], VIEW[1]] : [__LAT__, __LON__], VIEW ? VIEW[2] : 9);
var esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
var cartoKey = __CARTO_KEY__;
var dark = L.layerGroup([
  L.tileLayer(esri + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16,
    attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors'}),
  L.tileLayer(esri + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16})
]);
var osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
  className: 'osm-dark', attribution: '&copy; OpenStreetMap contributors'});
var layers = {'Dark (Esri)': dark, 'OpenStreetMap': osm};
if (cartoKey) {
  var carto = L.tileLayer('https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key='
      + encodeURIComponent(cartoKey), {maxNativeZoom: 18, maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO'});
  layers = Object.assign({'Dark (CARTO)': carto}, layers);
}
var want = __LAYER__;
(layers[want] || (cartoKey ? layers['Dark (CARTO)'] : dark)).addTo(map);
L.control.layers(layers, null, {position: 'topright'}).addTo(map);
map.on('baselayerchange', function(e){ console.log('LAYER:' + e.name); });
map.on('moveend', function(){ var c = map.getCenter();
  console.log('VIEW:' + c.lat.toFixed(5) + ',' + c.lng.toFixed(5) + ',' + map.getZoom()); });
L.circleMarker([__LAT__, __LON__], {radius:5, color:'#ffd54f', weight:2,
  fillOpacity:0.6}).addTo(map).bindTooltip('home');
[25, 50, 100, 150].forEach(function(mi){ L.circle([__LAT__, __LON__], {radius: mi*1609.344,
  color:'#444', weight:1, fill:false, dashArray:'4 6', interactive:false}).addTo(map); });

var planes = {}, selected = null;
var ALT = __ALT_STOPS__;
function altColor(a){
  if (a == null) return '#9e9e9e';
  a = Math.max(0, Math.min(a, ALT[ALT.length-1][0]));
  for (var i = 0; i < ALT.length-1; i++){
    var a0 = ALT[i][0], a1 = ALT[i+1][0];
    if (a <= a1){
      var t = (a-a0)/(a1-a0), c0 = ALT[i][1], c1 = ALT[i+1][1];
      return 'rgb(' + [0,1,2].map(function(k){ return Math.round(c0[k]+(c1[k]-c0[k])*t); }).join(',') + ')';
    }
  }
  return 'rgb(' + ALT[ALT.length-1][1].join(',') + ')';
}
function colorOf(p){ return p.emergency ? '#ff5252' : altColor(p.alt); }
function icon(p){
  var sel = (p.icao === selected), c = colorOf(p), r = p.track == null ? 0 : p.track;
  var s = sel ? 34 : 26, h = s/2;
  var glow = sel ? 'filter:drop-shadow(0 0 4px #ffd54f) drop-shadow(0 0 2px #fff);' : '';
  var stroke = sel ? '#ffd54f' : '#111';
  var svg = p.rotor
    ? '<svg width="'+s+'" height="'+s+'" viewBox="-12 -12 24 24" style="'+glow+'">' +
      '<g style="transform:rotate('+r+'deg)"><ellipse rx="3.2" ry="5.5" fill="'+c+'" stroke="'+stroke+'" stroke-width="0.8"/>' +
      '<rect x="-0.6" y="4" width="1.2" height="7" fill="'+c+'"/>' +
      '<line x1="-11" y1="-11" x2="11" y2="11" stroke="'+c+'" stroke-width="1.6"/>' +
      '<line x1="11" y1="-11" x2="-11" y2="11" stroke="'+c+'" stroke-width="1.6"/></g></svg>'
    : '<svg width="'+s+'" height="'+s+'" viewBox="-12 -12 24 24" style="'+glow+'transform:rotate('+r+'deg)">' +
      '<path d="M0,-11 C0.9,-11 1.4,-9.5 1.4,-8 L1.4,-3 L10.5,2.2 L10.5,4.2 L1.4,1.8 L1.1,7.4 ' +
      'L4,9.6 L4,11 L0,10 L-4,11 L-4,9.6 L-1.1,7.4 L-1.4,1.8 L-10.5,4.2 L-10.5,2.2 L-1.4,-3 ' +
      'L-1.4,-8 C-1.4,-9.5 -0.9,-11 0,-11 Z" fill="'+c+'" stroke="'+stroke+'" stroke-width="0.7"/></svg>';
  return L.divIcon({html: svg, className: '', iconSize: [s, s], iconAnchor: [h, h]});
}
function label(p){ return (p.flight || p.icao) + (p.alt != null ? ' · ' + Math.round(p.alt/100)*100 + 'ft' : ''); }
function restyle(icao){
  var o = planes[icao]; if (!o) return;
  var sel = icao === selected;
  o.m.setIcon(icon(o.p)); o.m.setZIndexOffset(sel ? 1000 : 0);
  o.m.getTooltip().getElement && o.m.getTooltip().getElement() &&
    o.m.getTooltip().getElement().classList.toggle('sel', sel);
  o.t.setStyle({color: colorOf(o.p), weight: sel ? 3 : 1.5, opacity: sel ? 0.95 : 0.45});
}
function update(list){
  var seen = {};
  list.forEach(function(p){
    if (p.lat == null) return;
    seen[p.icao] = true;
    var o = planes[p.icao];
    if (!o){
      o = planes[p.icao] = {p: p,
        m: L.marker([p.lat, p.lon], {icon: icon(p)}).addTo(map),
        t: L.polyline(p.trail, {weight: 1.5, opacity: 0.45}).addTo(map)};
      o.m.bindTooltip(label(p), {permanent: true, direction: 'right', offset: [12, 0], className: 'pl'});
      (function(icao){ o.m.on('click', function(e){
        L.DomEvent.stopPropagation(e); selectPlane(icao, false); console.log('SELECT:' + icao); }); })(p.icao);
    } else {
      o.p = p; o.m.setLatLng([p.lat, p.lon]); o.m.setTooltipContent(label(p)); o.t.setLatLngs(p.trail);
    }
    restyle(p.icao);
  });
  Object.keys(planes).forEach(function(k){
    if (!seen[k]){ map.removeLayer(planes[k].m); map.removeLayer(planes[k].t); delete planes[k]; }
  });
}
function selectPlane(icao, center){
  var old = selected; selected = icao || null;
  if (old && old !== selected) restyle(old);
  if (selected){ restyle(selected);
    var o = planes[selected];
    if (o && center){ map.panTo(o.m.getLatLng()); }
  }
}
map.on('click', function(){ if (selected){ selectPlane(null); console.log('SELECT:'); } });
</script></body></html>"""

COLS = ["Flight", "Airline", "LA airport", "Alt ft", "Speed kt", "Hdg", "V/S fpm", "Dist mi",
        "Squawk", "ICAO", "Seen"]
C_ICAO = COLS.index("ICAO")
ARROWS = "↑↗→↘↓↙←↖"


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


if HAVE_WEB:
    class _MapPage(QWebEnginePage):
        """Map -> Python: the page logs 'SELECT:<icao>' when a plane is clicked."""
        selected = QtCore.pyqtSignal(str)
        pref = QtCore.pyqtSignal(str, str)          # "layer"/"view", value

        def javaScriptConsoleMessage(self, level, message, line, source):
            if message.startswith("SELECT:"):
                self.selected.emit(message[7:])
            elif message.startswith("LAYER:"):
                self.pref.emit("layer", message[6:])
            elif message.startswith("VIEW:"):
                self.pref.emit("view", message[5:])


def _field(data, key):
    """adsbdb: {"response": {key: {...}}}, or {"response": "unknown ..."} -> dict or None."""
    try:
        resp = json.loads(data).get("response") if data else None
    except ValueError:
        return None
    v = resp.get(key) if isinstance(resp, dict) else None
    return v if isinstance(v, dict) else None


def flight_name(callsign: str):
    """("AAL1234" ->) 'American 1234', code 'AAL'; else callsign, None."""
    code, name, num = airlines.split(callsign)
    if code and name:
        return f"{name} {num}", code
    return callsign or "", code


class InfoCard(QtWidgets.QFrame):
    """Selected aircraft: photo, flight, route, type/registration, live numbers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet("""
            QFrame#card { background: #232629; border: 1px solid #3a3f44; border-radius: 10px; }
            QLabel { background: transparent; }
            QLabel#flight { font-size: 20pt; font-weight: 600; color: #ffffff; }
            QLabel#route { font-size: 13pt; color: #e0e0e0; }
            QLabel#sub { font-size: 10pt; color: #9aa0a6; }
            QLabel#live { font-size: 12pt; color: #ffffff; }
            QLabel#badge { border-radius: 4px; padding: 2px 8px; font-weight: 600; color: #fff; }
        """)
        self.photo = QtWidgets.QLabel()
        self.photo.setFixedSize(220, 146)
        self.photo.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.photo.setStyleSheet("background:#1b1d1f; border-radius:6px; color:#555;")
        self.badge = QtWidgets.QLabel()
        self.badge.setObjectName("badge")
        self.flight = QtWidgets.QLabel()
        self.flight.setObjectName("flight")
        self.route = QtWidgets.QLabel()
        self.route.setObjectName("route")
        self.route.setWordWrap(True)
        self.route.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.type_lbl = QtWidgets.QLabel()
        self.type_lbl.setObjectName("sub")
        self.type_lbl.setWordWrap(True)
        self.live = QtWidgets.QLabel()
        self.live.setObjectName("live")
        self.live.setTextFormat(QtCore.Qt.TextFormat.RichText)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.badge)
        top.addWidget(self.flight)
        top.addStretch(1)
        text = QtWidgets.QVBoxLayout()
        text.addLayout(top)
        text.addWidget(self.route)
        text.addWidget(self.type_lbl)
        text.addStretch(1)
        text.addWidget(self.live)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.addWidget(self.photo)
        lay.addLayout(text, 1)
        self.clear()

    def clear(self):
        self.badge.hide()
        self.flight.setText("No aircraft selected")
        self.route.setText("Click a plane on the map or a row in the list.")
        self.type_lbl.setText("")
        self.live.setText("")
        self.photo.setPixmap(QtGui.QPixmap())
        self.photo.setText("")


class AircraftTab(QtWidgets.QWidget):
    def __init__(self, opts: dict, devices: list, busy_index: int, parent=None):
        super().__init__(parent)
        self.opts = opts
        self.busy_index = busy_index            # the scanner's dongle
        self.reader = None
        self._map_ready = False
        self._planes = {}                       # icao -> latest snapshot dict
        self._selected = None
        self._info = {}                         # icao -> aircraft info (adsbdb), or None
        self._routes = {}                       # callsign -> adsbdb route dict / "unknown" / None
        self._fa = {}                           # callsign -> (day, AeroAPI summary or "none"/None)
        self._fa_times = []                     # call times (10/min limit)
        self._net = QtNetwork.QNetworkAccessManager(self)

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

        self.card = InfoCard()

        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSortingEnabled(True)
        sort = opts.get("adsb_sort")
        try:
            col, order = int(sort[0]), QtCore.Qt.SortOrder(int(sort[1]))
            if not 0 <= col < len(COLS):
                raise ValueError
        except (TypeError, ValueError, IndexError):
            col, order = COLS.index("Dist mi"), QtCore.Qt.SortOrder.AscendingOrder
        self.table.sortByColumn(col, order)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COLS.index("Airline"), QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        self.table.setStyleSheet("""
            QTableWidget { background: #1e2023; alternate-background-color: #24272b;
                           border: none; font-size: 11pt; }
            QTableWidget::item { padding: 2px 8px; }
            QTableWidget::item:selected { background: #4a3f12; color: #ffffff; }
            QHeaderView::section { background: #2b2f33; color: #9aa0a6; border: none;
                                   padding: 6px 8px; font-weight: 600; }
        """)
        self.table.itemSelectionChanged.connect(self._table_selection)

        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.card)
        ll.addWidget(self.table, 1)

        if HAVE_WEB:
            self.map = QWebEngineView()
            self._page = _MapPage(self.map)
            self._page.selected.connect(self._map_selected)
            self._page.pref.connect(self._map_pref)
            self.map.setPage(self._page)
            self.map.loadFinished.connect(self._map_loaded)
            self.reload_map()
        else:
            self.map = QtWidgets.QLabel(
                "Map needs PyQt6-WebEngine.\n\nIn the VSCode terminal run:\n"
                "python -m pip install --upgrade PyQt6 PyQt6-WebEngine\n\n"
                "then restart (F5). The list works without it.")
            self.map.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.map.setStyleSheet("color: gray;")

        split = self.split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.map)
        sizes = opts.get("adsb_split")
        split.setSizes(sizes if isinstance(sizes, list) and len(sizes) == 2 else [1100, 2200])

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

    def reload_map(self):
        """(Re)build the map page, e.g. after the CARTO key changed in Settings."""
        if not HAVE_WEB:
            return
        self._map_ready = False
        lat, lon = self.lat_box.value(), self.lon_box.value()
        stops = json.dumps([[a, list(c)] for a, c in airlines.ALT_STOPS])
        view = self.opts.get("adsb_map_view")
        html = (MAP_HTML.replace("__LAT__", f"{lat:.5f}").replace("__LON__", f"{lon:.5f}")
                .replace("__ALT_STOPS__", stops)
                .replace("__VIEW__", json.dumps(view if isinstance(view, list) and len(view) == 3 else None))
                .replace("__LAYER__", json.dumps(str(self.opts.get("adsb_map_layer", ""))))
                .replace("__CARTO_KEY__", json.dumps(str(self.opts.get("carto_key", "")).strip())))
        self.map.setHtml(html, QtCore.QUrl("https://smashdr.local/"))

    def _map_pref(self, kind, value):
        if kind == "layer":
            self.opts["adsb_map_layer"] = value
        elif kind == "view":
            try:
                lat, lon, z = value.split(",")
                self.opts["adsb_map_view"] = [float(lat), float(lon), int(z)]
            except ValueError:
                pass

    def collect(self):
        """Put the tab's layout state into opts (called before settings are saved)."""
        self.opts["adsb_split"] = self.split.sizes()
        hh = self.table.horizontalHeader()
        self.opts["adsb_sort"] = [hh.sortIndicatorSection(), hh.sortIndicatorOrder().value]

    def _map_loaded(self, ok):
        self._map_ready = bool(ok)
        if not ok:
            self.stat_lbl.setText("Map failed to load (no internet?). The list still works.")

    def _js(self, code):
        if HAVE_WEB and self._map_ready:
            self.map.page().runJavaScript(code)

    # ---- selection: map <-> list <-> card --------------------------------
    def _table_selection(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        it = self.table.item(rows[0].row(), C_ICAO)
        if it is None or it.text() == self._selected:
            return
        self._select(it.text(), center=True)

    def _map_selected(self, icao):
        if not icao:
            self._selected = None
            self.table.clearSelection()
            self.card.clear()
            return
        self._select(icao, center=False)
        self._select_row(icao)

    def _select(self, icao, center):
        self._selected = icao
        self._js(f"selectPlane({json.dumps(icao)}, {'true' if center else 'false'})")
        self._update_card(lookup=True)

    def _select_row(self, icao):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            it = self.table.item(row, C_ICAO)
            if it and it.text() == icao:
                self.table.selectRow(row)
                self.table.scrollToItem(it, QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible)
                break
        self.table.blockSignals(False)

    # ---- info card + online lookups (adsbdb.com, free, no key) -----------
    def _update_card(self, lookup=False):
        p = self._planes.get(self._selected)
        if p is None:
            if self._selected is None:
                self.card.clear()
            return
        name, code = flight_name(p["callsign"])
        c = self.card
        if code:
            c.badge.setText(code)
            c.badge.setStyleSheet(f"background:{airlines.badge_color(code)};")
            c.badge.show()
        else:
            c.badge.hide()
        c.flight.setText(name or p["icao"])
        c.route.setText(self._route_html(p, lookup))
        info = self._info.get(p["icao"])
        if info:
            c.type_lbl.setText(info["text"])
        elif p["rotor"]:
            c.type_lbl.setText("Rotorcraft · ICAO " + p["icao"])
        else:
            c.type_lbl.setText("ICAO " + p["icao"])
        alt = p["alt"]
        dist = p["dist_km"] / KM_PER_MI if p["dist_km"] is not None else None
        parts = []
        if alt is not None:
            parts.append(f"<span style='color:{airlines.alt_color(alt)}'>{alt:,} ft</span>")
        if p["gs"] is not None:
            parts.append(f"{p['gs']:.0f} kt")
        if p["track"] is not None:
            parts.append(f"{ARROWS[int((p['track'] + 22.5) % 360 // 45)]} {p['track']:.0f}°")
        if p["vrate"]:
            parts.append(("▲ " if p["vrate"] > 0 else "▼ ") + f"{abs(p['vrate']):,} fpm")
        if dist is not None:
            parts.append(f"{dist:.1f} mi away")
        if p["squawk"]:
            sq = p["squawk"]
            parts.append(f"<span style='color:#ff5252'>{sq} {EMERGENCY[sq]}</span>"
                         if sq in EMERGENCY else f"squawk {sq}")
        c.live.setText("  ·  ".join(parts))
        if lookup:
            self._lookup(p)

    def _route_html(self, p, lookup):
        """Route line: what the plane is doing (inferred) checked against the database."""
        seen = airports.infer(p)
        fa = self._fa.get(p["callsign"], (None, None))[1] if p["callsign"] else None
        if isinstance(fa, dict):
            return self._fa_html(fa, seen)
        if fa is None and p["callsign"] in self._fa:
            return "<span style='color:#9aa0a6'>FlightAware: looking up…</span>"
        db = self._routes.get(p["callsign"]) if p["callsign"] else None
        parts = []
        if seen:
            kind, code, name = seen
            word = "Arriving" if kind == "arr" else "Departed"
            parts.append(f"<b>{word} {code}</b> {name} "
                         "<span style='color:#9aa0a6; font-size:10pt'>(from its flight path)</span>")
        if isinstance(db, dict):
            (o, oc), (d, dc) = db["orig"], db["dest"]
            text = f"{o} {oc}  →  {d} {dc}"
            if seen and seen[1] not in (o, d):
                parts.append(f"<span style='color:#80868b; font-size:10pt'>database: {text} · "
                             "doesn't match, probably an old or different leg</span>")
            elif seen:
                parts.append(f"<span style='color:#9aa0a6; font-size:10pt'>{text} ✓</span>")
            else:
                parts.append(f"{text} <span style='color:#80868b; font-size:10pt'>"
                             "(usual route for this flight number, not confirmed)</span>")
        elif db == "unknown" and not seen:
            parts.append("<span style='color:#9aa0a6'>Route unknown</span>")
        elif db is None and p["callsign"] and lookup:
            parts.append("<span style='color:#9aa0a6'>Route: looking up…</span>")
        if not parts:
            parts.append(p["callsign"] or "No callsign yet")
        return "<br>".join(parts)

    @staticmethod
    def _fa_html(fa, seen):
        def late(mins):
            if mins is None or abs(mins) < 3:
                return "<span style='color:#81c784'>on time</span>"
            if mins > 0:
                return f"<span style='color:#ffb74d'>{mins} min late</span>"
            return f"<span style='color:#81c784'>{-mins} min early</span>"
        (o, oc), (d, dc) = fa["orig"], fa["dest"]
        out = [f"<b>{o}</b> {oc}  →  <b>{d}</b> {dc}"]
        small = [fa["status"]] if fa["status"] else []
        small.append(f"dep {fa['dep']} ({late(fa['dep_delay'])})")
        small.append(f"arr {fa['arr']} ({late(fa['arr_delay'])})")
        if fa["diverted"]:
            small.append("<span style='color:#ff5252'>DIVERTED</span>")
        out.append("<span style='font-size:10pt; color:#c8ccd0'>" + "  ·  ".join(small) +
                   "</span>")
        if seen and seen[1] not in (o, d):
            word = "arriving" if seen[0] == "arr" else "departed"
            out.append(f"<span style='font-size:10pt; color:#ffb74d'>flight path says {word} "
                       f"{seen[1]}: diversion or data mix-up?</span>")
        out.append("<span style='font-size:9pt; color:#80868b'>FlightAware</span>")
        return "<br>".join(out)

    def _get(self, url, handler):
        req = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
        req.setRawHeader(b"User-Agent", b"smashDR (personal ADS-B viewer)")
        reply = self._net.get(req)
        reply.finished.connect(lambda r=reply: self._got(r, handler))

    def _got(self, reply, handler):
        try:
            ok = reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else b""
            handler(data if ok else None)
        except Exception as e:                       # bad JSON etc.: just no extra info
            print("[adsbdb]", e)
        finally:
            reply.deleteLater()

    def _lookup(self, p):
        icao, cs = p["icao"], p["callsign"]
        if icao not in self._info:
            self._info[icao] = None
            self._get(f"https://api.adsbdb.com/v0/aircraft/{icao}",
                      lambda d, i=icao: self._got_aircraft(i, d))
        elif self._info[icao] and self._info[icao].get("pixmap") is not None:
            self.card.photo.setPixmap(self._info[icao]["pixmap"])
        if cs and self._fa_lookup(cs):
            pass                                  # FlightAware answers the route
        elif cs and cs not in self._routes:
            self._routes[cs] = None
            self._get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                      lambda d, c=cs: self._got_route(c, d))
        if self._info.get(icao) is None:
            self.card.photo.setPixmap(QtGui.QPixmap())
            self.card.photo.setText("no photo")

    # ---- FlightAware AeroAPI (optional key, monthly cap) -------------------
    def fa_usage(self):
        month = time.strftime("%Y-%m")
        if self.opts.get("aeroapi_month") != month:
            self.opts["aeroapi_month"], self.opts["aeroapi_used"] = month, 0
        return int(self.opts.get("aeroapi_used", 0)), int(self.opts.get("aeroapi_cap", 900))

    def _fa_lookup(self, cs):
        """True if FlightAware has / will have the route for this callsign."""
        key = str(self.opts.get("aeroapi_key", "")).strip()
        if not key:
            return False
        day = time.strftime("%Y-%m-%d")
        cached = self._fa.get(cs)
        if cached and cached[0] == day:
            return cached[1] != "none"
        used, cap = self.fa_usage()
        now = time.time()
        self._fa_times = [t for t in self._fa_times if now - t < 60]
        if used >= cap or len(self._fa_times) >= 10:
            return False
        self._fa_times.append(now)
        self.opts["aeroapi_used"] = used + 1
        self._fa[cs] = (day, None)
        req = QtNetwork.QNetworkRequest(QtCore.QUrl(flightaware.flight_url(cs)))
        req.setRawHeader(b"x-apikey", key.encode())
        req.setRawHeader(b"Accept", b"application/json")
        reply = self._net.get(req)
        reply.finished.connect(lambda r=reply, c=cs, d=day: self._fa_done(r, c, d))
        return True

    def _fa_done(self, reply, cs, day):
        try:
            code = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            data = bytes(reply.readAll())
            if code == 200:
                sm = flightaware.summary(data)
                self._fa[cs] = (day, sm if sm else "none")
            else:
                self._fa[cs] = (day, "none")
                msg = {401: "FlightAware key rejected (check Settings)",
                       429: "FlightAware rate limit, try again in a minute"}.get(
                    code, f"FlightAware lookup failed ({code or reply.errorString()})")
                self.stat_lbl.setText(msg)
                print("[aeroapi]", cs, code, data[:200])
            if self._fa[cs][1] == "none" and cs not in self._routes:
                self._routes[cs] = None                  # fall back to adsbdb
                self._get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                          lambda d, c=cs: self._got_route(c, d))
        finally:
            reply.deleteLater()
        p = self._planes.get(self._selected)
        if p and p["callsign"] == cs:
            self._update_card()

    def _got_aircraft(self, icao, data):
        a = _field(data, "aircraft")
        if not isinstance(a, dict):
            self._info[icao] = {"text": f"ICAO {icao} · not in the aircraft database",
                                "pixmap": None}
        else:
            bits = [a.get("manufacturer", ""), a.get("type", "")]
            line1 = " ".join(b for b in bits if b).strip() or a.get("icao_type", "")
            line2 = " · ".join(x for x in (a.get("registration"), a.get("registered_owner"),
                                            a.get("registered_owner_country_name")) if x)
            self._info[icao] = {"text": f"{line1}\n{line2}".strip(), "pixmap": None}
            url = a.get("url_photo_thumbnail") or a.get("url_photo")
            if url:
                self._get(url, lambda d, i=icao: self._got_photo(i, d))
        if icao == self._selected:
            self._update_card()

    def _got_photo(self, icao, data):
        if not data:
            return
        pm = QtGui.QPixmap()
        if pm.loadFromData(data):
            pm = pm.scaled(220, 146, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                           QtCore.Qt.TransformationMode.SmoothTransformation)
            if self._info.get(icao):
                self._info[icao]["pixmap"] = pm
            if icao == self._selected:
                self.card.photo.setPixmap(pm)

    def _got_route(self, cs, data):
        r = _field(data, "flightroute")
        if isinstance(r, dict) and r.get("origin") and r.get("destination"):
            def ap(x):
                x = x if isinstance(x, dict) else {}
                return (x.get("iata_code") or x.get("icao_code") or "?",
                        x.get("municipality") or x.get("name") or "")
            self._routes[cs] = {"orig": ap(r["origin"]), "dest": ap(r["destination"])}
        else:
            self._routes[cs] = "unknown"
        p = self._planes.get(self._selected)
        if p and p["callsign"] == cs:
            self._update_card()

    # ------------------------------------------------------------------
    def _on_snapshot(self, planes, stats):
        self._planes = {p["icao"]: p for p in planes}
        with_pos = sum(1 for p in planes if p["lat"] is not None)
        self.stat_lbl.setText(f"{len(planes)} aircraft ({with_pos} with position)  ·  "
                              f"{stats['msgs_per_s']:.0f} msg/s  ·  {stats['total']} total"
                              + (f"  ·  {stats['dropped']} blocks dropped" if stats["dropped"] else ""))
        self._fill_table(planes)
        if self._selected in self._planes:
            self._update_card()
        if HAVE_WEB and self._map_ready:
            js = []
            for p in planes:
                name, _code = flight_name(p["callsign"])
                js.append({"icao": p["icao"], "flight": p["callsign"] or "", "lat": p["lat"],
                           "lon": p["lon"], "alt": p["alt"], "track": p["track"],
                           "rotor": p["rotor"], "emergency": p["squawk"] in EMERGENCY,
                           "trail": p["trail"]})
            self._js(f"update({json.dumps(js)})")

    def _fill_table(self, planes):
        bold = QtGui.QFont()
        bold.setBold(True)
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(planes))
        for row, p in enumerate(planes):
            dist_mi = p["dist_km"] / KM_PER_MI if p["dist_km"] is not None else None
            sq = p["squawk"] or ""
            code, name, num = airlines.split(p["callsign"])
            flight = QtWidgets.QTableWidgetItem(p["callsign"] or "—")
            flight.setFont(bold)
            if code:
                flight.setBackground(QtGui.QColor(airlines.badge_color(code)))
                flight.setForeground(QtGui.QColor("#ffffff"))
            airline = QtWidgets.QTableWidgetItem(
                name or ("Helicopter" if p["rotor"] else ("Private / other" if p["callsign"] else "")))
            airline.setForeground(QtGui.QColor("#cfd8dc" if name else "#8a9399"))
            alt = _Num(f"{p['alt']:,}" if p["alt"] is not None else "", p["alt"])
            alt.setForeground(QtGui.QColor(airlines.alt_color(p["alt"])))
            hdg = _Num(f"{ARROWS[int((p['track'] + 22.5) % 360 // 45)]} {p['track']:.0f}°"
                       if p["track"] is not None else "", p["track"])
            vr = p["vrate"]
            vs = _Num(("▲ " if vr > 0 else "▼ " if vr < 0 else "") + f"{abs(vr):,}"
                      if vr is not None else "", vr)
            if vr:
                vs.setForeground(QtGui.QColor("#81c784" if vr > 0 else "#e57373"))
            seen = airports.infer(p)
            if seen:
                kind, acode, _aname = seen
                port = QtWidgets.QTableWidgetItem(("▼ " if kind == "arr" else "▲ ") + acode)
                port.setForeground(QtGui.QColor("#ffb74d" if kind == "arr" else "#81c784"))
                port.setToolTip(("Arriving " if kind == "arr" else "Departed ") + _aname +
                                " (from its flight path)")
            else:
                port = QtWidgets.QTableWidgetItem("")
            cells = [
                flight, airline, port, alt,
                _Num(f"{p['gs']:.0f}" if p["gs"] is not None else "", p["gs"]),
                hdg, vs,
                _Num(f"{dist_mi:.1f}" if dist_mi is not None else "", dist_mi),
                QtWidgets.QTableWidgetItem(sq + (f" {EMERGENCY[sq]}" if sq in EMERGENCY else "")),
                QtWidgets.QTableWidgetItem(p["icao"]),
                _Num(f"{p['age']:.0f}s", p["age"]),
            ]
            cells[C_ICAO].setForeground(QtGui.QColor("#8a9399"))
            if sq in EMERGENCY:
                for it in cells:
                    it.setForeground(QtGui.QColor("#ff5252"))
            for col, it in enumerate(cells):
                self.table.setItem(row, col, it)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        if self._selected:
            self._select_row(self._selected)

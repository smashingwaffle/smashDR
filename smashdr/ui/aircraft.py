"""
aircraft.py - the Aircraft tab: ADS-B from a second RTL-SDR (1090 MHz).

AdsbReader (QThread) owns its own dongle, demodulates and tracks; once a
second it emits a snapshot. AircraftTab shows a sortable table and, when
PyQt6-WebEngine is installed, a Leaflet map (Esri dark grey canvas, or OSM).
"""

import html
import json
import queue
import re
import time

from PyQt6 import QtCore, QtGui, QtWidgets

from smashdr.core import airlines, airports, flightaware, notify, squawks
from smashdr.core.icao_country import country
from smashdr.paths import USER
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
CONFIRM_S = 6.0         # an emergency squawk must hold this long (fat-finger guard)
SQ_COLOURS = {"emergency": "#ff5252", "special": "#ffb74d", "vfr": "#90a4ae"}
# helicopter makers / models as they appear in aircraft-type text
HELI_RX = re.compile(
    r"\b(helicopter|rotorcraft|bell|robinson|eurocopter|sikorsky|agusta|leonardo|"
    r"md helicopters|hughes|schweizer|enstrom|kaman|messerschmitt|mbb|"
    r"aw ?1[0-9]{2}|a109|a119|as ?3[3-6][0-9]|ec ?1[2-7][0-9]|h1[2-7][05]|bk ?117|bo ?105|"
    r"r22|r44|r66|s-?76|s-?92|uh-?60|hh-?60|ah-?64|ch-?47|mh-?60|uh-?1)\b", re.I)
TYPE_CACHE_DAYS = 30


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
.apt{font:600 10px Segoe UI,Arial,sans-serif;color:#cfd8dc;background:rgba(20,20,20,.7);
     border:none;box-shadow:none;padding:0 4px;border-radius:3px}
.apt:before{display:none}
.apt.airline{color:#fff;font-size:11px;background:rgba(41,98,255,.55)}
.apt.military{color:#c5e1a5}
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
var airportLayer = L.layerGroup();
var aptMarkers = [];
__AIRPORTS__.forEach(function(a){
  var big = a.kind === 'airline';
  var col = big ? '#2962ff' : (a.kind === 'military' ? '#7cb342' : '#90a4ae');
  var m = L.marker([a.lat, a.lon], {interactive: true, keyboard: false,
    icon: L.divIcon({className: '', iconSize: [big ? 16 : 11, big ? 16 : 11],
      iconAnchor: [big ? 8 : 5.5, big ? 8 : 5.5],
      html: '<svg width="'+(big?16:11)+'" height="'+(big?16:11)+'" viewBox="-8 -8 16 16">' +
            '<circle r="6.5" fill="#111" stroke="'+col+'" stroke-width="2"/>' +
            '<path d="M-4,1 L4,-1" stroke="'+col+'" stroke-width="2.2" stroke-linecap="round"/></svg>'})});
  m.bindTooltip(a.code, {permanent: true, direction: 'bottom', offset: [0, 4],
                         className: 'apt ' + a.kind});
  m.bindPopup('<b>' + a.code + '</b> ' + a.name + (a.kind === 'military' ? ' (military)' : ''));
  m.addTo(airportLayer); aptMarkers.push({m: m, big: big});
});
function aptLabels(){
  var z = map.getZoom();
  aptMarkers.forEach(function(o){
    var show = o.big ? z >= 7 : z >= 9;
    var t = o.m.getTooltip();
    if (show && !o.m.isTooltipOpen()) o.m.openTooltip();
    if (!show && o.m.isTooltipOpen()) o.m.closeTooltip();
  });
}
var showApts = __SHOW_AIRPORTS__;
if (showApts) airportLayer.addTo(map);
map.on('zoomend', aptLabels); map.whenReady(aptLabels);
map.on('overlayadd', function(e){ if (e.layer === airportLayer){ aptLabels(); console.log('AIRPORTS:1'); } });
map.on('overlayremove', function(e){ if (e.layer === airportLayer) console.log('AIRPORTS:0'); });
L.control.layers(layers, {'Airports': airportLayer}, {position: 'topright'}).addTo(map);
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

COLS = ["Flight", "Airline", "Squawk", "LA airport", "Alt ft", "Speed kt", "Hdg", "V/S fpm", "Dist mi",
        "ICAO", "Seen"]
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
            elif message.startswith("AIRPORTS:"):
                self.pref.emit("airports", message[9:])


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


class Images:
    """Country flags (flagcdn.com, cached in user/cache/flags) and your own airline
    logos (user/logos/<ICAO code>.png, e.g. AAL.png)."""

    FLAG_URL = "https://flagcdn.com/w40/{}.png"

    def __init__(self, net):
        self.net = net
        self.flag_dir = USER / "cache" / "flags"
        self.logo_dir = USER / "logos"
        for d in (self.flag_dir, self.logo_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self._flags = {}                      # iso -> QPixmap, or None while loading
        self._logos = {}                      # code -> QPixmap or None (not there)
        self._tails = {}                      # (code, height) -> generated QPixmap

    def flag(self, iso):
        if not iso:
            return None
        if iso in self._flags:
            return self._flags[iso]
        self._flags[iso] = None
        f = self.flag_dir / f"{iso}.png"
        pm = QtGui.QPixmap(str(f)) if f.exists() else QtGui.QPixmap()
        if not pm.isNull():
            self._flags[iso] = pm
            return pm
        reply = self.net.get(QtNetwork.QNetworkRequest(QtCore.QUrl(self.FLAG_URL.format(iso))))
        reply.finished.connect(lambda r=reply, i=iso: self._got_flag(r, i))
        return None

    def _got_flag(self, reply, iso):
        try:
            if reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                pm = QtGui.QPixmap()
                if pm.loadFromData(data):
                    self._flags[iso] = pm
                    try:
                        (self.flag_dir / f"{iso}.png").write_bytes(data)
                    except OSError:
                        pass
        finally:
            reply.deleteLater()

    def logo(self, code):
        if not code:
            return None
        if code not in self._logos:
            pm = None
            for ext in ("png", "svg", "jpg"):
                f = self.logo_dir / f"{code}.{ext}"
                if f.exists():
                    q = QtGui.QPixmap(str(f))
                    if not q.isNull():
                        pm = q
                        break
            self._logos[code] = pm
        return self._logos[code]

    def reload_logos(self):
        self._logos.clear()
        self._tails.clear()

    def tail(self, code, h=40):
        """Original tail-fin icon: fin in the airline's palette colour + its code.
        (Not a logo; your own files in user/logos override it.)"""
        key = (code, h)
        if key in self._tails:
            return self._tails[key]
        w = int(h * 1.6)
        pm = QtGui.QPixmap(w, h)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        qp = QtGui.QPainter(pm)
        qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        base = QtGui.QColor(airlines.badge_color(code))
        fin = QtGui.QPainterPath()
        fin.moveTo(w * 0.08, h * 0.95)
        fin.lineTo(w * 0.42, h * 0.06)
        fin.quadTo(w * 0.47, h * 0.0, w * 0.56, h * 0.04)
        fin.lineTo(w * 0.70, h * 0.10)
        fin.lineTo(w * 0.62, h * 0.95)
        fin.closeSubpath()
        grad = QtGui.QLinearGradient(0, 0, w * 0.6, h)
        grad.setColorAt(0, base.lighter(135))
        grad.setColorAt(1, base.darker(115))
        qp.setBrush(QtGui.QBrush(grad))
        qp.setPen(QtGui.QPen(base.darker(160), max(1.0, h / 30)))
        qp.drawPath(fin)
        # stabiliser stub
        qp.setBrush(base.darker(130))
        qp.drawRoundedRect(QtCore.QRectF(w * 0.04, h * 0.80, w * 0.66, h * 0.16), h * 0.06, h * 0.06)
        f = QtGui.QFont("Segoe UI")
        f.setBold(True)
        f.setPixelSize(int(h * 0.36))
        qp.setFont(f)
        qp.setPen(QtGui.QColor("#ffffff"))
        qp.drawText(QtCore.QRectF(w * 0.30, h * 0.18, w * 0.70, h * 0.5),
                    QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, code)
        qp.end()
        self._tails[key] = pm
        return pm

    def airline_icon(self, code, h=40):
        """Your logo from user/logos if there is one, else the generated tail fin."""
        if not code:
            return None
        logo = self.logo(code)
        if logo is not None:
            return logo.scaledToHeight(h, QtCore.Qt.TransformationMode.SmoothTransformation)
        return self.tail(code, h)


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
            QLabel#ehs { font-size: 10pt; color: #b0bec5; }
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
        self.flag = QtWidgets.QLabel()
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
        self.ehs = QtWidgets.QLabel()
        self.ehs.setObjectName("ehs")
        self.ehs.setWordWrap(True)
        self.ehs.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.ehs.setToolTip("Read from the plane's flight computers by ATC radar (Mode S Comm-B,\n"
                            "DF20/21 replies). Heading is magnetic.")

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.badge)
        top.addWidget(self.flight)
        top.addWidget(self.flag)
        top.addStretch(1)
        text = QtWidgets.QVBoxLayout()
        text.addLayout(top)
        text.addWidget(self.route)
        text.addWidget(self.type_lbl)
        text.addStretch(1)
        text.addWidget(self.live)
        text.addWidget(self.ehs)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.addWidget(self.photo)
        lay.addLayout(text, 1)
        self.clear()

    def clear(self):
        self.badge.hide()
        self.flag.clear()
        self.flight.setText("No aircraft selected")
        self.route.setText("Click a plane on the map or a row in the list.")
        self.type_lbl.setText("")
        self.live.setText("")
        self.ehs.setText("")
        self.photo.setPixmap(QtGui.QPixmap())
        self.photo.setText("")


class AircraftTab(QtWidgets.QWidget):
    emergency = QtCore.pyqtSignal(str)       # summary text, "" when none are active

    def __init__(self, opts: dict, devices: list, busy_index: int, parent=None):
        super().__init__(parent)
        self._alerted = set()                # (icao, code / state) already announced
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
        self.images = Images(self._net)
        self._type_file = USER / "cache" / "aircraft.json"
        self._load_types()
        self._typeq = []                        # ICAOs waiting for a background type lookup
        self._type_timer = QtCore.QTimer(self)
        self._type_timer.timeout.connect(self._next_type_lookup)
        self._type_timer.start(1500)            # gentle on adsbdb: < 1 request / second
        self._type_dirty = 0

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
        self.table.setIconSize(QtCore.QSize(40, 24))
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
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search: flight, airline, ICAO, squawk, country, LAX, "
                                       "helicopter…   (Enter = select first match)")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet("QLineEdit { padding: 6px 8px; font-size: 11pt; "
                                  "border-radius: 6px; }")
        self.search.textChanged.connect(lambda _t: self._refresh())
        self.search.returnPressed.connect(self._search_enter)
        self.match_lbl = QtWidgets.QLabel("")
        self.match_lbl.setStyleSheet("color: #9aa0a6;")
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(self.search, 1)
        srow.addWidget(self.match_lbl)
        ll.addLayout(srow)
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
        if self._type_dirty:
            self.save_types()

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
                .replace("__AIRPORTS__", json.dumps(airports.map_list()))
                .replace("__SHOW_AIRPORTS__", "true" if self.opts.get("adsb_map_airports", True) else "false")
                .replace("__CARTO_KEY__", json.dumps(str(self.opts.get("carto_key", "")).strip())))
        self.map.setHtml(html, QtCore.QUrl("https://smashdr.local/"))

    def _map_pref(self, kind, value):
        if kind == "layer":
            self.opts["adsb_map_layer"] = value
        elif kind == "airports":
            self.opts["adsb_map_airports"] = value == "1"
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
            c.badge.setText("")
            c.badge.setPixmap(self.images.airline_icon(code, 40))
            c.badge.setStyleSheet("background: transparent; padding: 0;")
            c.badge.show()
        else:
            c.badge.hide()
        c.flight.setText(name or p["icao"])
        iso, cname = country(p["icao"])
        fl = self.images.flag(iso)
        if fl is not None:
            c.flag.setPixmap(fl.scaledToHeight(20, QtCore.Qt.TransformationMode.SmoothTransformation))
            c.flag.setToolTip(f"Registered in {cname}")
        else:
            c.flag.clear()
        c.route.setText(self._route_html(p, lookup))
        info = self._info.get(p["icao"])
        if info:
            c.type_lbl.setText(info["text"])
        elif self.is_heli(p):
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
        state, sqtext, sqtip = self.squawk_status(p)
        if sqtext:
            col = SQ_COLOURS.get("emergency" if state in ("emergency", "unconfirmed") else state)
            parts.append(f"<span style='color:{col}'>squawk {html.escape(sqtext)}</span>" if col
                         else f"squawk {html.escape(sqtext)}")
        c.live.setText("  ·  ".join(parts))
        c.ehs.setText(self._ehs_html(p))
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
        else:
            self._fetch_photo(icao)
        if cs and self._fa_lookup(cs):
            pass                                  # FlightAware answers the route
        elif cs and cs not in self._routes:
            self._routes[cs] = None
            self._get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                      lambda d, c=cs: self._got_route(c, d))
        info = self._info.get(icao)
        if info is None or info.get("pixmap") is None:
            self.card.photo.setPixmap(QtGui.QPixmap())
            self.card.photo.setText("photo loading…" if info and info.get("photo") else "no photo")

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

    # ---- aircraft type: background lookups, disk cache, helicopter check ----
    def _load_types(self):
        try:
            data = json.loads(self._type_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        cutoff = time.time() - TYPE_CACHE_DAYS * 86400
        for icao, d in data.items() if isinstance(data, dict) else []:
            if isinstance(d, dict) and d.get("t", 0) > cutoff:
                self._info[icao] = dict(d, pixmap=None)

    def save_types(self):
        try:
            self._type_file.parent.mkdir(parents=True, exist_ok=True)
            out = {i: {k: v for k, v in d.items() if k != "pixmap"}
                   for i, d in self._info.items() if d}
            self._type_file.write_text(json.dumps(out), encoding="utf-8")
            self._type_dirty = 0
        except OSError as e:
            print("[!] Could not save aircraft cache:", e)

    def _next_type_lookup(self):
        while self._typeq:
            icao = self._typeq.pop(0)
            if icao not in self._info and icao in self._planes:
                self._info[icao] = None
                self._get(f"https://api.adsbdb.com/v0/aircraft/{icao}",
                          lambda d, i=icao: self._got_aircraft(i, d))
                return

    # ---- Comm-B (radar-requested data) ---------------------------------------
    @staticmethod
    def _ehs(p, bds, max_age=60.0):
        e = p.get("ehs") or {}
        t = e.get("t_" + bds)
        return e if t is not None and time.time() - t <= max_age else None

    @classmethod
    def selected_alt(cls, p):
        e = cls._ehs(p, "4,0")
        return (e.get("sel_alt") or e.get("fms_alt")) if e else None

    def _ehs_html(self, p):
        parts = []
        sel = self.selected_alt(p)
        if sel:
            e = p["ehs"]
            alt = p["alt"]
            how = ""
            if alt is not None:
                how = (" (climbing)" if sel > alt + 150 else " (descending)" if sel < alt - 150
                       else " (level)")
            modes = ", ".join(e.get("ap_modes") or [])
            parts.append(f"<b style='color:#80cbc4'>Autopilot ➜ {sel:,} ft</b>{how}"
                         + (f" {modes}" if modes else ""))
        e = self._ehs(p, "6,0")
        if e:
            bits = [f"heading {e['heading']:.0f}°"]
            if e.get("ias"):
                bits.append(f"IAS {e['ias']} kt")
            if e.get("mach") and e["mach"] >= 0.3:
                bits.append(f"Mach {e['mach']:.2f}")
            parts.append(" · ".join(bits))
        e = self._ehs(p, "5,0")
        if e:
            bits = []
            if e.get("tas"):
                bits.append(f"TAS {e['tas']} kt")
            r = e.get("roll")
            if r is not None:
                bits.append("wings level" if abs(r) < 2 else
                            f"banking {abs(r):.0f}° {'right' if r > 0 else 'left'}")
            if bits:
                parts.append(" · ".join(bits))
        e = self._ehs(p, "4,0")
        if e and e.get("baro"):
            parts.append(f"altimeter {e['baro']:.0f} hPa ({e['baro'] * 0.02953:.2f} inHg)")
        return ("<span style='color:#78909c'>Radar data:</span> " + "  ·  ".join(parts)) if parts else ""

    # ---- squawk / emergencies ------------------------------------------------
    @staticmethod
    def squawk_status(p):
        """-> (state, text, tooltip) with state 'emergency' | 'unconfirmed' | level | None."""
        sq = p.get("squawk") or ""
        es = p.get("emergency_state") or 0
        d = squawks.describe(sq)
        kind = squawks.EMERGENCY_STATE.get(es)
        if sq in squawks.EMERGENCY_CODES or es:
            held = time.time() - (p.get("squawk_since") or 0)
            if sq in squawks.EMERGENCY_CODES:
                text = f"{sq} {d['label']}" + (f" · {kind}" if kind and kind != "general emergency" else "")
            else:                                  # emergency flagged by the aircraft itself
                text = f"{sq} · {(kind or 'emergency').upper()}".strip(" ·")
            tip = (d["meaning"] if d else "Emergency") + (f"; aircraft reports: {kind}" if kind else "")
            if es or held >= CONFIRM_S:
                return "emergency", text, tip
            return "unconfirmed", text + " ?", tip + ". Just set: could be a mis-entry, confirming…"
        if not d:
            return None, "", ""
        since = p.get("squawk_since")
        held = ""
        if since:
            m = int((time.time() - since) // 60)
            held = f"\nSet {m} min ago." if m else "\nJust set."
        if d["level"] == "normal":
            return "normal", sq, (f"Squawk {sq}: ATC discrete code\n"
                                  "A unique code the controller (SoCal Approach / LA Center) gives this\n"
                                  "flight so radar can tag it. No special meaning; it changes when the\n"
                                  "flight is handed to another facility." + held)
        return d["level"], f"{sq} {d['label']}", f"Squawk {sq}: {d['label']}\n{d['meaning']}" + held

    def is_emergency(self, p):
        return self.squawk_status(p)[0] == "emergency"

    def _check_emergencies(self, planes):
        active = []
        for p in planes:
            state, text, tip = self.squawk_status(p)
            if state != "emergency":
                continue
            name = p["callsign"] or p["icao"]
            active.append(f"{name} {text}")
            key = (p["icao"], p.get("squawk"), p.get("emergency_state"))
            if key in self._alerted:
                continue
            self._alerted.add(key)
            QtWidgets.QApplication.beep()
            if self.opts.get("ntfy_aircraft", True):
                gm = (f"https://www.google.com/maps/search/?api=1&query={p['lat']:.5f},{p['lon']:.5f}"
                      if p["lat"] is not None else None)
                notify.send(self.opts.get("ntfy_topic", ""), f"Aircraft emergency: {name}",
                            f"{text}\n{tip}\nalt {p['alt']} ft, "
                            + (f"{p['dist_km'] / KM_PER_MI:.0f} mi from home" if p.get("dist_km") else "no position"),
                            priority="urgent", tags="airplane,rotating_light", click=gm)
            where = (f"{p['lat']:.4f},{p['lon']:.4f}" if p["lat"] is not None else "no position")
            line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {name}  {p['icao']}  {text}  "
                    f"alt {p['alt']}  {where}  ({tip})")
            print("[EMERGENCY]", line)
            try:
                with open(USER / "emergencies.txt", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            self._select(p["icao"], center=True)
            self._select_row(p["icao"])
        self.emergency.emit("  ·  ".join(active))

    def is_heli(self, p):
        info = self._info.get(p["icao"])
        return bool(p["rotor"] or p.get("hover") or (info and info.get("heli")))

    def _got_aircraft(self, icao, data):
        a = _field(data, "aircraft")
        if not isinstance(a, dict):
            self._info[icao] = {"text": f"ICAO {icao} · not in the aircraft database",
                                "pixmap": None, "heli": False, "photo": None, "t": time.time()}
        else:
            bits = [a.get("manufacturer", ""), a.get("type", "")]
            line1 = " ".join(b for b in bits if b).strip() or a.get("icao_type", "")
            line2 = " · ".join(x for x in (a.get("registration"), a.get("registered_owner"),
                                            a.get("registered_owner_country_name")) if x)
            heli = bool(HELI_RX.search(" ".join(str(a.get(k, "")) for k in
                                                ("manufacturer", "type", "icao_type"))))
            self._info[icao] = {"text": f"{line1}\n{line2}".strip(), "pixmap": None,
                                "heli": heli, "t": time.time(),
                                "photo": a.get("url_photo_thumbnail") or a.get("url_photo")}
        self._type_dirty += 1
        if self._type_dirty >= 20:
            self.save_types()
        if icao == self._selected:
            self._fetch_photo(icao)
            self._update_card()

    def _fetch_photo(self, icao):
        info = self._info.get(icao)
        if info and info.get("photo") and info.get("pixmap") is None and not info.get("photo_req"):
            info["photo_req"] = True
            self._get(info["photo"], lambda d, i=icao: self._got_photo(i, d))

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
    def _haystack(self, p):
        """Everything a search can match for one plane (lower case)."""
        code, name, _num = airlines.split(p["callsign"])
        _iso, cname = country(p["icao"])
        bits = [p["callsign"], p["icao"], p["squawk"], name, code, cname]
        if self.is_heli(p):
            bits += ["helicopter", "heli", "rotor"]
        if not name and p["callsign"]:
            bits += ["private"]
        if not p.get("adsb", True):
            bits += ["transponder", "mode s", "no position"]
        if self.is_heli(p):
            bits += ["helicopter", "heli"]
        seen = airports.infer(p)
        if seen:
            bits += [seen[1], seen[2], "arriving" if seen[0] == "arr" else "departed"]
        state, sqtext, sqtip = self.squawk_status(p)
        bits += [sqtext, sqtip]
        if state in ("emergency", "unconfirmed"):
            bits += ["emergency"]
        info = self._info.get(p["icao"])
        if info:
            bits.append(info.get("text"))
        fa = self._fa.get(p["callsign"], (None, None))[1] if p["callsign"] else None
        if isinstance(fa, dict):
            bits += [fa["orig"][0], fa["orig"][1], fa["dest"][0], fa["dest"][1], fa["type"], fa["reg"]]
        db = self._routes.get(p["callsign"]) if p["callsign"] else None
        if isinstance(db, dict):
            bits += [db["orig"][0], db["orig"][1], db["dest"][0], db["dest"][1]]
        return " ".join(str(b) for b in bits if b).lower()

    def _matches(self, planes):
        q = self.search.text().strip().lower()
        if not q:
            return planes
        words = q.split()
        return [p for p in planes if all(w in self._haystack(p) for w in words)]

    def _search_enter(self):
        shown = self._matches(list(self._planes.values()))
        if shown:
            first = min(shown, key=lambda p: p["dist_km"] if p["dist_km"] is not None else 1e9)
            self._select(first["icao"], center=True)
            self._select_row(first["icao"])

    def _refresh(self):
        """Re-draw list + map from the last snapshot (e.g. search text changed)."""
        planes = list(self._planes.values())
        shown = self._matches(planes)
        q = self.search.text().strip()
        self.match_lbl.setText(f"{len(shown)} of {len(planes)}" if q else "")
        self._fill_table(shown)
        if HAVE_WEB and self._map_ready:
            js = [{"icao": p["icao"], "flight": p["callsign"] or "", "lat": p["lat"],
                   "lon": p["lon"], "alt": p["alt"], "track": p["track"],
                   "rotor": self.is_heli(p), "emergency": self.is_emergency(p),
                   "trail": p["trail"]} for p in shown]
            self._js(f"update({json.dumps(js)})")

    def _on_snapshot(self, planes, stats):
        self._planes = {p["icao"]: p for p in planes}
        for p in planes:                       # learn types in the background (helicopters)
            if p["icao"] not in self._info and p["icao"] not in self._typeq:
                self._typeq.append(p["icao"])
        with_pos = sum(1 for p in planes if p["lat"] is not None)
        self.stat_lbl.setText(f"{len(planes)} aircraft ({with_pos} with position)  ·  "
                              f"{stats['msgs_per_s']:.0f} msg/s  ·  {stats['total']} total"
                              + (f"  ·  {stats['dropped']} blocks dropped" if stats["dropped"] else ""))
        self._refresh()
        if self._selected in self._planes:
            self._update_card()
        self._check_emergencies(planes)

    def _fill_table(self, planes):
        bold = QtGui.QFont()
        bold.setBold(True)
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(planes))
        for row, p in enumerate(planes):
            dist_mi = p["dist_km"] / KM_PER_MI if p["dist_km"] is not None else None
            sq_state, sq_text, sq_tip = self.squawk_status(p)
            code, name, num = airlines.split(p["callsign"])
            flight = QtWidgets.QTableWidgetItem(p["callsign"] or "—")
            flight.setFont(bold)
            if code:
                flight.setIcon(QtGui.QIcon(self.images.airline_icon(code, 40)))
            airline = QtWidgets.QTableWidgetItem(
                name or ("Helicopter" if self.is_heli(p) else
                         ("Private / other" if p["callsign"] else
                          ("" if p.get("adsb", True) else "Transponder only (no position)"))))
            airline.setForeground(QtGui.QColor("#cfd8dc" if name else "#8a9399"))
            iso, cname = country(p["icao"])
            fl = self.images.flag(iso)
            if fl is not None:
                airline.setIcon(QtGui.QIcon(fl))
                airline.setToolTip(f"Registered in {cname}")

            alt = _Num(f"{p['alt']:,}" if p["alt"] is not None else "", p["alt"])
            sel = self.selected_alt(p)
            if sel and p["alt"] is not None and abs(sel - p["alt"]) > 150:
                alt.setText(f"{p['alt']:,} ➜ {sel:,}")
                alt.setToolTip(f"Autopilot set to {sel:,} ft (from ATC radar data)")
            elif sel:
                alt.setToolTip(f"Autopilot holding {sel:,} ft")
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
            sqi = QtWidgets.QTableWidgetItem(sq_text)
            if sq_tip:
                sqi.setToolTip(sq_tip)
            col = SQ_COLOURS.get("emergency" if sq_state in ("emergency", "unconfirmed") else sq_state)
            if col:
                sqi.setForeground(QtGui.QColor(col))
            if sq_state == "emergency":
                sqi.setFont(bold)
            cells = [
                flight, airline, sqi, port, alt,
                _Num(f"{p['gs']:.0f}" if p["gs"] is not None else "", p["gs"]),
                hdg, vs,
                _Num(f"{dist_mi:.1f}" if dist_mi is not None else "", dist_mi),
                QtWidgets.QTableWidgetItem(p["icao"]),
                _Num(f"{p['age']:.0f}s", p["age"]),
            ]
            cells[C_ICAO].setForeground(QtGui.QColor("#8a9399"))
            if sq_state == "emergency":
                for it in cells:
                    it.setForeground(QtGui.QColor("#ff5252"))
                    it.setBackground(QtGui.QColor(90, 20, 20))
            for col, it in enumerate(cells):
                self.table.setItem(row, col, it)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        if self._selected:
            self._select_row(self._selected)

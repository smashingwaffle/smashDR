"""
bandplan.py - simplified US band plan for the RTL-SDR range (24 MHz - 1.77 GHz).

For orientation only ("where am I?"), not a regulatory reference. Based on
the NTIA/FCC allocation chart, condensed to what you actually hear.

Each entry: (lo_MHz, hi_MHz, category, label, suggested_mode, suggested_step_Hz)
suggested_mode / step may be None ("leave as is").
Where entries overlap, the narrowest one wins for the label.
"""

CATEGORIES = {
    # category: (display name, RGB)
    "amateur": ("Amateur", (46, 160, 100)),
    "broadcast": ("Broadcast", (60, 120, 200)),
    "aviation": ("Aviation", (90, 170, 230)),
    "marine": ("Marine", (40, 150, 160)),
    "landmobile": ("Land mobile", (30, 130, 150)),
    "publicsafety": ("Public safety", (200, 80, 70)),
    "government": ("Government", (130, 130, 125)),
    "satellite": ("Satellite", (150, 100, 200)),
    "cellular": ("Cellular", (210, 90, 150)),
    "ism": ("ISM / unlicensed", (220, 160, 40)),
    "personal": ("Personal radio", (230, 120, 50)),
    "navigation": ("Navigation", (170, 150, 60)),
}

BANDS = [
    (24.89, 24.99, "amateur", "Amateur 12 m", None, None),
    (25.67, 26.10, "broadcast", "Shortwave 11 m", "AM", 5000),
    (26.965, 27.405, "personal", "CB radio", "AM", 10000),
    (28.0, 29.7, "amateur", "Amateur 10 m", None, None),
    (29.7, 50.0, "landmobile", "VHF low band", "NFM", 5000),
    (50.0, 54.0, "amateur", "Amateur 6 m", None, None),
    (54.0, 72.0, "broadcast", "TV ch 2-4", None, None),
    (72.0, 76.0, "landmobile", "72 MHz fixed / RC", "NFM", 20000),
    (76.0, 88.0, "broadcast", "TV ch 5-6", None, None),
    (88.0, 108.0, "broadcast", "FM broadcast", "WFM", 200000),
    (108.0, 118.0, "navigation", "Air nav (VOR / ILS)", "AM", 50000),
    (118.0, 137.0, "aviation", "Airband", "AM", 25000),
    (137.0, 138.0, "satellite", "Weather satellites", "WFM", 5000),
    (138.0, 144.0, "government", "Federal / military", "NFM", 12500),
    (144.0, 148.0, "amateur", "Amateur 2 m", "NFM", 5000),
    (148.0, 150.8, "government", "Federal / military", "NFM", 12500),
    (150.8, 174.0, "landmobile", "VHF high band", "NFM", 2500),
    (151.82, 151.94, "personal", "MURS", "NFM", 60000),
    (154.57, 154.60, "personal", "MURS", "NFM", 30000),
    (153.725, 154.46, "publicsafety", "VHF public safety", "NFM", 7500),
    (155.025, 156.0, "publicsafety", "VHF public safety", "NFM", 7500),
    (156.0, 157.45, "marine", "Marine VHF", "NFM", 25000),
    (160.215, 161.565, "landmobile", "Railroad", "NFM", 15000),
    (161.6, 162.025, "marine", "Marine VHF", "NFM", 25000),
    (162.4, 162.55, "government", "NOAA weather radio", "NFM", 25000),
    (174.0, 216.0, "broadcast", "TV ch 7-13", None, None),
    (216.0, 222.0, "landmobile", "220 MHz land mobile", "NFM", 5000),
    (222.0, 225.0, "amateur", "Amateur 1.25 m", "NFM", 20000),
    (225.0, 400.0, "aviation", "Military airband", "AM", 25000),
    (400.0, 406.0, "satellite", "Met aids / satellites", None, None),
    (406.0, 406.1, "satellite", "Emergency beacons", None, None),
    (406.1, 420.0, "government", "Federal land mobile", "NFM", 12500),
    (420.0, 450.0, "amateur", "Amateur 70 cm", "NFM", 5000),
    (433.05, 434.79, "ism", "ISM 433 MHz devices", "NFM", 25000),
    (450.0, 470.0, "landmobile", "UHF land mobile", "NFM", 12500),
    (453.0, 454.0, "publicsafety", "UHF public safety", "NFM", 12500),
    (458.0, 459.0, "publicsafety", "UHF public safety", "NFM", 12500),
    (462.55, 462.725, "personal", "FRS / GMRS", "NFM", 25000),
    (467.5625, 467.7125, "personal", "FRS", "NFM", 25000),
    (470.0, 512.0, "landmobile", "UHF-T (TV / land mobile)", "NFM", 12500),
    (512.0, 608.0, "broadcast", "TV ch 21-36", None, None),
    (608.0, 614.0, "government", "Radio astronomy", None, None),
    (614.0, 698.0, "cellular", "600 MHz cellular", None, None),
    (698.0, 758.0, "cellular", "700 MHz cellular", None, None),
    (758.0, 769.0, "publicsafety", "FirstNet broadband", None, None),
    (769.0, 775.0, "publicsafety", "700 MHz public safety (P25)", "NFM", 6250),
    (775.0, 799.0, "cellular", "700 MHz cellular / FirstNet uplink", None, None),
    (799.0, 805.0, "publicsafety", "700 MHz public safety mobiles", "NFM", 6250),
    (806.0, 824.0, "landmobile", "800 MHz mobiles", "NFM", 12500),
    (824.0, 849.0, "cellular", "Cellular uplink", None, None),
    (851.0, 869.0, "publicsafety", "800 MHz trunked (P25)", "NFM", 12500),
    (869.0, 894.0, "cellular", "Cellular downlink", None, None),
    (896.0, 902.0, "landmobile", "900 MHz land mobile", "NFM", 12500),
    (902.0, 928.0, "amateur", "Amateur 33 cm / ISM 915", "NFM", 25000),
    (929.0, 932.0, "landmobile", "Paging", "NFM", 25000),
    (935.0, 941.0, "landmobile", "900 MHz land mobile", "NFM", 12500),
    (960.0, 1215.0, "navigation", "Air nav (DME / TACAN)", None, None),
    (1087.0, 1093.0, "aviation", "ADS-B / Mode S 1090", None, None),
    (1215.0, 1240.0, "navigation", "GNSS L2", None, None),
    (1240.0, 1300.0, "amateur", "Amateur 23 cm", "NFM", 25000),
    (1300.0, 1390.0, "government", "Radar", None, None),
    (1525.0, 1559.0, "satellite", "Inmarsat downlink", None, None),
    (1559.0, 1610.0, "navigation", "GNSS L1 (GPS 1575.42)", None, None),
    (1610.0, 1626.5, "satellite", "Iridium / Globalstar", None, None),
    (1670.0, 1710.0, "satellite", "Weather satellites (GOES)", None, None),
]


def lookup(hz: float):
    """Narrowest band containing hz, or None."""
    mhz = hz / 1e6
    hits = [b for b in BANDS if b[0] <= mhz < b[1]]
    if not hits:
        return None
    return min(hits, key=lambda b: b[1] - b[0])


def describe(hz: float) -> str:
    b = lookup(hz)
    if not b:
        return "Unlisted"
    return f"{CATEGORIES[b[2]][0]} · {b[3]}"


def in_span(lo_hz: float, hi_hz: float):
    """Bands overlapping a span, widest first (so narrow ones draw on top)."""
    lo, hi = lo_hz / 1e6, hi_hz / 1e6
    hits = [b for b in BANDS if b[1] > lo and b[0] < hi]
    return sorted(hits, key=lambda b: -(b[1] - b[0]))

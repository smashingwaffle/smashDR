"""
icao_country.py - country of registration from the 24-bit ICAO aircraft address.

ICAO hands each state a block of addresses (Annex 10 Vol III), so the hex
address alone tells where a plane is registered: A00000-AFFFFF is the US,
C00000-C3FFFF Canada, and so on.

    country("A8B964") -> ("us", "United States")
"""

# (first, last, ISO 3166 alpha-2, name) - the common blocks; more can be added
_RANGES = [
    (0x008000, 0x00FFFF, "za", "South Africa"),
    (0x010000, 0x017FFF, "eg", "Egypt"),
    (0x040000, 0x0403FF, "et", "Ethiopia"),
    (0x06A000, 0x06A3FF, "qa", "Qatar"),
    (0x0AC000, 0x0ACFFF, "co", "Colombia"),
    (0x0C2000, 0x0C23FF, "pa", "Panama"),
    (0x0D0000, 0x0D7FFF, "mx", "Mexico"),
    (0x100000, 0x1FFFFF, "ru", "Russia"),
    (0x300000, 0x33FFFF, "it", "Italy"),
    (0x340000, 0x37FFFF, "es", "Spain"),
    (0x380000, 0x3BFFFF, "fr", "France"),
    (0x3C0000, 0x3FFFFF, "de", "Germany"),
    (0x400000, 0x43FFFF, "gb", "United Kingdom"),
    (0x440000, 0x447FFF, "at", "Austria"),
    (0x448000, 0x44FFFF, "be", "Belgium"),
    (0x458000, 0x45FFFF, "dk", "Denmark"),
    (0x460000, 0x467FFF, "fi", "Finland"),
    (0x468000, 0x46FFFF, "gr", "Greece"),
    (0x470000, 0x477FFF, "hu", "Hungary"),
    (0x478000, 0x47FFFF, "no", "Norway"),
    (0x480000, 0x487FFF, "nl", "Netherlands"),
    (0x488000, 0x48FFFF, "pl", "Poland"),
    (0x490000, 0x497FFF, "pt", "Portugal"),
    (0x498000, 0x49FFFF, "cz", "Czechia"),
    (0x4A8000, 0x4AFFFF, "se", "Sweden"),
    (0x4B0000, 0x4B7FFF, "ch", "Switzerland"),
    (0x4B8000, 0x4BFFFF, "tr", "Turkey"),
    (0x4CA000, 0x4CAFFF, "ie", "Ireland"),
    (0x4CC000, 0x4CCFFF, "is", "Iceland"),
    (0x4D0000, 0x4D03FF, "lu", "Luxembourg"),
    (0x710000, 0x717FFF, "sa", "Saudi Arabia"),
    (0x718000, 0x71FFFF, "kr", "South Korea"),
    (0x738000, 0x73FFFF, "il", "Israel"),
    (0x750000, 0x757FFF, "my", "Malaysia"),
    (0x758000, 0x75FFFF, "ph", "Philippines"),
    (0x768000, 0x76FFFF, "sg", "Singapore"),
    (0x780000, 0x7BFFFF, "cn", "China"),
    (0x7C0000, 0x7FFFFF, "au", "Australia"),
    (0x800000, 0x83FFFF, "in", "India"),
    (0x840000, 0x87FFFF, "jp", "Japan"),
    (0x880000, 0x887FFF, "th", "Thailand"),
    (0x888000, 0x88FFFF, "vn", "Vietnam"),
    (0x896000, 0x896FFF, "ae", "United Arab Emirates"),
    (0x899000, 0x8993FF, "tw", "Taiwan"),
    (0x8A0000, 0x8A7FFF, "id", "Indonesia"),
    (0xA00000, 0xAFFFFF, "us", "United States"),
    (0xC00000, 0xC3FFFF, "ca", "Canada"),
    (0xC80000, 0xC87FFF, "nz", "New Zealand"),
    (0xC88000, 0xC88FFF, "fj", "Fiji"),
    (0xE00000, 0xE3FFFF, "ar", "Argentina"),
    (0xE40000, 0xE7FFFF, "br", "Brazil"),
    (0xE80000, 0xE80FFF, "cl", "Chile"),
    (0xE84000, 0xE84FFF, "pe", "Peru"),
]


def country(icao_hex: str):
    try:
        a = int(icao_hex, 16)
    except (TypeError, ValueError):
        return None, None
    for lo, hi, iso, name in _RANGES:
        if lo <= a <= hi:
            return iso, name
    return None, None

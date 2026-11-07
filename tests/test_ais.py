"""python -m tests.test_ais - AIS decoder round trip on synthetic GMSK + known AIVDM payloads."""

import numpy as np

from smashdr.decoders import ais

SIX = {c: i for i, c in enumerate(ais.SIXBIT)}


def armor_to_bits(s, fill=0):
    """NMEA armored payload -> bits (gpsd test vectors)."""
    out = []
    for ch in s:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        out += [(v >> (5 - k)) & 1 for k in range(6)]
    return np.array(out[:len(out) - fill], dtype=np.uint8)


def gmsk(bits, fs=2_400_000, baud=9600, dev=2400, bt=0.4):
    sps = fs // baud
    nrz = np.repeat(np.asarray(bits, np.float64) * 2 - 1, sps)
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    sig = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-t ** 2 / (2 * sig ** 2))
    g /= g.sum()
    f = np.convolve(nrz, g, "same") * dev
    ph = 2 * np.pi * np.cumsum(f) / fs
    return np.exp(1j * ph)


def test_parse():
    # gpsd sample: type 1, MMSI 371798000
    m = ais.parse(armor_to_bits("15RTgt0PAso;90TKcjM8h6g208CQ"))
    assert m["type"] == 1 and m["mmsi"] == 371798000, m
    assert abs(m["lat"] - 48.38163) < 1e-4 and abs(m["lon"] + 123.39538) < 1e-4, m
    assert m["sog"] == 12.3 and m["cog"] == 224.0 and m["heading"] == 215, m
    # type 5 (two-sentence, fill 2), MMSI 351759000
    p5 = ("55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8"
          "88888888880")
    m = ais.parse(armor_to_bits(p5, 2))
    assert m["type"] == 5 and m["mmsi"] == 351759000, m
    assert m["name"] == "EVER DIADEM" and m["callsign"] == "3FOF8", m
    assert m["destination"] == "NEW YORK" and m["shiptype"] == 70, m
    print("parse ok:", m["name"], m["destination"], m["length"], "m")


def test_rf():
    rate, center = 2_400_000, 162_000_000
    rx = ais.AisRx(rate, center)
    pays = [armor_to_bits("15RTgt0PAso;90TKcjM8h6g208CQ"),
            armor_to_bits("55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp888888888880", 2)]
    rng = np.random.default_rng(1)
    n = rate  # 1 s
    iq = (rng.normal(size=n) + 1j * rng.normal(size=n)) * 0.05
    t = np.arange(n) / rate
    for k, (p, f) in enumerate(zip(pays, ais.CHANNELS)):
        burst = gmsk(ais.encode_frame(p)) * 0.3
        s = 200_000 + k * 300_000
        iq[s:s + len(burst)] += burst * np.exp(2j * np.pi * (f - center) * t[s:s + len(burst)])
    q = np.clip(np.round(iq.real * 100 + 127.5), 0, 255), np.clip(np.round(iq.imag * 100 + 127.5), 0, 255)
    iq = ((q[0] - 127.5) + 1j * (q[1] - 127.5)).astype(np.complex64) / 128
    got = []
    for i in range(0, n, 65536):
        got += rx.process(iq[i:i + 65536])
    got += rx.process(np.zeros(4 * 65536, np.complex64))
    kinds = sorted((m["type"], m["mmsi"]) for m in got)
    assert kinds == [(1, 371798000), (5, 351759000)], kinds
    tr = ais.ShipTracker()
    for m in got:
        tr.update(m)
    s = tr.ships[351759000]
    print("rf ok:", kinds, "flag", s["flag"], tr.ships[371798000]["flag"])


if __name__ == "__main__":
    test_parse()
    test_rf()
    print("all AIS tests passed")

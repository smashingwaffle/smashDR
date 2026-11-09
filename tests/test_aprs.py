"""python -m tests.test_aprs - APRS AFSK round trip (IQ with Doppler + noise) and parser checks."""

import numpy as np

from smashdr.decoders import aprs


def test_parse():
    p = aprs.parse_aprs("!3405.12N/11811.34W-PHG2360 smashDR")
    assert p["kind"] == "position" and abs(p["lat"] - 34.0853) < 1e-3 and abs(p["lon"] + 118.189) < 1e-3, p
    p = aprs.parse_aprs("=/5L!!<*e7>7P[")                        # aprs spec compressed example
    assert p["kind"] == "position" and abs(p["lat"] - 49.5) < 0.01 and abs(p["lon"] + 72.75) < 0.01, p
    p = aprs.parse_aprs("`(_fn\"Oj/]Hello", "S32UVT")            # aprs spec Mic-E example
    assert p["kind"] == "position" and abs(p["lat"] - 33.4275) < 1e-3 and abs(p["lon"] + 112.129) < 1e-3, p
    p = aprs.parse_aprs(":KO6IKZ   :hello from space{01")
    assert p["kind"] == "message" and p["to"] == "KO6IKZ", p
    print("parse ok")


def test_rf():
    rate, center = 2_400_000, 145_500_000
    rx = aprs.AprsRx(rate, center)
    assert {c.hz for c in rx.chans} == {145_825_000, 144_390_000}
    pk = [aprs.ax25_frame("KO6IKZ-7", "APRS", ["RS0ISS*", "WIDE2-1"], "!3405.12N/11811.34W-via the ISS"),
          aprs.ax25_frame("W6ABC-9", "S32UVT", ["WIDE1-1"], "`(_fn\"Oj/]mobile")]
    n = 3 * rate
    rng = np.random.default_rng(2)
    iq = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64) * 0.05
    t = np.arange(n) / rate
    for k, (f, hz, dop) in enumerate(zip(pk, (145_825_000, 144_390_000), (3100, 0))):
        a = aprs.afsk_audio(f)
        a = np.interp(np.arange(len(a) * 50) / 50, np.arange(len(a)), a)     # 48 k -> 2.4 M
        ph = 2 * np.pi * 3000 / rate * np.cumsum(a)
        s = 300_000 + k * 1_000_000
        seg = slice(s, s + len(a))
        iq[seg] += 0.3 * np.exp(1j * (ph + 2 * np.pi * (hz - center + dop) * t[seg]))
    got = []
    for i in range(0, n, 131072):
        got += rx.process(iq[i:i + 131072], now=i / rate)
    got += rx.flush(now=n / rate)
    srcs = sorted((p["src"], p["band"], p["via_iss"], p["kind"]) for p in got)
    assert srcs == [("KO6IKZ-7", "ISS", True, "position"), ("W6ABC-9", "APRS", False, "position")], srcs
    print("rf ok:", [p["text"] for p in got])


if __name__ == "__main__":
    test_parse()
    test_rf()
    print("all APRS tests passed")

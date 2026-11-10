"""python -m tests.test_vdl2 - VDL Mode 2: RS(255,249) round trip, a synthetic D8PSK burst carrying
an ACARS frame through the whole receiver, and (if present) Stefaan's 136 MHz recording."""

import os
import random

import numpy as np

from smashdr.decoders import vdl2 as V


def test_rs():
    random.seed(1)
    for _ in range(200):
        n = random.choice([5, 20, 40, 100, 249])
        nr = V.fec_octets(n) if n < 249 else 6
        if not nr:
            continue
        data = [random.randrange(256) for _ in range(n)]
        cw = data + V.rs_encode(data, nr)
        bad = cw[:]
        for p in random.sample(range(len(cw)), nr // 2):
            bad[p] ^= random.randrange(1, 256)
        assert V.rs_decode(bad, nr) == cw
    print("rs ok")


def _addr_octets(typ, a, flag, last):
    v = (flag << 27) | (typ << 24) | a
    bits = [(v >> (27 - i)) & 1 for i in range(28)]
    out = []
    for o in range(4):
        x = 1 if (last and o == 3) else 0
        for j in range(7):
            x |= bits[o * 7 + j] << (j + 1)
        out.append(x)
    return bytes(out)


def make_frame(text):
    acars = b"\xff\xff\x01" + b"2.N225WN\x15H18\x02D84AWN3309" + text.encode() + b"\x03"
    fr = _addr_octets(5, 0x11500A, 0, False) + _addr_octets(1, 0xA1F595, 0, True) + b"\x00" + acars
    fcs = V._crc16_x25(fr)
    return fr + bytes([fcs & 255, fcs >> 8])


def burst_iq(frame, fs=96_000, snr_db=25, f_off=300.0):
    raw = np.unpackbits(np.frombuffer(frame, np.uint8).reshape(-1, 1), axis=1)[:, ::-1].ravel()
    st, run = [], 0
    for x in raw:
        st.append(int(x))
        run = run + 1 if x else 0
        if run == 5:
            st.append(0)
            run = 0
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = np.array(flag + st + flag, np.uint8)
    L = len(bits)
    nd = (L + 7) // 8
    bits = np.concatenate([bits, np.zeros(nd * 8 - L, np.uint8)])
    octets = np.packbits(bits.reshape(-1, 8)[:, ::-1], axis=1).ravel().tolist()
    nb = (nd + 248) // 249
    lens = [249] * (nb - 1) + [nd - 249 * (nb - 1)]
    fl = [6] * (nb - 1) + [V.fec_octets(lens[-1])]
    blocks, p = [], 0
    for ln in lens:
        blocks.append(octets[p:p + ln])
        p += ln
    fecs = [V.rs_encode(b, f) for b, f in zip(blocks, fl)]
    tx = []
    for r in range(max(lens)):
        tx += [blocks[b][r] for b in range(nb) if r < lens[b]]
    for r in range(6):
        tx += [fecs[b][r] for b in range(nb) if r < fl[b]]
    data = np.unpackbits(np.array(tx, np.uint8).reshape(-1, 1), axis=1)[:, ::-1].ravel()
    hdr = np.array([0, 0, 0] + [(L >> j) & 1 for j in range(17)] + [0] * 5, np.uint8)
    allb = np.concatenate([hdr, data])
    allb = np.concatenate([allb, np.zeros((-len(allb)) % 3, np.uint8)])
    allb ^= V._SCR[:len(allb)]
    inv = {V._gray(k): k for k in range(8)}
    ks = [0] * 5 + list(V.UNIQUE_WORD) + [inv[(a << 2) | (b << 1) | c] for a, b, c in allb.reshape(-1, 3)]
    ph = np.cumsum(np.array(ks) * np.pi / 4)
    sps = fs / V.SYM_RATE
    n = int(len(ks) * sps) + 200
    t = np.arange(n) / sps
    # raised-cosine-ish pulse: linear interpolation of the symbol phases (good enough for a loop test)
    sym = np.exp(1j * ph)
    re = np.interp(t, np.arange(len(sym)), sym.real)
    im = np.interp(t, np.arange(len(sym)), sym.imag)
    z = (re + 1j * im) * np.exp(2j * np.pi * f_off * np.arange(n) / fs)
    rng = np.random.default_rng(3)
    noise = (rng.normal(size=n) + 1j * rng.normal(size=n)) * 10 ** (-snr_db / 20) / np.sqrt(2)
    pad = (rng.normal(size=600) + 1j * rng.normal(size=600)) * 10 ** (-snr_db / 20) / np.sqrt(2)
    return np.concatenate([pad, z + noise, pad]).astype(np.complex64)


def test_burst():
    text = "#DFB TEST FROM SMASHDR " * 6
    fr = make_frame(text)
    k, q = V.burst_symbols(burst_iq(fr), 96_000)
    frames, info = V.decode_burst(k)
    assert frames and frames[0] == fr[:-2], info
    m = V.parse_avlc(frames[0])
    assert m["src_type"] == "aircraft" and m["src"] == "A1F595" and m["dst"] == "11500A", m
    assert m["acars"]["flight"] == "WN3309" and m["acars"]["reg"] == "N225WN", m["acars"]
    print("burst ok:", info, m["acars"]["label"], m["acars"]["text"][:30])


def test_recording():
    f = "recordings/20261109_215017_136_10000MHz_2400000sps.cu8"
    if not os.path.exists(f):
        print("recording not here, skipped")
        return
    rx = V.Vdl2Rx(2_400_000, 136_400_000)
    raw = np.memmap(f, np.uint8, mode="r")
    got = []
    B = 524288
    for i in range(0, len(raw) - B, B):
        u = np.asarray(raw[i:i + B])
        got += rx.process(((u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)).astype(np.complex64) / 128)
    flights = {m["acars"]["flight"] for m in got if m.get("acars")}
    assert {"UA1605", "WN3309"} <= flights, flights
    print("recording ok:", len(got), "frames", sorted(flights))


if __name__ == "__main__":
    test_rs()
    test_burst()
    test_recording()
    print("all VDL2 tests passed")

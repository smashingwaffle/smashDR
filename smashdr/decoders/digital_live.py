"""
digital_live.py - live DMR and Fusion (YSF) receivers for the main window.

    YsfLive(rate, offset_hz, decoder).process(iq) -> (48 kHz int16, [events])
    DmrLive(rate, offset_hz, decoder).process(iq) -> (48 kHz int16, [events])

Same shape as p25_live.P25Live: events are dicts with "type" start / update /
end plus "proto" and protocol fields. Both share the P25 C4FM front end
(channelizer + symbol timing) and a streaming symbol buffer with absolute
indices, so bursts / frames are cut exactly where their sync says.

DMR: voice is only decoded once the call's link control (voice LC header) is
known and says the call is clear; encrypted / unknown calls show metadata only.
"""

import numpy as np

from smashdr.core.dsp import Channelizer
from smashdr.decoders import dmr, ysf
from smashdr.decoders.p25 import C4FMDemod, CHAN_RATE
from smashdr.decoders.p25_live import Upsample6

CHAN_HALF_BW = 6_250.0
SYMBOL_RATE = 4800
END_TIMEOUT_S = 0.6


class _SymStream:
    """Channelizer + C4FM demod into a symbol buffer addressed by absolute index."""

    def __init__(self, rate, offset_hz):
        self.chan = Channelizer(rate, CHAN_RATE, CHAN_HALF_BW, offset_hz)
        self.demod = C4FMDemod()
        self.buf = np.zeros(0, dtype=np.float32)
        self.base = 0                    # absolute index of buf[0]
        self.searched = 0                # absolute index searched up to (sync starts)

    @property
    def end(self):
        return self.base + len(self.buf)

    def push(self, iq):
        s = self.demod.process(self.chan.process(iq))
        if len(s):
            self.buf = np.concatenate([self.buf, s.astype(np.float32)])

    def seg(self, a, b):
        return self.buf[a - self.base:b - self.base]

    def find(self, patterns: dict, need_after: int, thr=0.88):
        """Sync starts (abs index, name) found since the last call, sorted.
        A hit is only reported once need_after symbols after its start exist."""
        lo = max(self.searched, self.base)
        hi = self.end - need_after
        if hi - lo < 1:
            return []
        hits = []
        for name, s in patterns.items():
            L = len(s)
            z = self.seg(lo, hi + L - 1)
            if len(z) < L:
                continue
            sn = (s - s.mean()) / np.linalg.norm(s - s.mean())
            W = np.lib.stride_tricks.sliding_window_view(z, L)
            Wc = W - W.mean(1, keepdims=True)
            c = (Wc @ sn) / (np.linalg.norm(Wc, axis=1) + 1e-9)
            for k in np.flatnonzero(c > thr):
                hits.append((lo + int(k), float(c[k]), name))
        hits.sort()
        out = []
        for h in hits:
            if out and h[0] - out[-1][0] < 20:
                if h[1] > out[-1][1]:
                    out[-1] = h
                continue
            out.append(h)
        self.searched = hi
        return [(i, n) for i, _c, n in out]

    def fit(self, i, sync_syms):
        """Gain / offset from a known sync at absolute index i."""
        w = self.seg(i, i + len(sync_syms))
        A = np.vstack([sync_syms, np.ones(len(sync_syms))]).T
        g, o = np.linalg.lstsq(A, w, rcond=None)[0]
        return (g if abs(g) > 1e-3 else 1.0), o

    def trim(self, keep_from):
        cut = keep_from - self.base
        if cut > 0:
            self.buf = self.buf[cut:]
            self.base = keep_from


class _LiveBase:
    def __init__(self, rate, offset_hz, decoder=None):
        self.st = _SymStream(rate, offset_hz)
        self.dec = decoder
        self.up = Upsample6()
        self.t_sym = 0                    # symbols seen (clock)

    def set_offset(self, offset_hz):
        self.st.chan.set_offset(offset_hz)

    def t(self, i):
        return i / SYMBOL_RATE

    def _finish(self, pcm, events):
        audio8 = np.concatenate(pcm) if pcm else np.zeros(0, dtype=np.int16)
        return self.up(audio8), events


# --------------------------------------------------------------------------
class YsfLive(_LiveBase):
    def __init__(self, rate, offset_hz, decoder=None):
        super().__init__(rate, offset_hz, decoder)
        self.call = None

    def _snap(self, kind):
        c = self.call
        return {"type": kind, "proto": "YSF", "src": c["src"], "dst": c["dst"],
                "via": c["via"], "mode": c["mode"], "encrypted": False,
                "duration": c["last"] - c["start"]}

    def _end(self, events):
        if self.call is not None:
            events.append(self._snap("end"))
        self.call = None

    def process(self, iq):
        self.st.push(iq)
        events, pcm = [], []
        for i, _n in self.st.find({"sync": ysf.SYNC_SYMS}, ysf.FRAME_SYMS):
            self._frame(i, events, pcm)
            self.st.searched = max(self.st.searched, i + ysf.FRAME_SYMS - 30)
        now = self.t(self.st.end)
        if self.call is not None and now - self.call["last"] > END_TIMEOUT_S:
            self._end(events)
        self.st.trim(self.st.end - 2 * ysf.FRAME_SYMS)
        return self._finish(pcm, events)

    def _frame(self, i, events, pcm):
        g, o = self.st.fit(i, ysf.SYNC_SYMS)
        bits = ysf.bits_from_syms((self.st.seg(i, i + ysf.FRAME_SYMS) - o) / g)
        f = ysf.decode_fich(bits)
        if f is None:
            return
        t = self.t(i)
        if f["fi"] == 3:
            return
        if self.call is None or t - self.call["last"] > END_TIMEOUT_S:
            self._end(events)
            self.call = {"start": t, "last": t, "src": "", "dst": "", "via": "",
                         "mode": ysf.DT_NAMES.get(f["dt"], "?")}
            if self.dec is not None:
                self.dec.reset()
            events.append(self._snap("start"))
        c = self.call
        c["last"] = t + 0.1
        before = (c["src"], c["dst"], c["via"])
        if f["fi"] in (0, 2):
            csd = ysf.header_csd1(bits)
            if csd:
                c["dst"] = ysf.callsign(csd[0]) or c["dst"]
                c["src"] = ysf.callsign(csd[1]) or c["src"]
        elif f["fi"] == 1 and f["dt"] == 2:
            d = ysf.dn_dch(bits)
            if d:
                txt = ysf.callsign(d.decode("ascii", "replace"))
                if f["fn"] == 0 and txt:
                    c["dst"] = txt
                elif f["fn"] == 1 and txt:
                    c["src"] = txt
                elif f["fn"] in (2, 3) and txt:
                    c["via"] = txt
            if self.dec is not None:
                for p in ysf.dn_voice(bits):
                    a, _r = self.dec.decode_params(p)
                    pcm.append(a)
        if (c["src"], c["dst"], c["via"]) != before:
            events.append(self._snap("update"))
        if f["fi"] == 2:
            self._end(events)


# --------------------------------------------------------------------------
class DmrLive(_LiveBase):
    SLOT_SYMS = dmr.SYMS_PER_SLOT          # 144

    def __init__(self, rate, offset_hz, decoder=None):
        super().__init__(rate, offset_hz, decoder)
        self.calls = {1: None, 2: None}
        self.audio_slot = None
        self.pending = []                  # (abs sync index, gain, offset, slot)
        self.done = set()
        self.cc = None
        self._csbk = {}                    # colour code -> CSBK count this second
        self._csbk_t = 0.0

    def _snap(self, kind, slot):
        c = self.calls[slot]
        return {"type": kind, "proto": "DMR", "slot": slot, "cc": self.cc,
                "tgid": c["tgid"] if c["flco"] == "group" else None,
                "dst": c["tgid"] if c["flco"] != "group" else None,
                "src": c["src"], "encrypted": c["encrypted"],
                "emergency": c.get("emergency", False),
                "duration": c["last"] - c["start"]}

    def _start(self, slot, t, events):
        self.calls[slot] = {"start": t, "last": t, "tgid": None, "src": None,
                            "flco": "group", "encrypted": None}
        if self.audio_slot is None:
            self.audio_slot = slot
            if self.dec is not None:
                self.dec.reset()
        if slot == self.audio_slot:
            events.append(self._snap("start", slot))

    def _end(self, slot, events):
        if self.calls[slot] is not None and slot == self.audio_slot:
            events.append(self._snap("end", slot))
        self.calls[slot] = None
        if slot == self.audio_slot:
            self.audio_slot = None

    def _set_lc(self, slot, lc, t, events):
        p = dmr.parse_lc(lc)
        c = self.calls[slot]
        if c is None:
            self._start(slot, t, events)
            c = self.calls[slot]
        before = (c["tgid"], c["src"], c["encrypted"])
        c.update(tgid=p["dst"], src=p["src"], flco=p["flco"], encrypted=p["encrypted"],
                 emergency=p["emergency"])
        if (c["tgid"], c["src"], c["encrypted"]) != before and slot == self.audio_slot:
            events.append(self._snap("update", slot))

    def process(self, iq):
        st = self.st
        st.push(iq)
        events, pcm = [], []
        pats = {"v": dmr.SYNC_SYMS["bs_voice"], "d": dmr.SYNC_SYMS["bs_data"]}
        for i, kind in st.find(pats, 78):
            if i - 66 < st.base:
                continue
            g, o = st.fit(i, pats[kind])
            seg = (st.seg(i - 66, i + 78) - o) / g
            b = dmr.bits_from_syms(seg)
            cach, burst = b[:24], b[24:]
            slot = int(cach[4]) + 1
            t = self.t(i)
            if kind == "d":
                cc, dt = dmr.slot_type(burst)
                if dt == 3:                             # CSBK: control signalling
                    self._csbk[cc] = self._csbk.get(cc, 0) + 1
                if dt in (0, 1, 2):
                    if dt == 0:                         # privacy header: encrypted call
                        if self.calls[slot] is None:
                            self._start(slot, t, events)
                        self.calls[slot]["encrypted"] = True
                        self.calls[slot]["last"] = t
                        continue
                    data, _fixed, _ok = dmr.bptc196_decode(burst)
                    lc, _corr = dmr.lc_check(np.packbits(data).tobytes(), dt)
                    if lc is None:
                        continue
                    self.cc = cc
                    if dt == 1:
                        self._set_lc(slot, lc, t, events)
                        self.calls[slot]["last"] = t
                    else:
                        if self.calls[slot] is not None:
                            self._set_lc(slot, lc, t, events)
                            self.calls[slot]["last"] = t
                            self._end(slot, events)
            else:
                c = self.calls[slot]
                if c is None:
                    self._start(slot, t, events)            # late entry: no LC yet
                    c = self.calls[slot]
                c["last"] = t + 0.36
                if slot == self.audio_slot and c["encrypted"] is False:
                    for k in range(6):
                        j = i + 2 * self.SLOT_SYMS * k
                        if j not in self.done:
                            self.pending.append((j, g, o, slot))
                            self.done.add(j)
        # voice bursts whose symbols are in
        keep = []
        for j, g, o, slot in self.pending:
            if j + 78 > st.end:
                keep.append((j, g, o, slot))
                continue
            if j - 54 >= st.base and self.dec is not None and slot == self.audio_slot:
                burst = dmr.bits_from_syms((st.seg(j - 54, j + 78) - o) / g)
                for fr in dmr.ambe_frames(burst):
                    a, _r = self.dec.decode_frame(fr)
                    pcm.append(a)
        self.pending = keep
        now = self.t(st.end)
        if now - self._csbk_t >= 1.0:
            n = sum(self._csbk.values())
            # a control channel sends CSBKs nonstop (~33/s); a voice repeater only a few
            if n >= 10 and all(c is None for c in self.calls.values()):
                cc = max(self._csbk, key=self._csbk.get)
                events.append({"type": "cc", "proto": "DMR", "cc": cc,
                               "csbk_per_s": n / (now - self._csbk_t)})
            self._csbk = {}
            self._csbk_t = now
        for slot in (1, 2):
            c = self.calls[slot]
            if c is not None and now - c["last"] > END_TIMEOUT_S:
                self._end(slot, events)
        if len(self.done) > 500:
            self.done = {j for j in self.done if j > st.end - 4000}
        st.trim(min([st.end - 600] + [j - 70 for j, *_ in self.pending]))
        return self._finish(pcm, events)

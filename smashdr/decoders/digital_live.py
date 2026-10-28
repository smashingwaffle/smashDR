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
from smashdr.decoders import dmr, dstar, ysf
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


# --------------------------------------------------------------------------
class DstarLive(_LiveBase):
    """D-STAR (GMSK): radio header, voice superframes, slow-data header / message.
    The C4FM front end works for GMSK too (2 levels instead of 4: sign = bit)."""

    def __init__(self, rate, offset_hz, decoder=None):
        super().__init__(rate, offset_hz, decoder)
        sym = lambda s: dstar.bits_of(s).astype(np.float32) * 2 - 1
        self.pats = {"v": sym(dstar.VOICE_SYNC),
                     "h": sym("1010101010101010" + dstar.FRAME_SYNC),
                     "e": sym("1010101010101010" + dstar.EOT)}
        self.call = None
        self.pending = []                 # (kind, abs pos, gain, offset, extra)
        self.done = set()
        self.next_sync = None             # expected next voice sync (abs index)
        self.misses = 0
        self.slow = dstar.SlowData()
        self.sf = {}                      # sync index -> {k: 3 slow bytes}

    def _snap(self, kind):
        c = self.call
        return {"type": kind, "proto": "DSTAR", "my": c["my"], "sfx": c["sfx"], "ur": c["ur"],
                "rpt1": c["rpt1"], "rpt2": c["rpt2"], "msg": c["msg"], "src": c["my"] or None,
                "encrypted": False, "duration": c["last"] - c["start"]}

    def _start(self, t, events):
        if self.call is None:
            self.call = {"start": t, "last": t, "my": "", "sfx": "", "ur": "", "rpt1": "",
                         "rpt2": "", "msg": ""}
            self.slow = dstar.SlowData()
            if self.dec is not None:
                self.dec.reset()
            events.append(self._snap("start"))

    def _end(self, events):
        if self.call is not None:
            events.append(self._snap("end"))
        self.call = None
        self.next_sync = None
        self.misses = 0

    def _info(self, h, events):
        c = self.call
        before = (c["my"], c["sfx"], c["ur"], c["rpt1"], c["rpt2"], c["msg"])
        for k in ("my", "sfx", "ur", "rpt1", "rpt2"):
            if k in h and h[k] is not None:
                c[k] = h[k]
        if "msg" in h:
            c["msg"] = h["msg"]
        if (c["my"], c["sfx"], c["ur"], c["rpt1"], c["rpt2"], c["msg"]) != before:
            events.append(self._snap("update"))

    def _sync(self, i, g, o, t, events):
        self._start(t, events)
        self.call["last"] = t + 0.42
        self.next_sync = i + dstar.SUPERFRAME
        self.misses = 0
        self.sf[i] = {}
        for k in range(21):                           # voice of frames 1..20 + next frame 0
            j = i + 24 + 96 * k
            if j not in self.done:
                self.done.add(j)
                self.pending.append(("voice", j, g, o, None))
            if 1 <= k <= 20:
                self.pending.append(("slow", i + 96 * k, g, o, (i, k)))

    def process(self, iq):
        st = self.st
        st.push(iq)
        events, pcm = [], []
        for i, kind in st.find(self.pats, 24, thr=0.85):
            t = self.t(i)
            g, o = st.fit(i, self.pats[kind])
            if kind == "v":
                if self.next_sync is not None and abs(i - self.next_sync) <= 4:
                    i = self.next_sync if abs(i - self.next_sync) > 1 else i
                self._sync(i, g, o, t, events)
            elif kind == "h":
                self.pending.append(("header", i + len(self.pats["h"]), g, o, None))
            elif kind == "e" and self.call is not None:
                self._end(events)
        # a missed voice sync: carry on where it should have been
        if (self.call is not None and self.next_sync is not None
                and st.searched > self.next_sync + 30):
            self.misses += 1
            if self.misses > 3:
                self._end(events)
            else:
                ns = self.next_sync
                prev = [p for p in self.pending if p[0] == "voice"]
                g, o = (prev[-1][2], prev[-1][3]) if prev else (1.0, 0.0)
                m = self.misses
                self._sync(ns, g, o, self.t(ns), events)
                self.misses = m
        keep = []
        for kind, j, g, o, extra in self.pending:
            n = {"voice": 72, "slow": 24, "header": dstar.HEADER_BITS}[kind]
            if j + n > st.end:
                keep.append((kind, j, g, o, extra))
                continue
            if j < st.base:
                continue
            bits = ((st.seg(j, j + n) - o) / g > 0).astype(np.uint8)
            if kind == "voice":
                if self.call is not None and self.dec is not None:
                    a, _r = self.dec.decode_dstar(dstar.ambe_frame(bits))
                    pcm.append(a)
            elif kind == "slow":
                i0, k = extra
                sf = self.sf.setdefault(i0, {})
                sf[k] = dstar.slow_bytes(bits)
                if len(sf) == 20:
                    info = self.slow.feed_superframe(b"".join(sf[x] for x in range(1, 21)))
                    del self.sf[i0]
                    if self.call is not None:
                        if "header" in info:
                            self._info(info["header"], events)
                        if "message" in info:
                            self._info({"msg": info["message"]}, events)
            elif kind == "header":
                h = dstar.decode_header(bits)
                if h:
                    self._end(events)                 # a header always starts a new call
                    self._start(self.t(j), events)
                    self.call["last"] = self.t(j) + 0.5
                    self.next_sync = None
                    self._info(h, events)
                    # voice frames follow the header directly; frame 0's sync comes 2016 bits on
                    start = j + dstar.HEADER_BITS
                    for k in range(21):
                        p = start + 96 * k
                        if p not in self.done:
                            self.done.add(p)
                            keep.append(("voice", p, g, o, None))
        self.pending = keep
        if len(self.sf) > 4:
            for k in sorted(self.sf)[:-4]:
                del self.sf[k]
        now = self.t(st.end)
        if self.call is not None and now - self.call["last"] > END_TIMEOUT_S:
            self._end(events)
        if len(self.done) > 2000:
            self.done = {j for j in self.done if j > st.end - 5000}
        st.trim(min([st.end - 700] + [j - 40 for _k, j, *_r in self.pending]))
        return self._finish(pcm, events)

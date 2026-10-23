"""
p25_live.py - streaming P25 Phase 1 for the live receiver.

    P25Live.process(iq) -> (48 kHz int16 audio, [events])

Call state is tracked across frames: a call starts at an HDU or the first
LDU after a gap, and ends at a TDU/TDULC or after a gap. Voice is only ever
decoded for clear calls (ALGID 0x80). If a call is picked up mid-stream, its
voice frames are held until the encryption sync (LDU2) or header says the
call is clear; encrypted calls are reported but never decoded.

Events (dicts): {"type": "start" | "update" | "end", "tgid", "src", "dst",
"algid", "encrypted", "nac", "emergency", "duration"}
plus, once a second while a control channel is being received,
{"type": "cc", "nac", "tsdu_per_s"}.
"""

import numpy as np

import p25
from dsp import StreamFir, lowpass_taps

GAP_S = 0.6                 # silence that separates transmissions
END_TIMEOUT_S = 1.0         # end a call if no frames arrive for this long
MAX_PENDING_LDUS = 2        # voice held while the ALGID is still unknown


class Upsample6:
    """8 kHz -> 48 kHz (zero-stuff x6 + 3.6 kHz lowpass), state kept across blocks."""

    def __init__(self):
        self.fir = StreamFir(lowpass_taps(127, 3600 / 48_000), np.float32)

    def __call__(self, x8: np.ndarray) -> np.ndarray:
        if len(x8) == 0:
            return np.zeros(0, dtype=np.int16)
        up = np.zeros(len(x8) * 6, dtype=np.float32)
        up[::6] = x8.astype(np.float32) * 6.0
        y = self.fir(up)
        return np.clip(y, -32768, 32767).astype(np.int16)


class P25Live:
    def __init__(self, rate: float, offset_hz: float, decoder=None):
        self.rx = p25.P25Phase1Receiver(rate, offset_hz)
        self.rate = float(rate)
        self.dec = decoder                  # vocoder.ImbeDecoder or None (metadata only)
        self.up = Upsample6()
        self.t_now = 0.0
        self.call = None
        self.pending = []                   # IMBE frames waiting for the ALGID
        self._cc_count = 0
        self._cc_nac = None
        self._cc_t = 0.0

    def set_offset(self, offset_hz: float):
        self.rx.chan.set_offset(offset_hz)

    # ------------------------------------------------------------------
    def process(self, iq: np.ndarray):
        self.t_now += len(iq) / self.rate
        frames = self.rx.process(iq)
        events, pcm = [], []
        for f in frames:
            self._handle(f, events, pcm)
        if self.call is not None and self.t_now - self.call["last"] > END_TIMEOUT_S + 0.5:
            self._end(events)
        if self.t_now - self._cc_t >= 1.0:
            if self._cc_count:
                events.append({"type": "cc", "nac": self._cc_nac,
                               "tsdu_per_s": self._cc_count / (self.t_now - self._cc_t)})
            self._cc_count = 0
            self._cc_t = self.t_now
        audio8 = np.concatenate(pcm) if pcm else np.zeros(0, dtype=np.int16)
        return self.up(audio8), events

    # ------------------------------------------------------------------
    def _snapshot(self, kind):
        c = self.call
        return {"type": kind, "tgid": c["tgid"], "src": c["src"], "dst": c["dst"],
                "algid": c["algid"], "encrypted": c["encrypted"], "nac": c["nac"],
                "emergency": c["emergency"], "duration": c["last"] - c["start"]}

    def _end(self, events):
        if self.call is not None:
            events.append(self._snapshot("end"))
        self.call = None
        self.pending = []

    def _decode(self, frames, pcm):
        if self.dec is None:
            return
        for fr in frames:
            a, _r = self.dec.decode(fr)
            pcm.append(a)

    def _handle(self, f, events, pcm):
        d = f["duid"]
        if d not in (0x0, 0x5, 0xA, 0x3, 0xF):
            if d == 0x7:                                  # trunking control channel
                self._cc_count += 1
                self._cc_nac = f["nac"]
            return
        if self.call is None or d == 0x0 or f["t"] - self.call["last"] > GAP_S:
            if d in (0x3, 0xF) and self.call is None:
                return                                   # stray terminator
            self._end(events)
            self.call = {"start": f["t"], "last": f["t"], "nac": f["nac"], "tgid": None,
                         "src": None, "dst": None, "algid": None, "encrypted": None,
                         "emergency": False}
            if self.dec is not None:
                self.dec.reset()
            events.append(self._snapshot("start"))
        c = self.call
        c["last"] = f["t"] + p25.FRAME_DIBITS.get(d, p25.NID_ONLY_DIBITS) / p25.SYMBOL_RATE
        before = (c["tgid"], c["src"], c["dst"], c["algid"], c["emergency"])

        if d == 0x0 and f.get("rs_errors", -1) >= 0:
            c["tgid"], c["algid"] = f["tgid"], f["algid"]
        elif d in (0x5, 0xA):
            if f.get("kind") == "group":
                c["tgid"], c["src"] = f["tgid"], f["src"]
                c["emergency"] |= f.get("emergency", False)
            elif f.get("kind") == "unit":
                c["dst"], c["src"] = f["dst"], f["src"]
            if "algid" in f:
                c["algid"] = f["algid"]
        if c["algid"] is not None:
            c["encrypted"] = c["algid"] != 0x80

        if "imbe" in f:
            if c["encrypted"] is None:
                self.pending.extend(f["imbe"])
                self.pending = self.pending[-9 * MAX_PENDING_LDUS:]
            elif c["encrypted"]:
                self.pending = []                         # never decoded
            else:
                if self.pending:
                    self._decode(self.pending, pcm)
                    self.pending = []
                self._decode(f["imbe"], pcm)

        if (c["tgid"], c["src"], c["dst"], c["algid"], c["emergency"]) != before:
            events.append(self._snapshot("update"))
        if d in (0x3, 0xF):
            self._end(events)

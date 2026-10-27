"""
p25_trunk.py - P25 mode with trunk following inside the captured span ("option B").

    P25Trunk(rate, offset_hz, center_hz, vocoder_factory).process(iq) -> (audio, events)

The tuned channel runs a normal P25Live. If it turns out to be a control
channel, every voice grant whose frequency lies inside the SDR span gets its
own P25Live (own channelizer + vocoder) on the same IQ, so several calls are
followed at once without retuning. One call at a time owns the audio (the
first clear one); the others are still decoded for their metadata.

Never decodes encrypted voice: P25Live itself drops it, and talkgroups seen
encrypted (grant flag or ALGID) are remembered and not followed again.

Events: the P25Live events of the tuned channel; the owner's call events with
"freq" and "trunk": True; and a "cc" event (once a second, from P25Live) with
each active grant annotated: "state" = playing / clear / encrypted /
checking / phase 2 / out of span / busy / no plan.
"""

import numpy as np

from smashdr.decoders.p25_live import P25Live

MAX_VOICES = 3              # extra channelizers cost CPU: keep it small
EDGE_MARGIN_HZ = 15_000     # a voice channel must sit this far inside the span
GRANT_HOLD_S = 3.0          # a grant counts as active this long after its last update
IDLE_DROP_S = 2.0           # drop a voice decoder this long after its call and grant end


class P25Trunk:
    def __init__(self, rate: float, offset_hz: float, center_hz: float, vocoder_factory=None):
        self.rate = float(rate)
        self.center = float(center_hz)
        self.offset = float(offset_hz)
        self.factory = vocoder_factory or (lambda: None)
        self.cc = P25Live(rate, offset_hz, self.factory())
        self.voices = {}                    # freq -> {"rx": P25Live, "tg": int, "t": cc time}
        self.owner = None                   # freq whose audio is played
        self.enc_tgs = set()                # talkgroups seen encrypted
        self.clear_tgs = set()

    # ------------------------------------------------------------------
    def set_offset(self, offset_hz: float, center_hz: float = None):
        if center_hz is None:
            center_hz = self.center
        if abs((center_hz + offset_hz) - (self.center + self.offset)) > 1000:
            # another channel: forget this control channel's system and grants
            self.cc = P25Live(self.rate, offset_hz, self.factory())
            self.enc_tgs, self.clear_tgs = set(), set()
            self._drop_all()
        elif center_hz != self.center:
            self._drop_all()                # voice offsets are relative to the old centre
        self.center = float(center_hz)
        self.offset = float(offset_hz)
        self.cc.set_offset(offset_hz)

    def _drop_all(self):
        self.voices = {}
        self.owner = None

    def _cc_freq(self):
        return self.center + self.offset

    def _in_span(self, f):
        return abs(f - self.center) < self.rate / 2 - EDGE_MARGIN_HZ

    # ------------------------------------------------------------------
    def process(self, iq: np.ndarray):
        audio, events = self.cc.process(iq)
        is_cc = bool(self.cc.grants) or self.cc._cc_nac is not None
        out_pcm = audio
        if is_cc:
            self._follow()
            out_pcm = np.zeros(0, dtype=np.int16)      # a control channel has no voice
            for f, v in list(self.voices.items()):
                pcm, evs = v["rx"].process(iq)
                for ev in evs:
                    self._voice_event(f, v, ev, events)
                if f == self.owner and len(pcm):
                    out_pcm = pcm
            self._pick_owner(events)
            self._reap()
        for ev in events:
            if ev["type"] == "cc":
                self._annotate(ev)
        return out_pcm, events

    def _active_grants(self):
        now = self.cc.t_now
        return [g for g in self.cc.grants.values() if now - g["t"] <= GRANT_HOLD_S]

    def _follow(self):
        for g in self._active_grants():
            f = g.get("freq")
            if (f is None or g.get("slot") is not None or not self._in_span(f)
                    or abs(f - self._cc_freq()) < 1000 or g.get("encrypted")
                    or g["tgid"] in self.enc_tgs):
                continue
            v = self.voices.get(f)
            if v is None:
                if len(self.voices) >= MAX_VOICES:
                    continue
                try:
                    rx = P25Live(self.rate, f - self.center, self.factory())
                except ValueError:
                    continue
                v = self.voices[f] = {"rx": rx, "tg": g["tgid"], "t": g["t"], "meta": None}
            v["tg"], v["t"] = g["tgid"], g["t"]

    def _voice_event(self, f, v, ev, events):
        ev = dict(ev, freq=f, trunk=True)
        if ev.get("tgid") is None:
            ev["tgid"] = v["tg"]
        if ev.get("encrypted") is True:
            self.enc_tgs.add(ev["tgid"])
        elif ev.get("encrypted") is False:
            self.clear_tgs.add(ev["tgid"])
        if ev["type"] == "end":
            v["meta"] = None
        else:
            v["meta"] = ev
        if f == self.owner:
            events.append(ev)
            if ev["type"] == "end":
                self.owner = None
        elif self.owner is None and ev["type"] != "end" and ev.get("encrypted") is False:
            self.owner = f
            events.append(dict(ev, type="start"))

    def _pick_owner(self, events):
        if self.owner is not None and self.owner not in self.voices:
            self.owner = None
        if self.owner is not None:
            return
        for f, v in self.voices.items():
            m = v["meta"]
            if m is not None and m.get("encrypted") is False and v["rx"].call is not None:
                self.owner = f
                events.append(dict(m, type="start"))
                return

    def _reap(self):
        now = self.cc.t_now
        for f, v in list(self.voices.items()):
            idle = v["rx"].call is None
            enc = v["tg"] in self.enc_tgs
            if (idle or enc) and now - v["t"] > IDLE_DROP_S:
                if f == self.owner:
                    self.owner = None
                del self.voices[f]

    def _annotate(self, ev):
        ev["trunk"] = True
        for g in ev.get("active", []):
            f = g.get("freq")
            if f is None:
                g["state"] = "no plan"
            elif g.get("slot") is not None:
                g["state"] = "phase 2"
            elif g.get("encrypted") or g["tgid"] in self.enc_tgs:
                g["state"] = "encrypted"
            elif not self._in_span(f):
                g["state"] = "out of span"
            elif f == self.owner:
                g["state"] = "playing"
            elif f in self.voices:
                g["state"] = "clear" if g["tgid"] in self.clear_tgs else "checking"
            else:
                g["state"] = "busy"

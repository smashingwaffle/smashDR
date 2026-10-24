"""
transcribe.py - speech-to-text for finished calls (faster-whisper, local, CPU).

    t = Transcriber(on_result, model="small.en")
    t.submit(pcm_48k_int16, meta)   # returns at once; work is queued
    t.set_model("large-v3-turbo")   # takes effect on the next call

A model loads on first use (first run downloads it from Hugging Face into the
usual cache: small.en ~480 MB, large-v3-turbo ~1.6 GB). Nothing leaves the
machine after that. Needs: pip install faster-whisper
"""

import queue
import re
import threading
import time

import numpy as np

# label shown in Settings -> faster-whisper model name
MODELS = {
    "Fast (small.en)": "small.en",
    "Accurate (large-v3-turbo)": "large-v3-turbo",
}
DEFAULT_MODEL = "small.en"
MIN_SECONDS = 0.8           # skip blips / key-ups
PAD_S = 0.3                 # silence around the call helps Whisper start/stop cleanly

# Context prompt: Whisper continues "in the style" of this text.
PROMPT = ("LAPD Hollenbeck dispatch. 4A27, 4A15, 4L30, 4X2, Air 10, Drone 1. "
          "Adam, Boy, Charles, David, Edward, Frank, George, Henry, Ida, John, King, "
          "Lincoln, Mary, Nora, Ocean, Paul, Robert, Sam, Tom, Victor, William, X-ray, Zebra. "
          "Code 3, Code 4, Code 6, Code 7, 415, 211, 245, 459, 10-4, roger, copy, show me, "
          "en route, PR, suspect, vehicle, 2315 Cesar Chavez Avenue, Soto Street, "
          "Whittier Boulevard, 1st Street, 4th Street, Olympic, Lorena, Indiana, State Street, "
          "Mission Road, Marengo, Valley Boulevard, Huntington Drive, Eastern Avenue, "
          "Boyle Heights, Lincoln Heights, El Sereno.")
# Hotwords: pushed harder than the prompt (faster-whisper >= 1.0.2; ignored if older).
HOTWORDS = ("Hollenbeck Code 6 Code 4 Code 3 Drone Air 10 Cesar Chavez Soto Whittier "
            "Boyle Heights Lincoln Heights El Sereno roger copy en route show me")

# Typical Whisper inventions on noise, tones or silence.
_JUNK = re.compile(
    r"^(thank(s| you)( (so much|very much))?( for watching| for listening)?|"
    r"please subscribe.*|subtitles by.*|you|bye|\.+|okay)[.!]?$", re.I)


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def clean(segments) -> str:
    """Join segments, dropping likely hallucinations."""
    out = []
    for s in segments:
        t = s.text.strip()
        if not t:
            continue
        # Whisper's own "probably not speech" signal
        if getattr(s, "no_speech_prob", 0) > 0.6 and getattr(s, "avg_logprob", 0) < -1.0:
            continue
        if _JUNK.match(t):
            continue
        if len(t) > 20 and t.lower().rstrip(".") in PROMPT.lower():
            continue                                  # prompt echoed back
        if out and t == out[-1]:
            continue                                  # repeated segment loop
        out.append(t)
    return " ".join(out).strip()


class Transcriber:
    def __init__(self, on_result, model: str = DEFAULT_MODEL):
        self.on_result = on_result
        self.model_name = model if model in MODELS.values() else DEFAULT_MODEL
        self._q = queue.Queue(maxsize=50)
        self._model = None
        self._loaded_name = None
        self._hotwords_ok = True
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()

    def set_model(self, name: str):
        if name in MODELS.values():
            self.model_name = name

    def submit(self, pcm48: np.ndarray, meta: dict):
        if len(pcm48) < MIN_SECONDS * 48_000:
            return
        try:
            self._q.put_nowait((pcm48, dict(meta)))
        except queue.Full:
            print("[!] Transcriber queue full, call skipped")

    def stop(self):
        self._q.put(None)

    # ------------------------------------------------------------------
    def _load(self):
        from faster_whisper import WhisperModel
        name = self.model_name
        t0 = time.time()
        print(f"[i] Loading Whisper '{name}' (first time downloads it)...")
        self._model = None
        self._model = WhisperModel(name, device="cpu", compute_type="int8")
        self._loaded_name = name
        print(f"[i] Whisper '{name}' ready in {time.time() - t0:.1f} s")

    def _transcribe(self, x):
        kw = dict(language="en", beam_size=5, initial_prompt=PROMPT,
                  condition_on_previous_text=False, vad_filter=False,
                  temperature=0.0)
        if self._hotwords_ok:
            try:
                segs, _info = self._model.transcribe(x, hotwords=HOTWORDS, **kw)
                return list(segs)
            except TypeError:
                self._hotwords_ok = False             # older faster-whisper
                print("[i] This faster-whisper has no hotwords; using the prompt only")
        segs, _info = self._model.transcribe(x, **kw)
        return list(segs)

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            pcm48, meta = item
            try:
                if self._model is None or self._loaded_name != self.model_name:
                    self._load()
                # 48 kHz -> 16 kHz: the P25 audio is already band-limited to 3.6 kHz
                x = pcm48[::3].astype(np.float32) / 32768.0
                x -= float(np.mean(x)) if len(x) else 0.0
                peak = float(np.max(np.abs(x))) if len(x) else 0.0
                if peak > 0:
                    x *= 0.9 / peak
                pad = np.zeros(int(PAD_S * 16_000), dtype=np.float32)
                x = np.concatenate([pad, x, pad])
                t0 = time.time()
                text = clean(self._transcribe(x))
                meta.update(text=text, audio_s=len(pcm48) / 48_000,
                            took_s=time.time() - t0, model=self._loaded_name, error=None)
            except Exception as e:                       # never take the radio down
                meta.update(text="", error=f"{type(e).__name__}: {e}")
            self.on_result(meta)

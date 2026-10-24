"""
transcribe.py - speech-to-text for finished calls (faster-whisper, local, CPU).

    t = Transcriber(on_result)      # on_result(dict) is called from the worker thread
    t.submit(pcm_48k_int16, meta)   # returns at once; work is queued

The model loads on first use (first run downloads it, ~250-500 MB, from
Hugging Face into the usual cache). Nothing leaves the machine after that.
Needs: pip install faster-whisper
"""

import queue
import threading
import time

import numpy as np

MODEL = "small.en"          # "base.en" is lighter/faster, less accurate
MIN_SECONDS = 0.8           # skip blips / key-ups
# Vocabulary hints: Whisper leans towards these spellings.
PROMPT = ("LAPD radio dispatch, Hollenbeck, Northeast, Central, Rampart, Newton. "
          "Unit 4A23, 4L10, 11A45. Code 3, Code 4, Code 6, Code 7, 415, 211, 459, 245, "
          "10-4, roger, copy, en route, show me, handle, PR, suspect, vehicle, "
          "Boyle Heights, Lincoln Heights, Highland Park, El Sereno, Eagle Rock, "
          "Cesar Chavez, Soto, Huntington Drive, Figueroa, York Boulevard.")


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


class Transcriber:
    def __init__(self, on_result, model: str = MODEL):
        self.on_result = on_result
        self.model_name = model
        self._q = queue.Queue(maxsize=50)
        self._model = None
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()

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
        t0 = time.time()
        print(f"[i] Loading Whisper '{self.model_name}' (first time downloads it)...")
        self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        print(f"[i] Whisper ready in {time.time() - t0:.1f} s")

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            pcm48, meta = item
            try:
                if self._model is None:
                    self._load()
                # 48 kHz -> 16 kHz: the P25 audio is already band-limited to 3.6 kHz
                x = pcm48[::3].astype(np.float32) / 32768.0
                peak = float(np.max(np.abs(x))) if len(x) else 0.0
                if peak > 0:
                    x *= 0.9 / peak
                t0 = time.time()
                segments, _info = self._model.transcribe(
                    x, language="en", beam_size=5, initial_prompt=PROMPT,
                    condition_on_previous_text=False, vad_filter=False)
                text = " ".join(s.text.strip() for s in segments).strip()
                meta.update(text=text, audio_s=len(pcm48) / 48_000,
                            took_s=time.time() - t0, error=None)
            except Exception as e:                       # never take the radio down
                meta.update(text="", error=f"{type(e).__name__}: {e}")
            self.on_result(meta)

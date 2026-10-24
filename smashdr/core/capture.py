"""capture.py - gap-free RTL-SDR streaming (rtlsdr_read_async -> queue)."""

import queue
import threading

import numpy as np


class AsyncCapture(threading.Thread):
    """
    Streams from librtlsdr with rtlsdr_read_async and does nothing but copy
    each USB block into a queue. Blocking read_bytes() in the processing loop
    lost samples between calls (clicks every few blocks); this doesn't.
    """

    def __init__(self, sdr, nbytes: int, q: queue.Queue):
        super().__init__(name="rtlsdr-capture", daemon=True)
        self.sdr = sdr
        self.nbytes = nbytes
        self.q = q
        self.dropped = 0      # blocks thrown away because processing fell behind
        self.error = None

    def _cb(self, buf, _ctx):
        try:
            data = np.frombuffer(buf, dtype=np.uint8).tobytes()
        except TypeError:
            data = bytes(bytearray(buf))
        try:
            self.q.put_nowait(data)
        except queue.Full:
            self.dropped += 1

    def run(self):
        try:
            self.sdr.read_bytes_async(self._cb, self.nbytes)
        except Exception as e:
            self.error = e

    def stop(self):
        try:
            self.sdr.cancel_read_async()
        except Exception:
            pass

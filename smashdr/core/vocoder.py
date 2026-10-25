"""
vocoder.py - IMBE (P25 Phase 1) and AMBE+2 (DMR, Fusion DN) voice decoding
through mbelib-neo, via ctypes.

mbelib-neo (https://github.com/arancormonk/mbelib-neo, GPL-2.0-or-later) is
loaded at runtime from mbe-neo.dll / libmbe-neo.so placed next to smashDR or
in lib/. It does the IMBE FEC (Golay/Hamming + PN descrambling) and speech
synthesis: 160 samples at 8 kHz per 20 ms frame.

Only frames from clear (ALGID 0x80) calls are ever passed in.
"""

import ctypes
import os
import sys
from pathlib import Path

import numpy as np

from smashdr.paths import LIB_DIRS
LIB_NAMES = ["mbe-neo.dll", "libmbe-neo.so", "libmbe-neo.so.2", "libmbe-neo.dylib"]
PARMS_BYTES = 8192            # sizeof(mbe_parms) is 2604 in 2.6.x; leave headroom
SAMPLES_PER_FRAME = 160       # 20 ms at 8 kHz
RATE = 8000


class _Result(ctypes.Structure):
    _fields_ = [("c0_errors", ctypes.c_int), ("protected_errors", ctypes.c_int),
                ("c4_errors", ctypes.c_int), ("total_errors", ctypes.c_int),
                ("flags", ctypes.c_uint)]


FLAG_MUTE = 0x0080
FLAG_REPEAT = 0x0040


def find_library(extra_dirs=()):
    dirs = [*map(Path, extra_dirs), *LIB_DIRS]
    env = os.environ.get("SMASHDR_MBELIB")
    if env:
        return Path(env)
    for d in dirs:
        for n in LIB_NAMES:
            if (d / n).exists():
                return d / n
    return None


def load_library(path=None):
    path = Path(path) if path else find_library()
    if path is None:
        raise OSError("mbelib-neo not found: put mbe-neo.dll (Windows) next to smashDR or in lib/")
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(path.parent))
    lib = ctypes.CDLL(str(path))
    fr_t = ctypes.c_char * 23 * 8
    lib.mbe_initMbeParms.argtypes = [ctypes.c_void_p] * 3
    lib.mbe_initMbeParms.restype = None
    lib.mbe_processImbe7200x4400Frame.argtypes = [
        ctypes.POINTER(ctypes.c_short), ctypes.POINTER(_Result), ctypes.POINTER(fr_t),
        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.mbe_processImbe7200x4400Frame.restype = ctypes.c_int
    # AMBE+2 (DMR, Fusion DN): 3600x2450 with FEC (4x24 bitplanes) and raw 49-bit params
    try:
        amb_t = ctypes.c_char * 24 * 4
        lib.mbe_processAmbe3600x2450Frame.argtypes = [
            ctypes.POINTER(ctypes.c_short), ctypes.POINTER(_Result), ctypes.POINTER(amb_t),
            ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.mbe_processAmbe3600x2450Frame.restype = ctypes.c_int
        lib.mbe_processAmbe2450Data.argtypes = [
            ctypes.POINTER(ctypes.c_short), ctypes.POINTER(_Result), ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.mbe_processAmbe2450Data.restype = ctypes.c_int
    except AttributeError:
        pass                                   # old library: IMBE only
    return lib, path


class ImbeDecoder:
    """Stateful IMBE 7200x4400 decoder (one per call / voice stream)."""

    def __init__(self, lib=None):
        self.lib, self.path = (lib, None) if lib is not None else load_library()
        self._cur = ctypes.create_string_buffer(PARMS_BYTES)
        self._prev = ctypes.create_string_buffer(PARMS_BYTES)
        self._prev_enh = ctypes.create_string_buffer(PARMS_BYTES)
        self._fr = (ctypes.c_char * 23 * 8)()
        self._d = ctypes.create_string_buffer(88)
        self._out = (ctypes.c_short * SAMPLES_PER_FRAME)()
        self._res = _Result()
        self.reset()

    def reset(self):
        self.lib.mbe_initMbeParms(self._cur, self._prev, self._prev_enh)

    def decode(self, imbe_fr: np.ndarray):
        """imbe_fr: (8, 23) array of 0/1 -> (int16[160] audio, result dict)."""
        flat = bytes(np.ascontiguousarray(imbe_fr, dtype=np.uint8).ravel())
        ctypes.memmove(self._fr, flat, 8 * 23)
        rc = self.lib.mbe_processImbe7200x4400Frame(
            self._out, ctypes.byref(self._res), ctypes.byref(self._fr), self._d,
            self._cur, self._prev, self._prev_enh)
        pcm = np.ctypeslib.as_array(self._out).copy()
        r = self._res
        return pcm, {"rc": rc, "c0": r.c0_errors, "protected": r.protected_errors,
                     "c4": r.c4_errors, "total": r.total_errors,
                     "muted": bool(r.flags & FLAG_MUTE), "repeat": bool(r.flags & FLAG_REPEAT)}


class AmbeDecoder:
    """Stateful AMBE+2 3600x2450 decoder (one per call / voice stream).
    decode_frame(ambe_fr 4x24) for DMR (mbelib does the FEC);
    decode_params(ambe_d 49 bits) for Fusion DN (FEC done by the caller)."""

    def __init__(self, lib=None):
        self.lib, self.path = (lib, None) if lib is not None else load_library()
        if not hasattr(self.lib, "mbe_processAmbe2450Data"):
            raise OSError("this mbe-neo build has no AMBE+2 support")
        self._cur = ctypes.create_string_buffer(PARMS_BYTES)
        self._prev = ctypes.create_string_buffer(PARMS_BYTES)
        self._prev_enh = ctypes.create_string_buffer(PARMS_BYTES)
        self._fr = (ctypes.c_char * 24 * 4)()
        self._d = ctypes.create_string_buffer(49)
        self._out = (ctypes.c_short * SAMPLES_PER_FRAME)()
        self._res = _Result()
        self.reset()

    def reset(self):
        self.lib.mbe_initMbeParms(self._cur, self._prev, self._prev_enh)

    def _result(self, rc):
        pcm = np.ctypeslib.as_array(self._out).copy()
        r = self._res
        return pcm, {"rc": rc, "total": r.total_errors,
                     "muted": bool(r.flags & FLAG_MUTE), "repeat": bool(r.flags & FLAG_REPEAT)}

    def decode_frame(self, ambe_fr: np.ndarray):
        ctypes.memmove(self._fr, bytes(np.ascontiguousarray(ambe_fr, dtype=np.uint8).ravel()), 96)
        rc = self.lib.mbe_processAmbe3600x2450Frame(
            self._out, ctypes.byref(self._res), ctypes.byref(self._fr), self._d,
            self._cur, self._prev, self._prev_enh)
        return self._result(rc)

    def decode_params(self, ambe_d: np.ndarray):
        d = bytes(np.ascontiguousarray(ambe_d, dtype=np.uint8)[:49])
        rc = self.lib.mbe_processAmbe2450Data(
            self._out, ctypes.byref(self._res), d, self._cur, self._prev, self._prev_enh)
        return self._result(rc)

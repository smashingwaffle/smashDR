"""
Cut one channel out of a smashDR .cu8 recording into a small IQ WAV
(stereo 16-bit: left = I, right = Q, 48 kHz complex), e.g. to share it.

    python -m tools.clip_iq recordings\\20261024_220354_449.46160MHz_2400000sps.cu8
    python -m tools.clip_iq <file.cu8> --seconds 60 --start 10 --freq 449.4616
    python -m tools.clip_iq <file.cu8> --freq auto     (strongest signal in the recording)
    python -m tools.clip_iq <file.cu8> --list          (just list the strongest signals)

The WAV lands next to the recording: <name>_ch48k.wav (about 11.5 MB per minute).
A .json sidecar with the channel frequency is written too.
"""

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smashdr.core.dsp import Channelizer, cu8_to_iq  # noqa: E402

OUT_RATE = 48_000
HALF_BW = 9_000            # keeps a 12.5 kHz channel (DMR, P25, NFM) with margin


def strongest(src, rate, centre, n_fft=8192, seconds=20.0, top=6):
    """Peaks of the averaged spectrum (first `seconds`): [(freq_hz, dB above floor)]."""
    nbytes = int(min(seconds * rate, src.stat().st_size // 2)) * 2
    with open(src, "rb") as f:
        iq = cu8_to_iq(f.read(nbytes))
    k = len(iq) // n_fft
    if k == 0:
        return []
    win = np.hanning(n_fft).astype(np.float32)
    p = np.fft.fftshift((np.abs(np.fft.fft(iq[:k * n_fft].reshape(k, n_fft) * win, axis=1)) ** 2)
                        .max(axis=0))
    db = 10 * np.log10(p + 1e-20)
    floor = float(np.median(db))
    freqs = centre + (np.arange(n_fft) - n_fft / 2) * rate / n_fft
    db[n_fft // 2 - 3:n_fft // 2 + 4] = floor          # ignore the DC spike
    edge = int(n_fft * 0.05)                            # and the filter edges
    db[:edge] = floor
    db[-edge:] = floor
    out = []
    guard = int(15_000 / (rate / n_fft))                # one result per 15 kHz
    for _ in range(top):
        i = int(np.argmax(db))
        if db[i] - floor < 6:
            break
        out.append((float(freqs[i]), float(db[i] - floor)))
        db[max(0, i - guard):i + guard] = floor
    return out


def main():
    ap = argparse.ArgumentParser(description="channel IQ clip -> stereo WAV")
    ap.add_argument("cu8")
    ap.add_argument("--freq", help="channel MHz, or 'auto' for the strongest signal "
                                   "(default: target in the .json)")
    ap.add_argument("--list", action="store_true", help="list the strongest signals and exit")
    ap.add_argument("--start", type=float, default=0.0, help="seconds into the recording")
    ap.add_argument("--seconds", type=float, default=90.0, help="length to keep")
    args = ap.parse_args()

    src = Path(args.cu8)
    meta = json.loads(src.with_suffix(".json").read_text(encoding="utf-8"))
    rate = float(meta["sample_rate"])
    centre = float(meta["center_hz"])
    if args.list or (args.freq or "").lower() == "auto":
        found = strongest(src, rate, centre)
        for k, (f_hz, db) in enumerate(found):
            print(f"  {k + 1}. {f_hz / 1e6:.5f} MHz   {db:+.1f} dB above noise")
        if args.list or not found:
            return
        target = found[0][0]
        print(f"Using {target / 1e6:.5f} MHz")
    else:
        target = float(args.freq) * 1e6 if args.freq else float(meta["target_hz"])
    ch = Channelizer(rate, OUT_RATE, HALF_BW, target - centre)

    out = src.with_name(src.stem + "_ch48k.wav")
    block = int(rate) // 10 * 2                    # 0.1 s of cu8 bytes
    skip = int(args.start * rate) * 2
    total = int(args.seconds * rate) * 2
    peak, parts = 1e-9, []
    with open(src, "rb") as f:
        f.seek(skip)
        done = 0
        while done < total:
            raw = f.read(min(block, total - done))
            if not raw:
                break
            done += len(raw)
            y = ch.process(cu8_to_iq(raw))
            if len(y):
                parts.append(y)
                peak = max(peak, float(np.max(np.abs(y))))
    iq = np.concatenate(parts) if parts else np.zeros(0, dtype=np.complex64)
    scale = 0.7 * 32767 / peak
    st = np.empty((len(iq), 2), dtype=np.int16)
    st[:, 0] = np.clip(iq.real * scale, -32768, 32767)
    st[:, 1] = np.clip(iq.imag * scale, -32768, 32767)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(st.tobytes())
    out.with_suffix(".json").write_text(json.dumps({
        "format": "wav stereo int16 I/Q (left=I, right=Q)", "sample_rate": OUT_RATE,
        "center_hz": target, "target_hz": target, "source": src.name,
        "start_s": args.start, "seconds": len(iq) / OUT_RATE}, indent=2), encoding="utf-8")
    print(f"Wrote {out.name}: {len(iq) / OUT_RATE:.1f} s at {target / 1e6:.5f} MHz, "
          f"{out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

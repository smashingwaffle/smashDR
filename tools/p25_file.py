"""
Run a smashDR IQ recording through the P25 Phase 1 receive chain and report
what it finds: frame syncs, NACs, frame types (DUIDs), NID error counts.

    python -m tools.p25_file recordings\\20261022_221500_507.18750MHz_960000sps.cu8
    python -m tools.p25_file <file.cu8> --freq 507.1875   (override the target)
    python -m tools.p25_file <file.cu8> --wav calls.wav   (decode clear voice, needs mbe-neo)
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smashdr.decoders import p25  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="P25 Phase 1 frame report for a .cu8 recording")
    ap.add_argument("cu8")
    ap.add_argument("--freq", type=float, help="target MHz (default: from the .json sidecar)")
    ap.add_argument("--show", type=int, default=0, help="also list this many individual frames")
    ap.add_argument("--wav", help="write the voice of clear calls to this WAV file (8 kHz)")
    args = ap.parse_args()

    target = args.freq * 1e6 if args.freq else None
    frames, meta = p25.decode_recording(args.cu8, target)
    secs = Path(args.cu8).stat().st_size / 2 / float(meta["sample_rate"])
    tgt = (target or float(meta["target_hz"])) / 1e6
    print(f"{Path(args.cu8).name}: {secs:.1f} s at {float(meta['sample_rate']) / 1e6:.2f} MS/s, "
          f"target {tgt:.5f} MHz")
    if not frames:
        print("No P25 frame syncs found. Check: is the channel P25 and active during the "
              "recording? Is the target frequency right? Is 'clip' at 0%?")
        return
    nacs = Counter(f"0x{f['nac']:03X}" for f in frames)
    duids = Counter(f["duid_name"] for f in frames)
    errs = [f["bch_errors"] for f in frames]
    print(f"{len(frames)} frames ({len(frames) / secs:.1f}/s)")
    print("NAC:  " + ", ".join(f"{k} x{v}" for k, v in nacs.most_common()))
    print("Type: " + ", ".join(f"{k} x{v}" for k, v in duids.most_common()))
    print(f"NID bit errors: mean {sum(errs) / len(errs):.2f}, max {max(errs)} (BCH corrects up to 11)")
    print(f"Sync level: mean {sum(f['sync_level'] for f in frames) / len(frames):.2f} (ideal 3.00)")
    print()
    calls = p25.group_calls(frames)
    print(f"{len(calls)} transmission(s):")
    print("     start   length  talkgroup  radio ID   encryption   voice frames")
    for c in calls:
        enc = p25.algid_name(c["algid"]) if c["algid"] is not None else "?"
        who = f"{c['src']}" if c["src"] is not None else "?"
        tg = c["tgid"] if c["tgid"] is not None else (f"unit {c['dst']}" if c["dst"] else "?")
        flag = "  EMERGENCY" if c["emergency"] else ""
        print(f"  {c['start']:7.2f}s  {c['end'] - c['start']:5.1f}s  {str(tg):>9}  {who:>9}   "
              f"{enc:11s}  {len(c['imbe']):5d}{flag}")
    if args.wav:
        write_wav(calls, args.wav)
    if not args.show:
        return
    print()
    print("    time      NAC   type   NID err  sync err")
    for f in frames[:args.show]:
        print(f"  {f['t']:7.3f}s   0x{f['nac']:03X}  {f['duid_name']:6s}  {f['bch_errors']:4d}     "
              f"{f['sync_errors']:3d}")
    if len(frames) > args.show:
        print(f"  ... {len(frames) - args.show} more")


def write_wav(calls, path):
    import wave
    import numpy as np
    from smashdr.core import vocoder
    try:
        dec = vocoder.ImbeDecoder()
    except OSError as e:
        print(f"\nNo voice: {e}")
        return
    pcm, errs = [], []
    gap = np.zeros(vocoder.RATE // 4, dtype=np.int16)          # 250 ms between calls
    for c in calls:
        if c["encrypted"] or not c["imbe"]:
            continue
        dec.reset()
        for fr in c["imbe"]:
            a, r = dec.decode(fr)
            pcm.append(a)
            errs.append(r["total"])
        pcm.append(gap)
    if not pcm:
        print("\nNo clear voice to write.")
        return
    audio = np.concatenate(pcm).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(vocoder.RATE)
        w.writeframes(audio.tobytes())
    print(f"\nWrote {len(audio) / vocoder.RATE:.1f} s of clear voice to {path} "
          f"({len(errs)} IMBE frames, {100 * sum(e == 0 for e in errs) / len(errs):.0f}% with no FEC errors)")


if __name__ == "__main__":
    main()

"""
Run a smashDR IQ recording through the P25 Phase 1 receive chain and report
what it finds: frame syncs, NACs, frame types (DUIDs), NID error counts.

    python -m tools.p25_file recordings\\20261022_221500_507.18750MHz_960000sps.cu8
    python -m tools.p25_file <file.cu8> --freq 507.1875   (override the target)
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import p25  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="P25 Phase 1 frame report for a .cu8 recording")
    ap.add_argument("cu8")
    ap.add_argument("--freq", type=float, help="target MHz (default: from the .json sidecar)")
    ap.add_argument("--show", type=int, default=25, help="how many frames to list")
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
    print("    time      NAC   type   NID err  sync err")
    for f in frames[:args.show]:
        print(f"  {f['t']:7.3f}s   0x{f['nac']:03X}  {f['duid_name']:6s}  {f['bch_errors']:4d}     "
              f"{f['sync_errors']:3d}")
    if len(frames) > args.show:
        print(f"  ... {len(frames) - args.show} more")


if __name__ == "__main__":
    main()

# smashDR - notes for Claude

## Workflow
- Claude edits files, commits to `main`, and pushes. Stefaan hits F5 in VSCode;
  the preLaunchTask runs `git pull --ff-only`, then `main.py` starts.
- No zips, no copy-paste file drops.
- Every push must run. Never commit a broken intermediate state.
- Keep replies concise; Stefaan tests right away and pastes raw logs back.

## Project
- Own P25 / DMR decoder for an RTL-SDR Blog V4 on Windows, Python + PyQt6.
- Goal: one installer, everything bundled, a UI that doesn't fight you.
- Unencrypted traffic only. No decryption work.
- A Uniden SDS200 is available as ground truth when debugging decodes.

## Build order
1. Probe: IQ off the V4, spectrum/waterfall, IQ recording  <- current
2. C4FM demod + symbol timing, run on recordings
3. P25 Phase 1 frame sync + NID (BCH 63,16)
4. Frame types (HDU/LDU/TSBK), FEC
5. IMBE voice via mbelib (ctypes DLL)
6. Trunking: control channel, follow grants
7. DMR (4FSK, TDMA bursts, AMBE+2)
8. P25 Phase 2
UX comes after the decode chain works.

## Develop against recordings
`recordings/*.cu8` (interleaved uint8 IQ) + `.json` sidecar with centre,
rate, gain, target frequency. Decoder stages should run on a file first,
live SDR second.

## Native libs (not in git)
rtlsdr.dll + msvcr100.dll + pthreadVC2.dll from the rtl-sdr-blog release (x64; libusb is built in), next to
the scripts or in `lib/`. The V4 needs the rtl-sdr-blog fork of librtlsdr.

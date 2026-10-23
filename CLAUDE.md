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
- Clear (unencrypted) digital traffic only: P25/DMR in the clear is the goal.
  Encrypted (RR modes P25E / D Enc / DMRE) is never targeted, no key or
  decryption work. Presets mark digital channels with a `digital` field.
- A Uniden SDS200 is available as ground truth when debugging decodes.

## Build order
1. Receiver: spectrum/waterfall, IQ recording, NFM/AM/WFM, VFO, band plan, settings  <- done
1b. Scanner: memories, scan lists, band/range scan  <- next
2. C4FM demod + symbol timing, run on recordings
3. P25 Phase 1 frame sync + NID (BCH 63,16)
4. Frame types (HDU/LDU/TSBK), FEC
5. IMBE voice via mbelib (ctypes DLL)
6. Trunking: control channel, follow grants
7. DMR (4FSK, TDMA bursts, AMBE+2)
8. P25 Phase 2
UX comes after the decode chain works.

## Code layout
- `main.py` entry; `receiver.py` main window, AsyncCapture (gap-free USB
  thread) and SdrReader (processing thread)
- `dsp.py` block DSP with state: Channelizer (NCO + FFT overlap-save
  decimator), Demod (NFM/AM/WFM -> 48 kHz, gate, limiter, click-free switches)
- `widgets.py` FreqDisplay (VFO) and LevelMeter
- `bandplan.py` simplified US band plan, lookup() and in_span()
- `presets.py` preset store (presets.json local, seeded from
  presets_default.json in git) + PresetsWindow (second-screen button grid)
- Stefaan is in LA (90032); presets and examples should be LA-area, Mt Wilson
  is his main repeater site. Never seed other regions' frequencies.
- `audio.py` QtMultimedia output, converts to device's preferred format
- `settings.py` settings.json store (not in git)
- Don't change `.vscode/launch.json` casually: Stefaan may have local edits,
  and a conflicting change breaks the F5 `git pull --ff-only`.

## Testing
PyQt6 can't be installed in Claude's sandbox (PyPI blocked), so the GUI is
only statically checked there. DSP is verified offline against synthetic
signals and Stefaan's recordings before every push.

## Develop against recordings
`recordings/*.cu8` (interleaved uint8 IQ) + `.json` sidecar with centre,
rate, gain, target frequency. Decoder stages should run on a file first,
live SDR second.

## Native libs (not in git)
rtlsdr.dll + msvcr100.dll + pthreadVC2.dll from the rtl-sdr-blog release (x64; libusb is built in), next to
the scripts or in `lib/`. The V4 needs the rtl-sdr-blog fork of librtlsdr.

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
2. C4FM demod + symbol timing  <- done, verified on LAPD Hollenbeck (507.1875)
3. P25 Phase 1 frame sync + NID (BCH 63,16)  <- done, verified on air
4. Frame types HDU/LDU/TDU, FEC (Hamming, Golay, RS)  <- done, verified on air; TSBK next
5. IMBE voice via mbelib-neo (ctypes)  <- done (97% frames 0 FEC errors on air)
5b. Live P25 mode in the receiver (P25Live)  <- done, confirmed on air (Hollenbeck, "better than the SDS200")
6. Trunking: control channel, follow grants
7. DMR (4FSK, TDMA bursts, AMBE+2)
8. P25 Phase 2
UX comes after the decode chain works.

## Code layout
```
main.py                  entry (F5); stays in the root
smashdr/paths.py         ROOT, data/, user/, recordings/, DLL dirs (root or lib/)
smashdr/core/            no Qt widgets: dsp, bandplan, settings, vocoder, transcribe
smashdr/ui/              PyQt6: receiver (main window), presets, widgets, audio
smashdr/decoders/        p25, p25_fec, p25_live (DMR / Phase 2 go here later)
data/presets_default.json  seed presets (in git)
user/                    settings.json, presets.json (not in git; old root copies
                         are moved here on first start)
tests/  tools/           run from the root: python -m tests.test_p25,
                         python -m tools.p25_file <file.cu8>
```
Imports are absolute (`from smashdr.core.dsp import ...`).
- `ui/receiver.py` main window, AsyncCapture (gap-free USB thread) and
  SdrReader (processing thread)
- `core/dsp.py` block DSP with state: Channelizer (NCO + FFT overlap-save
  decimator), Demod (NFM/AM/WFM -> 48 kHz, gate, limiter, click-free switches)
- `ui/widgets.py` FreqDisplay (VFO) and LevelMeter
- `core/bandplan.py` simplified US band plan, lookup() and in_span()
- `ui/presets.py` preset store + PresetsWindow (second-screen window: group
  sidebar, group colours, Pinned section, live marker)
- Stefaan is in LA (90032); presets and examples should be LA-area, Mt Wilson
  is his main repeater site. Never seed other regions' frequencies.
- `decoders/p25.py` P25 Phase 1: C4FMDemod (raw symbols; integrate-and-dump + Gardner,
  loop gain negative), P25Framer (normalised-correlation sync, per-frame
  gain/offset LS fit from the sync -> catches HDUs; NID ML BCH; collects
  whole frames), decode_frame_body (LC, ESS/ALGID, HDU, IMBE de-interleave
  formula), group_calls(), decode_recording()
- `decoders/p25_fec.py` Hamming(10,6), Golay(24,12)/(18,6), RS over GF(64) (0x43,
  FCR 1) encode + decode. Matches the air: 0 corrections on clean frames.
- `decoders/p25_live.py` P25Live: streaming chain + call state -> 48 kHz audio + events
  (start/update/end per call, 'cc' once a second on control channels). Voice
  held until ALGID known; encrypted never decoded. Receiver mode "P25"
  (dsp.DIGITAL_MODES), P25 presets switch to it.
- Channelizer mixer uses a cached rotation table (was 70% of CPU)
- `core/vocoder.py` mbelib-neo via ctypes (mbe-neo.dll / libmbe-neo.so,
  SMASHDR_MBELIB env override). imbe_fr[8][23], MSB at index 22.
- `.github/workflows/build-mbelib-neo.yml` builds mbe-neo.dll on a Windows
  runner from a pinned commit -> release 'mbelib-neo-win64'.
  Must stay MBELIB_ENABLE_NATIVE=OFF (native build = illegal instruction on
  Stefaan's CPU).
- Encrypted calls (ALGID != 0x80) keep metadata, voice is dropped in
  group_calls(); never pass encrypted IMBE to the vocoder.
- `tests/` synthetic C4FM generator (standard tx filter) + `python -m tests.test_p25`
- `tools/p25_file.py` frame report for a .cu8 recording
- `ui/audio.py` QtMultimedia output, converts to device's preferred format
- `core/settings.py` user/settings.json store (not in git)
- `core/transcribe.py` faster-whisper (small.en or large-v3-turbo via Settings, CPU int8, hotwords + junk filter; Stefaan has Iris Xe,
  no NVIDIA) on finished clear P25 calls, background queue -> right-hand
  Transcripts column + user/transcripts.txt. Optional dependency.
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

# smashDR

A P25 / DMR decoder for the RTL-SDR Blog V4, in Python + PyQt6.
One app, one install, no scattered plugins.

**Status:** receiver with NFM / AM / WFM, VFO tuning, US band plan, spectrum,
waterfall, IQ recording and a settings tab. Scanning is next; P25 decoding after.

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. Download the latest release from the rtl-sdr-blog GitHub repo and copy
   `rtlsdr.dll`, `msvcr100.dll` and `pthreadVC2.dll` from its **x64** folder into this folder
   (or a `lib` subfolder).
2. One-time driver: run Zadig -> Options -> List All Devices ->
   *Bulk-In, Interface (Interface 0)* -> install **WinUSB**.
3. Close any other SDR app that might be holding the dongle.

## Run

```
python main.py                     # uses last frequency/gain/audio settings
python main.py --freq 851.0125     # command line overrides saved settings
```

Or press F5 in VSCode (pulls the latest code first). Settings (frequency,
gain, rate, squelch, volume, audio device) are saved to `settings.json`
on close. Pick the audio output device on the **Settings** tab.

Tuning: wheel over a digit of the frequency display to change that digit,
Up/Down arrows step, double-click (or start typing) to enter a frequency.
In the spectrum or waterfall, click to tune (snaps to the step) and use the
wheel to step. The band strip and label show what band you're in, and the
mode and step follow the band plan (switch that off in Settings).
Set the gain (Settings tab) so `clip` stays at 0%. **Record IQ** writes `recordings/*.cu8` plus a `.json`
sidecar; those captures are what the decoder stages are developed against.
2.4 MS/s is about 290 MB per minute; 0.96 MS/s is plenty for one channel.

## Presets

**Open presets** opens a separate window (made for a second screen; the first
time it fills your secondary monitor, after that it remembers where you put it).
Groups collapse; click a preset to tune, right-click to edit. **Import…**
takes rows copied from a RadioReference table: select the rows (with or
without the header), copy, paste. Clear digital channels are kept and labelled; encrypted ones are skipped.
Your presets live in `presets.json` (not in git); new defaults added to
`presets_default.json` merge in without undoing your deletions.

## P25 (in progress)

Phase 1 decoding works offline on recordings: frames, talkgroup, radio ID,
clear/encrypted, and clear voice through mbelib-neo.

1. Get `mbe-neo.dll`: GitHub repo -> Releases -> "mbelib-neo for Windows"
   (built by this repo's workflow). Put it next to `main.py`.
2. Record a P25 channel (Record IQ, 0.96 MS/s), then:

```
python -m tools.p25_file recordings\<file>.cu8 --wav calls.wav
```

It lists each transmission (talkgroup, radio ID, encryption) and writes the
clear calls' audio to the WAV. Encrypted calls are listed but never decoded.

## Scope

Unencrypted traffic only.

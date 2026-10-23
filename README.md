# smashDR

A P25 / DMR decoder for the RTL-SDR Blog V4, in Python + PyQt6.
One app, one install, no scattered plugins.

**Status:** step 1 - receiver probe: spectrum, waterfall, NFM audio monitor, IQ recording.
Nothing digital is decoded yet.

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

Click a signal to put the 12.5 kHz channel marker on it. Set the gain so
`clip` stays at 0%. **Record IQ** writes `recordings/*.cu8` plus a `.json`
sidecar; those captures are what the decoder stages are developed against.
2.4 MS/s is about 290 MB per minute; 0.96 MS/s is plenty for one channel.

## Scope

Unencrypted traffic only.

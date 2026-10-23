# smashDR

A P25 / DMR decoder for the RTL-SDR Blog V4, in Python + PyQt6.
One app, one install, no scattered plugins.

**Status:** step 1 - SDR probe (spectrum, waterfall, IQ recording). Nothing is decoded yet.

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. Download the latest release from the rtl-sdr-blog GitHub repo and copy
   `rtlsdr.dll` and `libusb-1.0.dll` from its **x64** folder into this folder
   (or a `lib` subfolder).
2. One-time driver: run Zadig -> Options -> List All Devices ->
   *Bulk-In, Interface (Interface 0)* -> install **WinUSB**.
3. Close any other SDR app that might be holding the dongle.

## Run

```
python main.py --freq 851.0125
python main.py --freq 446.0 --gain 30 --rate 1.2
```

Or press F5 in VSCode (pulls the latest code first).

Click a signal to put the 12.5 kHz channel marker on it. Set the gain so
`clip` stays at 0%. **Record IQ** writes `recordings/*.cu8` plus a `.json`
sidecar; those captures are what the decoder stages are developed against.
2.4 MS/s is about 290 MB per minute; 0.96 MS/s is plenty for one channel.

## Scope

Unencrypted traffic only.

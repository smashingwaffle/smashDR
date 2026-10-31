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
4. Frame types HDU/LDU/TDU, FEC (Hamming, Golay, RS)  <- done, verified on air; TSBK done (857.1625 / 857.4625)
5. IMBE voice via mbelib-neo (ctypes)  <- done (97% frames 0 FEC errors on air)
5b. Live P25 mode in the receiver (P25Live)  <- done, confirmed on air (Hollenbeck, "better than the SDS200")
6. Trunking: control channel, follow grants  <- P25 in progress: P25Trunk follows grants inside the span
7. DMR (4FSK, TDMA bursts, AMBE+2)  <- done offline + live mode (446.82 CC1 TG 91 decoded, voice ok)
7b. Fusion / YSF DN  <- done offline + live mode (449.34 AA6RJ/R, VE6DBD; voice ok)
7c. D-STAR  <- done offline + live mode (446.56 KI6MGN C, KJ6ACO; 1299 frames ~0 errors)
8. P25 Phase 2
UX comes after the decode chain works.

## Code layout
```
main.py                  entry (F5); stays in the root
smashdr/paths.py         ROOT, data/, user/, recordings/, DLL dirs (root or lib/)
smashdr/core/            no Qt widgets: dsp, bandplan, settings, vocoder, transcribe
smashdr/ui/              PyQt6: receiver (main window), presets, widgets, audio, aircraft
smashdr/decoders/        p25, p25_fec, p25_live, dmr, ysf, digital_live, adsb
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
- `data/bandplan_la.json` + receiver.PlanStrip: second strip under the band bar with the
  LA ham plan (2 m TASMA rev 2015, 70 cm SCRRBA), shown only when the span covers it.
- `ui/presets.py` preset store + PresetsWindow (second-screen window: group
  sidebar, group colours, Pinned section, live marker)
- Stefaan is in LA (90032); presets and examples should be LA-area, Mt Wilson
  is his main repeater site. Never seed other regions' frequencies.
- `decoders/p25.py` P25 Phase 1: C4FMDemod (raw symbols; integrate-and-dump + Gardner,
  loop gain negative), P25Framer (normalised-correlation sync, per-frame
  gain/offset LS fit from the sync -> catches HDUs; NID ML BCH; collects
  whole frames), decode_frame_body (LC, ESS/ALGID, HDU, IMBE de-interleave
  formula), group_calls(), decode_recording()
- `decoders/p25_tsbk.py` TSDU -> TSBKs: deinterleave (received i -> position IL[i]),
  rate-1/2 trellis with the constellation->dibit-pair table, CRC-CCITT init 0 inverted.
  Parses grants, IDEN_UP/VU/TDMA (IdenTable.freq), RFSS/NET/ADJ status, SCCB.
  P25Live keeps system + grants and puts them in the 'cc' event. TSDU frame length
  = 360 dibits max, framer resumes after the TSBK with LB set (_used).
  Verified: 857.4625 = CCCS 45C site 21 grant TG 193 -> 857.9625 matched the voice
  channel's LDU (AES). 857.1625 = BEE00-346 RFSS 1 site 6 (has Phase 2 idens).
- `decoders/p25_trunk.py` P25Trunk (receiver's P25 mode): P25Live on the tuned channel; if it's a
  control channel, each active grant inside the span (not encrypted, not Phase 2, max 3)
  gets its own P25Live on the same IQ ("option B", no retuning). First clear call owns
  the audio; TGs seen encrypted aren't followed again. cc event grants carry 'state'.
  UI: cc_lbl under p25_lbl. 0.22x realtime CPU on the 857 recording (sandbox).
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
- `decoders/dmr.py` DMR: syncs, slot type (no Golay fix yet), BPTC(196,96) with
  Hamming row/col fixes, RS(12,9) LC check (mask 0x96 header / 0x99 terminator),
  AMBE 72-bit -> ambe_fr[4][24] (DSD rW/rX/rY/rZ). Voice only after a clear LC
  (late entry held; embedded LC not done yet).
- `decoders/ysf.py` Fusion: FICH (K=5 Viterbi, IL 2r+40c, Golay24, CRC16 init 0
  xorout FFFF over sent bytes), header CSD1, DN DCH callsigns by FN, DN voice
  (VCH deinterleave argsort(26*(j%4)+j//4), whiten, 27x3 vote + 22 -> 49 bits).
  VD1 / VW voice not done.
- `decoders/dstar.py` D-STAR: syncs, 660-bit header (LFSR x^7+x^4+1 seed 7F scramble,
  DSD deinterleave k+=24, K=3 Viterbi G1=7 G2=5, 41 bytes LSB first, FCS X.25 LE),
  voice 72 bits -> ambe_fr via DW/DX -> vocoder decode_dstar (mbe 3600x2400),
  slow data XOR 70 4F 93, 6-byte blocks (0x5n header copy, 0x4n message).
  DstarLive (digital_live) reuses the C4FM front end: GMSK sign = bit.
- `decoders/digital_live.py` DmrLive / YsfLive: shared _SymStream (channelizer +
  P25 C4FMDemod + absolute-index symbol buffer); same event shape as P25Live
  plus 'proto'. vocoder.AmbeDecoder: decode_frame (DMR) / decode_params (YSF).
- `tools/clip_iq.py` cuts one channel of a .cu8 into a 48 kHz stereo IQ WAV
  (the chat won't take .cu8; Stefaan shares recordings this way)
- `ui/audio.py` QtMultimedia output, converts to device's preferred format
- `core/settings.py` user/settings.json store (not in git)
- `core/transcribe.py` faster-whisper (small.en or large-v3-turbo via Settings, CPU int8, hotwords + junk filter; Stefaan has Iris Xe,
  no NVIDIA) on finished clear P25 calls, background queue -> right-hand
  Transcripts column + user/transcripts.txt. Optional dependency.
- `core/codes.py` + `data/codes.json`: codes in transcripts (Code N, 10-/11-codes,
  PC/VC/H&S numbers, address-guarded) get highlighted + explained. Glossary from
  Stefaan's CA list; `lapd: true` entries override (LAPD Code 6 = out to investigate).
- `core/places.py` + `data/streets.json`: addresses / streets / intersections in
  transcripts become Google Maps links (", Los Angeles, CA" appended).
- Aircraft tab: second RTL-SDR V4 on a 1090 MHz antenna. `decoders/adsb.py`
  (2 MS/s PPM demod, vectorised preamble + CRC-24, DF17/18, CPR local decode vs
  last fix/home, Tracker) + `ui/aircraft.py` (AdsbReader thread, table, Leaflet
  map via optional PyQt6-WebEngine). `core/capture.py` AsyncCapture shared.
  Tests: `python -m tests.test_adsb` (1090 Riddle vectors + synthetic PPM).
  UI: map<->list click sync (map logs 'SELECT:<icao>' to the console, caught by
  _MapPage), info card with route/type/photo from api.adsbdb.com (free, no key,
  looked up only for the selected plane), airline badges from data/airlines.json
  (`core/airlines.py`; no real logos: trademarks), altitude colour scale.
  adsbdb routes are "usual route per flight number" and often wrong:
  `core/airports.py` infers arriving/departed SoCal airport from position,
  track, altitude, climb (airline callsigns -> airline airports only) and the
  card flags database routes that disagree.
  Optional FlightAware AeroAPI key (Settings > Maps and flight info,
  `core/flightaware.py`): /flights/{callsign} on click only, cached per day,
  monthly cap (default 900, counter in settings), 10/min; else adsbdb.
  Flags: country of registration from the ICAO address block
  (`core/icao_country.py`), images from flagcdn cached in user/cache/flags.
  Airline logos: only user-supplied files in user/logos/<ICAO code>.png
  (never drawn or bundled by us). Default: Images.tail() generates an original
  tail-fin icon (palette colour + ICAO code), not a logo.
  Mode S too: DF11 all-call (CRC remainder = II/SI) and DF17 fill a 'known
  address' set; DF4/5/20/21 (address/parity, remainder = ICAO) are accepted only
  for known addresses -> 'transponder only' rows with altitude/squawk, no position.
  crc24(data) = parity of data (augmentation-free); icao() recovers AP addresses.
  Helicopters: emitter category 7, hover (<35 kt above 400 ft), or adsbdb type
  text (HELI_RX); types looked up in the background (<1/s), cached 30 days in
  user/cache/aircraft.json; photos only fetched for the selected plane.
  Squawks: `core/squawks.py` + `data/squawks.json` (meanings), Squawk column next
  to Airline; ADS-B TC28 emergency state (medical, min fuel...) decoded; 7500/
  7600/7700 only count after holding CONFIRM_S=6 s (fat-finger guard) unless the
  aircraft itself flags an emergency; alerts: beep, auto-select, tab turns red,
  user/emergencies.txt log.
  Comm-B (DF20/21 MB): adsb.commb() infers BDS 2,0 callsign / 4,0 selected alt +
  baro / 5,0 track, GS, TAS, roll / 6,0 magnetic heading, IAS, Mach (pyModeS
  rules + check vs ADS-B track; ambiguous -> dropped) -> p['ehs'] (+ t_<bds>).
  Shown in the card ('Radar data') and as 'alt ➜ selected' in the Alt column.
- ISS tab: `core/satellites.py` (vendored pure-Python SGP4 in smashdr/thirdparty/sgp4,
  MIT; TEME->ECEF via GMST; look angles, Doppler, sunlit/visible, passes 48 h; TLE from
  CelesTrak cached 12 h in user/cache/tle.json) + `ui/satellites.py` SatTab (Leaflet map
  with track/footprint, pass table, SkyPlot polar plot, ntfy alert before good passes,
  'Tune receiver' + optional Doppler follow -> MainWindow._sat_tune).
- `core/notify.py` phone alerts via ntfy.sh (free app, private topic in Settings >
  Phone alerts): aircraft emergencies + P25/DMR emergency flag. Background thread.
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

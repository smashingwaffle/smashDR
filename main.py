"""
smashDR entry point.

For now this launches the SDR probe (spectrum, waterfall, IQ recording).
As decoder stages land, this becomes the main app.

Pass arguments straight through, e.g.:
  python main.py --freq 851.0125 --gain 30
"""

import sdr_probe

if __name__ == "__main__":
    sdr_probe.main()

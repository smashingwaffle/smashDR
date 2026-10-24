"""
smashDR entry point.

  python main.py                     (uses last settings)
  python main.py --freq 851.0125     (command line overrides saved settings)
"""

from smashdr.ui import receiver

if __name__ == "__main__":
    receiver.main()

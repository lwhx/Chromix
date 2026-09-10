#!/usr/bin/env python3
"""Repository entry point for the packaged device-pool validator."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from chromix.device_pool import *  # noqa: F401,F403

if __name__ == '__main__':
    raise SystemExit(main())

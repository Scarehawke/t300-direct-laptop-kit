#!/usr/bin/env python3
"""Provision a verified live USB candidate; never targets T300 eMMC."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.provision import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

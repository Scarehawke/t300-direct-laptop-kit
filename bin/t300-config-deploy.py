#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t300_mainline.config_deploy import main


if __name__ == "__main__":
    raise SystemExit(main())

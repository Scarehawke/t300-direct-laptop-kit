#!/usr/bin/env python3
"""Lock the already-downloaded T300 ARM64 Python runtime artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.python_artifacts import (  # noqa: E402
    PythonArtifactError,
    create_lock,
    lock_bytes,
    lock_environment,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-on", required=True)
    parser.add_argument(
        "--stage", type=Path, required=True, help="staged root used for requirement hashes"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "mainline/build/python-artifacts.lock.json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    reports = ROOT / ".cache/mainline/python-reports"
    wheels = ROOT / ".cache/mainline/python-wheelhouse"
    source = args.stage / "opt/t300/src"
    try:
        environments = [
            lock_environment(
                "build",
                reports / "build.json",
                wheels / "build",
            ),
            lock_environment(
                "klipper",
                reports / "klipper.json",
                wheels / "klipper",
                source / "klipper/scripts/klippy-requirements.txt",
                "/opt/t300/src/klipper/scripts/klippy-requirements.txt",
                extras=(("wheel", "0.45.1"),),
            ),
            lock_environment(
                "moonraker",
                reports / "moonraker.json",
                wheels / "moonraker",
                source / "moonraker/scripts/moonraker-requirements.txt",
                "/opt/t300/src/moonraker/scripts/moonraker-requirements.txt",
            ),
            lock_environment(
                "klipperscreen",
                reports / "klipperscreen.json",
                wheels / "klipperscreen",
                source / "klipperscreen/scripts/KlipperScreen-requirements.txt",
                "/opt/t300/src/klipperscreen/scripts/KlipperScreen-requirements.txt",
            ),
        ]
        lock = create_lock(environments, args.resolved_on)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(lock_bytes(lock))
    except (OSError, PythonArtifactError) as exc:
        print("Error: %s" % (exc,), file=sys.stderr)
        return 2
    print("Locked Python artifacts: %s" % (args.output,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

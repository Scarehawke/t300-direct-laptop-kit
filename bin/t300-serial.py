#!/usr/bin/env python3
"""List or interact with the T300 screen's USB service serial connection."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.serial_console import (  # noqa: E402
    SERIAL_BAUD,
    SerialConsoleError,
    interactive_console,
    list_serial_devices,
    serial_device_access,
    validate_serial_device,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    console = subparsers.add_parser("console")
    console.add_argument("--device", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            devices = list_serial_devices()
            if not devices:
                print("No supported USB serial device is currently visible.")
                return 1
            for device in devices:
                print("%s (%s)" % (device, serial_device_access(device)))
            return 0
        device = validate_serial_device(args.device)
        print(
            "Opening %s at %d baud. No commands are sent automatically. "
            "Press Ctrl-] to exit." % (device, SERIAL_BAUD),
            file=sys.stderr,
        )
        interactive_console(device, sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except (OSError, SerialConsoleError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

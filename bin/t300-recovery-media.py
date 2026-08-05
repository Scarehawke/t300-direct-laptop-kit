#!/usr/bin/env python3
"""Audit or locally render the boot configuration for the T300 recovery USB."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.recovery_media import (  # noqa: E402
    RecoveryMediaError,
    audit_recovery_boot,
    audit_recovery_overlay,
    format_json,
    render_recovery_env,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "stack.lock.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--boot-root", type=Path, required=True)
    render = subparsers.add_parser("render-env")
    render.add_argument("--source", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    overlay = subparsers.add_parser("audit-overlay")
    overlay.add_argument("--overlay-root", type=Path, required=True)
    overlay.add_argument("--manifest-sha256", required=True)
    overlay.add_argument("--installed-root", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            result = audit_recovery_boot(args.boot_root, args.lock)
            print(format_json(result))
            return 0 if result["ready_for_interactive_usb_boot"] else 3
        if args.command == "render-env":
            result = render_recovery_env(args.source, args.output, args.lock)
            print(format_json(result))
            return 0
        result = audit_recovery_overlay(
            args.overlay_root, args.manifest_sha256, args.installed_root
        )
        print(format_json(result))
        return 0 if result["ready"] else 3
    except (OSError, RecoveryMediaError, ValueError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

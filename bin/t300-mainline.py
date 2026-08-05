#!/usr/bin/env python3
"""Build and audit the pinned T300 mainline staging tree without deploying it."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.staging import (  # noqa: E402
    StagingError,
    stage_recovery_overlay,
    stage_rootfs,
    verify_cache,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "stack.lock.json")
    parser.add_argument(
        "--cache", type=Path, default=ROOT / ".cache/mainline/downloads"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify-cache")
    verify.add_argument("--without-base-image", action="store_true")
    stage = subparsers.add_parser("stage")
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--mcu-serial", required=True)
    stage.add_argument("--trusted-laptop-cidr", required=True)
    stage.add_argument("--printer-hostname", default="t300")
    stage.add_argument("--data-usb-uuid", default="C66C-ADD5")
    stage.add_argument("--calibration", type=Path)
    stage.add_argument(
        "--gergo-source",
        type=Path,
        help="untouched purchased outer GerGo ZIP; remains a private build input",
    )
    stage.add_argument(
        "--deploy-public-key",
        type=Path,
        help="Ed25519 laptop key accepted only by the restricted bundle receiver",
    )
    stage.add_argument(
        "--python-cache",
        type=Path,
        default=ROOT / ".cache/mainline/python-wheelhouse",
    )
    stage.add_argument(
        "--debian-cache",
        type=Path,
        default=ROOT / ".cache/mainline/debian-packages",
    )
    recovery = subparsers.add_parser("stage-recovery")
    recovery.add_argument("--output", type=Path, required=True)
    recovery.add_argument(
        "--recovery-public-key",
        type=Path,
        required=True,
        help="dedicated Ed25519 key allowed only through the recovery imaging gate",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify-cache":
            checked = verify_cache(
                args.lock, args.cache, include_base=not args.without_base_image
            )
            print("Verified %d locked artifacts" % (len(checked),))
        elif args.command == "stage":
            manifest = stage_rootfs(
                ROOT,
                args.lock,
                args.cache,
                args.output,
                args.mcu_serial,
                args.trusted_laptop_cidr,
                args.printer_hostname,
                args.calibration,
                args.python_cache,
                args.debian_cache,
                args.data_usb_uuid,
                args.gergo_source,
                args.deploy_public_key,
            )
            print("Staged pinned root filesystem: %s" % (manifest,))
        elif args.command == "stage-recovery":
            manifest = stage_recovery_overlay(
                ROOT, args.output, args.recovery_public_key
            )
            print("Staged marked recovery overlay: %s" % (manifest,))
        return 0
    except (OSError, StagingError, ValueError) as exc:
        print("Error: %s" % (exc,), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

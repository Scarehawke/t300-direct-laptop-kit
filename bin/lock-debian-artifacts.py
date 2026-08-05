#!/usr/bin/env python3
"""Lock the APT-authenticated Debian ARM64 package closure."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.debian_artifacts import (  # noqa: E402
    DebianArtifactError,
    create_debian_lock,
    lock_bytes,
)
from t300_mainline.lockfile import load_lock  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-on", required=True)
    parser.add_argument("--apt-command", type=Path, required=True)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--base-status", type=Path, required=True)
    parser.add_argument(
        "--roots",
        type=Path,
        default=ROOT / "mainline/build/debian-root-packages.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "mainline/build/debian-artifacts.lock.json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        stack = load_lock(ROOT / "stack.lock.json")
        lock = create_debian_lock(
            args.roots,
            args.apt_command,
            args.index_dir,
            args.archive_dir,
            args.base_status,
            stack["base_image"]["sha256"],
            args.resolved_on,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(lock_bytes(lock))
    except (OSError, ValueError, DebianArtifactError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print("Locked Debian artifacts: %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

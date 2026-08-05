#!/usr/bin/env python3
"""Scan and approve T300 G-code without contacting the printer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from t300_mainline.gcode_policy import (  # noqa: E402
    GCodePolicy,
    PolicyError,
    admit_gcode,
    format_findings,
    scan_gcode,
)


DEFAULT_POLICY = REPO_ROOT / "mainline" / "policy" / "gcode-policy.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan one file and make no changes")
    scan.add_argument("gcode", type=Path)
    scan.add_argument("--json", action="store_true")
    approve = subparsers.add_parser("approve", help="scan and write a root-owned approval record")
    approve.add_argument("gcode", type=Path)
    approve.add_argument("--gcode-root", type=Path, required=True)
    approve.add_argument("--approval-dir", type=Path, required=True)
    approve.add_argument("--spool-dir", type=Path, required=True)
    approve.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        policy = GCodePolicy.from_json(args.policy)
        report = scan_gcode(args.gcode, policy, args.policy)
        if args.command == "scan":
            if args.json:
                print(json.dumps(report.to_json(), sort_keys=True, indent=2))
            elif report.accepted:
                print(
                    f"ACCEPT {report.sha256} objects={report.object_count} "
                    f"lines={report.lines}"
                )
            else:
                print(format_findings(report.findings), file=sys.stderr)
            return 0 if report.accepted else 2
        if not report.accepted:
            print(format_findings(report.findings), file=sys.stderr)
            return 2
        if not args.apply:
            print("Dry run: file is admissible; no approval record was written")
            print(json.dumps(report.to_json(), sort_keys=True, indent=2))
            return 0
        report, target = admit_gcode(
            args.gcode,
            policy,
            args.policy,
            args.approval_dir,
            args.gcode_root,
            args.spool_dir,
        )
        if not report.accepted or target is None:
            print(format_findings(report.findings), file=sys.stderr)
            return 2
        print(f"Approval written: {target}")
        return 0
    except (OSError, PolicyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

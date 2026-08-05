#!/usr/bin/env python3
"""Exercise full G-code admission with a deterministic large-format job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.gcode_policy import (  # noqa: E402
    GCodePolicy,
    SCANNER_VERSION,
    admit_gcode,
    scan_gcode,
)
from t300_mainline.provision import verify_stage  # noqa: E402


POLICY_PATH = ROOT / "mainline/policy/gcode-policy.json"
LAYERS = 900
RASTERS_PER_LAYER = 40
ROUNDING_EXCESS_PER_RECOVERY_MM = 0.00001
RASTER_FILAMENT_MM = 14.03


def write_large_job(path: Path) -> None:
    current_x = 25.0
    current_y = 25.0
    with path.open("w", encoding="ascii", newline="\n") as handle:
        handle.write(
            "EXCLUDE_OBJECT_DEFINE NAME=large_format_mock CENTER=150,150 "
            "POLYGON=[[25,25],[275,25],[275,275],[25,275]]\n"
            "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220\n"
            "G21\nG90\nM83\nG92 E0\n"
            "G1 X25 Y25 Z0.2 F3000\n"
            "SET_PRESSURE_ADVANCE ADVANCE=0.04\n"
        )
        for layer in range(LAYERS):
            z = 0.2 + layer * 0.3
            handle.write(f";LAYER:{layer}\nG1 Z{z:.3f} F9000\n")
            handle.write("TIMELAPSE_TAKE_FRAME\n")
            for raster in range(RASTERS_PER_LAYER):
                target_y = 25.0 + raster * 6.0
                start_x = 25.0 if (layer + raster) % 2 == 0 else 275.0
                end_x = 275.0 if start_x == 25.0 else 25.0
                wipe_x = current_x + (0.1 if current_x < 150.0 else -0.1)
                wipe_y = current_y + (0.1 if current_y < 275.0 else -0.1)
                handle.write("G1 E-.35 F3000\n")
                handle.write(f"G1 X{wipe_x:.3f} Y{wipe_y:.3f} E-.14999 F2250\n")
                handle.write(f"G1 X{start_x:.3f} Y{target_y:.3f} F15000\n")
                handle.write("G1 E.5 F3000\n")
                handle.write(
                    f"G1 X{end_x:.3f} Y{target_y:.3f} "
                    f"E{RASTER_FILAMENT_MM:.2f} F3600\n"
                )
                current_x = end_x
                current_y = target_y
        handle.write("SET_PRESSURE_ADVANCE ADVANCE=0\nEND_PRINT\n")


def write_stationary_attack(path: Path) -> None:
    path.write_text(
        "EXCLUDE_OBJECT_DEFINE NAME=attack CENTER=35,35 "
        "POLYGON=[[30,30],[40,30],[40,40],[30,40]]\n"
        "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220\n"
        "G21\nG90\nM83\nG1 X20 Y20 Z0.2 F3000\n"
        "G1 E-.49999 F3000\nG1 E.5 F3000\n"
        "G1 E-.49999 F3000\nG1 E.5 F3000\n"
        "END_PRINT\n",
        encoding="ascii",
    )


def write_tiny_move_attack(path: Path) -> None:
    lines = [
        "EXCLUDE_OBJECT_DEFINE NAME=attack CENTER=35,35 "
        "POLYGON=[[30,30],[40,30],[40,40],[30,40]]",
        "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220",
        "G21",
        "G90",
        "M83",
        "G1 X20 Y20 Z0.2 F3000",
    ]
    for index in range(20):
        lines.extend(
            (
                "G1 E-.49999 F3000",
                "G1 E.5 F3000",
                f"G1 X{20.001 + index * 0.001:.3f} Y20 E.00001 F3000",
            )
        )
    lines.append("END_PRINT")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--stage-manifest-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.stage is None) != (args.stage_manifest_sha256 is None):
        raise RuntimeError(
            "--stage and --stage-manifest-sha256 must be supplied together"
        )
    policy_path = POLICY_PATH
    stage_label = "repository policy"
    if args.stage is not None:
        stage = verify_stage(args.stage, args.stage_manifest_sha256)
        policy_path = stage["root"] / "etc/t300/gcode-policy.json"
        stage_label = "verified stage %s" % args.stage_manifest_sha256[:12]
    policy = GCodePolicy.from_json(policy_path)
    with tempfile.TemporaryDirectory(prefix="t300-large-admission-") as directory:
        root = Path(directory)
        gcodes = root / "gcodes"
        approvals = root / "approvals"
        spool = root / "spool"
        for path in (gcodes, approvals, spool):
            path.mkdir()

        source = gcodes / "large-format-digital-twin.gcode"
        write_large_job(source)
        report, approval = admit_gcode(
            source,
            policy,
            policy_path,
            approvals,
            gcodes,
            spool,
        )
        if not report.accepted or approval is None:
            raise RuntimeError("large-format mock was rejected: %r" % report.to_json())
        record = json.loads(approval.read_text(encoding="utf-8"))
        protected = spool / record["spool_file"]
        if record["scanner_version"] != SCANNER_VERSION:
            raise RuntimeError("approval used the wrong scanner version")
        if protected.read_bytes() != source.read_bytes():
            raise RuntimeError("protected snapshot differs from admitted source")

        attack = gcodes / "stationary-rounding-loop.gcode"
        write_stationary_attack(attack)
        attack_report = scan_gcode(attack, policy, policy_path)
        if attack_report.accepted or not any(
            "retraction credit" in finding.message
            for finding in attack_report.findings
        ):
            raise RuntimeError("stationary rounding loop was not rejected")

        tiny_move_attack = gcodes / "tiny-moving-rounding-loop.gcode"
        write_tiny_move_attack(tiny_move_attack)
        tiny_move_report = scan_gcode(tiny_move_attack, policy, policy_path)
        if tiny_move_report.accepted or not any(
            "retraction credit" in finding.message
            for finding in tiny_move_report.findings
        ):
            raise RuntimeError("tiny moving-extrusion rounding loop was not rejected")

        recoveries = LAYERS * RASTERS_PER_LAYER
        total_rounding = recoveries * ROUNDING_EXCESS_PER_RECOVERY_MM
        deposited_filament = recoveries * RASTER_FILAMENT_MM
        print(
            "PASS: admitted %d-line, %d-layer, 250x234x270 mm digital job "
            "with %d retract/wipe recoveries" % (
                report.lines,
                LAYERS,
                recoveries,
            )
        )
        print(
            "PASS: %.0f mm of simulated deposited filament and %.3f mm cumulative "
            "decimal discrepancy remained scale-independent" % (
                deposited_filament,
                total_rounding,
            )
        )
        print("PASS: stationary and tiny-move rounding loops were rejected")
        print("PASS: protected snapshot and approval record match scanner %s" % SCANNER_VERSION)
        print("PASS: large-format test used %s" % stage_label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

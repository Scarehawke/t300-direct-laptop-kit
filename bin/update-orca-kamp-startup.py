#!/usr/bin/env python3
"""Migrate a reviewed OrcaSlicer 3MF to the T300 runtime interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile


PROJECT_SETTINGS = "Metadata/project_settings.config"
TIMELAPSE_BEFORE_LAYER_GCODE = """;BEFORE_LAYER_CHANGE
;[layer_z]
G92 E0
TIMELAPSE_TAKE_FRAME
"""
RUNTIME_START_GCODE = (
    "START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] "
    "EXTRUDER_TEMP=[nozzle_temperature_initial_layer]\n"
)
RUNTIME_END_GCODE = "END_PRINT\n"
RUNTIME_PRINTER_SETTINGS_ID = "T300 AUDITED Runtime 0.4 - REVIEW ONLY"
RUNTIME_REQUIRED_SETTINGS = {
    "enable_power_loss_recovery": "printer_configuration",
    "exclude_object": "1",
    "gcode_flavor": "klipper",
    "gcode_label_objects": "1",
    "printer_settings_id": RUNTIME_PRINTER_SETTINGS_ID,
    "print_sequence": "by layer",
}
LEGACY_FULL_MESH_START_GCODE = (
    "START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] "
    "EXTRUDER_TEMP=[nozzle_temperature_initial_layer] MESH=FULL\n"
)
COMGROW_FACTORY_START_GCODE = (
    "M104 S150 ; prime extruder temp\n"
    "M140 S[bed_temperature_initial_layer_single] ; set bed temp\n"
    "G28\n"
    "G1 Z2.0 F3000 ;Move Z Axis up\n"
    "G1 Z50 X0 Y300 F5000 ;nozzle cleaning position\n"
    "M190 S[bed_temperature_initial_layer_single] ; wait for bed temp\n"
    "M104 S[nozzle_temperature_initial_layer] ; set extruder temp\n"
    "M82 ;absolute extrusion mode\n"
    "START_PRINT \n"
    ";G28 Z\n"
    "G92 E0 ;Reset Extruder\n"
    "G1 Z2.0 F3000 ;Move Z Axis up\n"
    "\n"
    "G1 X3 Y10 Z0.30 F5000.0 ;Move to start position\n"
    "G1 X3 Y150 Z0.28 F1500.0 E15 ;Draw the first line\n"
    "G1 X2.5 Y155 F1500\n"
    "G1 X3.8 Y155 F1500\n"
    "G1 X2.7 Y150.0 Z0.28 F5000.0 ;Move to side a little\n"
    "G1 X2.7 Y50 Z0.35 F1500.0 E30 ;Draw the second line\n"
    "G92 E0 ;Reset Extruder\n"
    "G1 Z2.0 F3000 ;Move Z Axis up"
)
ADAPTIVE_START_GCODE = """M117
M140 S[bed_temperature_initial_layer_single]
M104 S150
M190 S[bed_temperature_initial_layer_single]
G28
BED_MESH_CALIBRATE ADAPTIVE=1
SMART_PARK
M109 S[nozzle_temperature_initial_layer]
LINE_PURGE
M400
G90
M83
"""

SAVED_FULL_MESH_START_GCODE = """M117
M140 S[bed_temperature_initial_layer_single]
M104 S150
M190 S[bed_temperature_initial_layer_single]
G28
BED_MESH_PROFILE LOAD=default
SMART_PARK
M109 S[nozzle_temperature_initial_layer]
LINE_PURGE
M400
G90
M83
"""

FRESH_FULL_MESH_START_GCODE = """M117
M140 S[bed_temperature_initial_layer_single]
M104 S150
M190 S[bed_temperature_initial_layer_single]
G28
BED_MESH_CLEAR
BED_MESH_CALIBRATE_BASE
BED_MESH_OUTPUT
SMART_PARK
M109 S[nozzle_temperature_initial_layer]
LINE_PURGE
M400
G90
M83
"""

DENSE_LOCAL_MESH_START_GCODE = """M117
M140 S[bed_temperature_initial_layer_single]
M104 S150
M190 S[bed_temperature_initial_layer_single]
G28
BED_MESH_CLEAR
BED_MESH_CALIBRATE_BASE MESH_MIN=100,100 MESH_MAX=200,200 PROBE_COUNT=9,9
BED_MESH_OUTPUT
SMART_PARK
M109 S[nozzle_temperature_initial_layer]
LINE_PURGE
M400
G90
M83
"""

LEGACY_START_GCODES = {
    ADAPTIVE_START_GCODE,
    COMGROW_FACTORY_START_GCODE,
    DENSE_LOCAL_MESH_START_GCODE,
    FRESH_FULL_MESH_START_GCODE,
    LEGACY_FULL_MESH_START_GCODE,
    SAVED_FULL_MESH_START_GCODE,
}


class ProjectError(RuntimeError):
    pass


def update_project(
    source: Path,
    destination: Path,
    *,
    initial_nozzle_temp: int | None = None,
    bottom_flow_ratio: float | None = None,
    timelapse_per_layer: bool = False,
    use_printer_retraction: bool = False,
    retract_infill_travels: bool = False,
) -> tuple[str, str]:
    if source.suffix.lower() != ".3mf" or destination.suffix.lower() != ".3mf":
        raise ProjectError("source and destination must be .3mf files")
    if not source.is_file():
        raise ProjectError(f"source project does not exist: {source}")
    if destination.exists():
        raise ProjectError(f"destination already exists: {destination}")

    with zipfile.ZipFile(source, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ProjectError("project contains duplicate ZIP member names")
        if names.count(PROJECT_SETTINGS) != 1:
            raise ProjectError("project must contain exactly one Orca project settings file")
        original_members = {info.filename: archive.read(info) for info in infos}

    try:
        settings = json.loads(original_members[PROJECT_SETTINGS])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError("Orca project settings are not valid JSON") from exc
    if not isinstance(settings, dict):
        raise ProjectError("Orca project settings are not a JSON object")
    if settings.get("print_sequence") != "by layer":
        raise ProjectError(
            "project must use Orca's by-layer print sequence for the shared end/cancel park"
        )
    old_start = settings.get("machine_start_gcode")
    if not isinstance(old_start, str):
        raise ProjectError("Orca project has no machine_start_gcode string")
    if (
        old_start != RUNTIME_START_GCODE
        and old_start not in LEGACY_START_GCODES
    ):
        raise ProjectError("refusing to replace an unrecognized machine start sequence")

    old_end = settings.get("machine_end_gcode")
    if old_end not in {None, "", RUNTIME_END_GCODE}:
        raise ProjectError("refusing to replace an unrecognized machine end sequence")
    for key, value in RUNTIME_REQUIRED_SETTINGS.items():
        settings[key] = value
    settings["machine_start_gcode"] = RUNTIME_START_GCODE
    settings["machine_end_gcode"] = RUNTIME_END_GCODE
    if initial_nozzle_temp is not None:
        old_temp = settings.get("nozzle_temperature_initial_layer")
        if not isinstance(old_temp, list) or not old_temp:
            raise ProjectError("Orca project has no initial nozzle temperature list")
        settings["nozzle_temperature_initial_layer"] = [str(initial_nozzle_temp)] * len(old_temp)
    if bottom_flow_ratio is not None:
        if "bottom_solid_infill_flow_ratio" not in settings:
            raise ProjectError("Orca project has no bottom solid infill flow ratio")
        settings["bottom_solid_infill_flow_ratio"] = f"{bottom_flow_ratio:g}"
    if timelapse_per_layer:
        old_before_layer = settings.get("before_layer_change_gcode")
        if not isinstance(old_before_layer, str):
            raise ProjectError("Orca project has no before-layer-change G-code string")
        if old_before_layer not in {"", TIMELAPSE_BEFORE_LAYER_GCODE} and not (
            ";BEFORE_LAYER_CHANGE" in old_before_layer
            and "TIMELAPSE_TAKE_FRAME" not in old_before_layer
        ):
            raise ProjectError("refusing to replace unrecognized before-layer-change G-code")
        settings["before_layer_change_gcode"] = TIMELAPSE_BEFORE_LAYER_GCODE
    if use_printer_retraction:
        old_retraction = settings.get("filament_retraction_length")
        if not isinstance(old_retraction, list) or not old_retraction:
            raise ProjectError("Orca project has no filament retraction override list")
        settings["filament_retraction_length"] = ["nil"] * len(old_retraction)
    if retract_infill_travels:
        if "reduce_infill_retraction" not in settings:
            raise ProjectError("Orca project has no reduce-infill-retraction setting")
        settings["reduce_infill_retraction"] = "0"
    updated_settings = (json.dumps(settings, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    staged = Path(staged_name)
    try:
        with zipfile.ZipFile(staged, "w") as output:
            for info in infos:
                content = (
                    updated_settings
                    if info.filename == PROJECT_SETTINGS
                    else original_members[info.filename]
                )
                output.writestr(info, content)

        with zipfile.ZipFile(staged, "r") as check:
            if check.testzip() is not None:
                raise ProjectError("updated 3MF failed ZIP integrity validation")
            revised = json.loads(check.read(PROJECT_SETTINGS))
            if revised.get("machine_start_gcode") != RUNTIME_START_GCODE:
                raise ProjectError("updated 3MF does not contain the reviewed start sequence")
            if revised.get("machine_end_gcode") != RUNTIME_END_GCODE:
                raise ProjectError("updated 3MF does not contain the reviewed end sequence")
            if revised.get("print_sequence") != "by layer":
                raise ProjectError("updated 3MF no longer uses the reviewed by-layer sequence")
            for key, expected in RUNTIME_REQUIRED_SETTINGS.items():
                if revised.get(key) != expected:
                    raise ProjectError(
                        f"updated 3MF does not contain required Orca setting: {key}"
                    )
            if initial_nozzle_temp is not None and set(
                revised.get("nozzle_temperature_initial_layer", [])
            ) != {str(initial_nozzle_temp)}:
                raise ProjectError("updated 3MF does not contain the requested nozzle temperature")
            if bottom_flow_ratio is not None and revised.get(
                "bottom_solid_infill_flow_ratio"
            ) != f"{bottom_flow_ratio:g}":
                raise ProjectError("updated 3MF does not contain the requested bottom flow ratio")
            if timelapse_per_layer and revised.get(
                "before_layer_change_gcode"
            ) != TIMELAPSE_BEFORE_LAYER_GCODE:
                raise ProjectError("updated 3MF does not contain per-layer timelapse capture")
            if use_printer_retraction and set(
                revised.get("filament_retraction_length", [])
            ) != {"nil"}:
                raise ProjectError("updated 3MF does not use the printer retraction setting")
            if retract_infill_travels and revised.get("reduce_infill_retraction") != "0":
                raise ProjectError("updated 3MF still suppresses infill-travel retractions")
            for name, content in original_members.items():
                if name != PROJECT_SETTINGS and check.read(name) != content:
                    raise ProjectError(f"unexpected project member change: {name}")

        if destination.exists():
            raise ProjectError(f"destination appeared during validation: {destination}")
        staged.rename(destination)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    return source_hash, destination_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--initial-nozzle-temp", type=int)
    parser.add_argument("--bottom-flow-ratio", type=float)
    parser.add_argument("--timelapse-per-layer", action="store_true")
    parser.add_argument("--use-printer-retraction", action="store_true")
    parser.add_argument("--retract-infill-travels", action="store_true")
    args = parser.parse_args()
    try:
        source_hash, destination_hash = update_project(
            args.source,
            args.destination,
            initial_nozzle_temp=args.initial_nozzle_temp,
            bottom_flow_ratio=args.bottom_flow_ratio,
            timelapse_per_layer=args.timelapse_per_layer,
            use_printer_retraction=args.use_printer_retraction,
            retract_infill_travels=args.retract_infill_travels,
        )
    except (OSError, ProjectError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(f"source sha256:      {source_hash}")
    print(f"destination sha256: {destination_hash}")
    print("changed setting: machine_start_gcode (parameterized runtime call)")
    print("changed setting: machine_end_gcode (END_PRINT)")
    print("changed settings: Klipper flavor, object labels, exclude objects, runtime printer id, by-layer print sequence")
    if args.initial_nozzle_temp is not None:
        print(f"changed setting: nozzle_temperature_initial_layer ({args.initial_nozzle_temp})")
    if args.bottom_flow_ratio is not None:
        print(f"changed setting: bottom_solid_infill_flow_ratio ({args.bottom_flow_ratio:g})")
    if args.timelapse_per_layer:
        print("changed setting: before_layer_change_gcode (TIMELAPSE_TAKE_FRAME)")
    if args.use_printer_retraction:
        print("changed setting: filament_retraction_length (inherit printer setting)")
    if args.retract_infill_travels:
        print("changed setting: reduce_infill_retraction (disabled)")
    print("all embedded model and metadata members otherwise verified unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

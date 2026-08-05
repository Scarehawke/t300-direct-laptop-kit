"""Fail-closed admission policy for T300 print G-code.

This scanner is defense in depth. Klipper's configured thermal, motion, and
extrusion limits remain authoritative when an admitted file is executed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
from typing import Any, Iterable


SCANNER_VERSION = "1.4"
APPROVAL_SCHEMA_VERSION = 2
KLIPPER_ARGS_RE = re.compile(r"([A-Z_]+|[A-Z*])")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class AxisLimit:
    minimum: float
    maximum: float


@dataclass(frozen=True)
class GCodePolicy:
    policy_version: int = 1
    max_file_bytes: int = 512 * 1024 * 1024
    max_line_bytes: int = 16 * 1024
    max_lines: int = 5_000_000
    max_findings: int = 256
    max_objects: int = 4096
    max_polygon_points: int = 2048
    max_total_object_points: int = 65_536
    max_candidate_files: int = 1024
    max_spool_bytes: int = 1024 * 1024 * 1024
    min_system_free_bytes: int = 1024 * 1024 * 1024
    max_rejection_records: int = 256
    max_timelapse_frames: int = 10_000
    kamp_purge_margin: float = 20.0
    kamp_purge_amount: float = 30.0
    kamp_breakaway_distance: float = 10.0
    nozzle_temp_max: float = 300.0
    bed_temp_max: float = 100.0
    hotend_max_power: float = 1.0
    bed_max_power: float = 1.0
    min_extrude_temp_floor: float = 150.0
    max_velocity: float = 600.0
    max_accel: float = 12000.0
    max_z_velocity: float = 12.0
    max_z_accel: float = 100.0
    max_square_corner_velocity: float = 3.0
    minimum_cruise_ratio_floor: float = 0.75
    max_extrude_cross_section: float = 5.0
    max_extrude_only_distance: float = 100.0
    max_extrude_only_velocity: float = 2000.0
    max_extrude_only_accel: float = 10000.0
    max_instantaneous_corner_velocity: float = 10.0
    max_stationary_positive_extrude: float = 5.0
    filament_diameter: float = 1.75
    x: AxisLimit = AxisLimit(-2.0, 302.0)
    y: AxisLimit = AxisLimit(-6.0, 302.0)
    z: AxisLimit = AxisLimit(-5.0, 370.0)
    tmc_current_max: dict[str, float] = field(
        default_factory=lambda: {
            "stepper_x": 1.1,
            "stepper_y": 1.5,
            "stepper_z": 0.75,
            "extruder": 1.0,
        }
    )

    @classmethod
    def from_json(cls, path: Path) -> "GCodePolicy":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyError(f"could not read policy {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PolicyError("policy must contain one JSON object")
        data = dict(raw)
        for axis in ("x", "y", "z"):
            value = data.get(axis)
            if not isinstance(value, dict):
                raise PolicyError(f"policy axis {axis} must be an object")
            if any(
                isinstance(value.get(bound), bool)
                or not isinstance(value.get(bound), (int, float))
                for bound in ("minimum", "maximum")
            ):
                raise PolicyError(f"policy axis {axis} bounds must be numeric")
            try:
                data[axis] = AxisLimit(float(value["minimum"]), float(value["maximum"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyError(f"policy axis {axis} is malformed") from exc
        try:
            policy = cls(**data)
        except TypeError as exc:
            raise PolicyError(f"policy has unknown or missing fields: {exc}") from exc
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.policy_version != 1:
            raise PolicyError("unsupported G-code policy version")
        if self.max_file_bytes <= 0 or self.max_line_bytes < 128:
            raise PolicyError("file and line limits must be positive")
        for name in (
            "max_lines",
            "max_findings",
            "max_objects",
            "max_polygon_points",
            "max_total_object_points",
            "max_candidate_files",
            "max_spool_bytes",
            "min_system_free_bytes",
            "max_rejection_records",
            "max_timelapse_frames",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PolicyError(f"policy value {name} must be a positive integer")
        if self.max_findings < 2:
            raise PolicyError("max_findings must leave room for a truncation record")
        if self.max_total_object_points < self.max_polygon_points:
            raise PolicyError("total object-point limit is below one polygon limit")
        numeric_names = (
            "kamp_purge_margin",
            "kamp_purge_amount",
            "kamp_breakaway_distance",
            "nozzle_temp_max",
            "bed_temp_max",
            "hotend_max_power",
            "bed_max_power",
            "min_extrude_temp_floor",
            "max_velocity",
            "max_accel",
            "max_z_velocity",
            "max_z_accel",
            "max_square_corner_velocity",
            "minimum_cruise_ratio_floor",
            "max_extrude_cross_section",
            "max_extrude_only_distance",
            "max_extrude_only_velocity",
            "max_extrude_only_accel",
            "max_instantaneous_corner_velocity",
            "max_stationary_positive_extrude",
            "filament_diameter",
        )
        for name in numeric_names:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise PolicyError(f"policy value {name} must be one finite number")
        positive_names = tuple(
            name
            for name in numeric_names
            if name
            not in (
                "max_square_corner_velocity",
                "minimum_cruise_ratio_floor",
                "max_stationary_positive_extrude",
            )
        )
        for name in positive_names:
            if getattr(self, name) <= 0:
                raise PolicyError(f"policy value {name} must be greater than zero")
        if self.max_square_corner_velocity < 0:
            raise PolicyError("square-corner velocity may not be negative")
        if not 0 <= self.minimum_cruise_ratio_floor < 1:
            raise PolicyError("minimum cruise-ratio floor must be in [0,1)")
        if self.max_stationary_positive_extrude < 0:
            raise PolicyError("stationary extrusion limit may not be negative")
        if not 0 < self.hotend_max_power <= 1 or not 0 < self.bed_max_power <= 1:
            raise PolicyError("heater power ceilings must be in (0,1]")
        if self.min_extrude_temp_floor > self.nozzle_temp_max:
            raise PolicyError("cold-extrusion floor exceeds the nozzle ceiling")
        for name, axis in (("x", self.x), ("y", self.y), ("z", self.z)):
            if not math.isfinite(axis.minimum) or not math.isfinite(axis.maximum):
                raise PolicyError(f"axis {name} bounds must be finite")
            if axis.minimum >= axis.maximum:
                raise PolicyError(f"axis {name} minimum must be below its maximum")
        if self.max_stationary_positive_extrude > self.max_extrude_only_distance:
            raise PolicyError("stationary extrusion limit exceeds the E-only distance limit")
        bounded_purge_length = self.kamp_purge_amount + self.kamp_breakaway_distance
        if (
            self.x.maximum < bounded_purge_length
            and self.y.maximum < bounded_purge_length
        ):
            raise PolicyError("neither production axis can contain the bounded KAMP purge")
        if not isinstance(self.tmc_current_max, dict) or set(self.tmc_current_max) != {
            "stepper_x",
            "stepper_y",
            "stepper_z",
            "extruder",
        }:
            raise PolicyError("TMC current policy must cover exactly the stock T300 steppers")
        for stepper, current in self.tmc_current_max.items():
            if not re.fullmatch(r"(?:stepper_[xyz]|extruder)", stepper):
                raise PolicyError(f"unsupported TMC current key: {stepper}")
            if (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not math.isfinite(current)
                or current <= 0
            ):
                raise PolicyError(f"invalid current ceiling for {stepper}")


FORBIDDEN_COMMANDS = {
    "BED_MESH_CALIBRATE",
    "BED_MESH_CLEAR",
    "BED_MESH_PROFILE",
    "BUILD_PLATE_READY",
    "FIRMWARE_RESTART",
    "FORCE_MOVE",
    "G28",
    "INIT_TMC",
    "LINE_PURGE",
    "LOAD_FILAMENT",
    "MANUAL_STEPPER",
    "M112",
    "PROBE",
    "PROBE_CALIBRATE",
    "QUERY_ENDSTOPS",
    "RESTART",
    "RUN_SHELL_COMMAND",
    "SAVE_CONFIG",
    "SDCARD_PRINT_FILE",
    "SET_EXTRUDER_ROTATION_DISTANCE",
    "SET_FILAMENT_SENSOR",
    "SET_HEATER_TEMPERATURE",
    "SET_IDLE_TIMEOUT",
    "SET_KINEMATIC_POSITION",
    "SET_PIN",
    "SET_STEPPER_ENABLE",
    "SET_TMC_FIELD",
    "SYNC_EXTRUDER_MOTION",
    "SMART_PARK",
    "T_CONFIRM_STEEL_SHEET",
    "T_MARK_BUILD_PLATE_DIRTY",
    "T_RESERVE_PRINT_HOME",
    "UNLOAD_FILAMENT",
    "HOME_PRINTER",
    "Z_ENDSTOP_CALIBRATE",
    "Z_TILT_ADJUST",
    "Z_TILT_CALIBRATION",
}

ALLOWED_EXTENDED_COMMANDS = {
    "CANCEL_PRINT",
    "END_PRINT",
    "EXCLUDE_OBJECT_DEFINE",
    "EXCLUDE_OBJECT_END",
    "EXCLUDE_OBJECT_START",
    "M600",
    "PAUSE",
    "SET_PRINT_STATS_INFO",
    "SET_VELOCITY_LIMIT",
    "START_PRINT",
    "TIMELAPSE_TAKE_FRAME",
}

ALLOWED_AFTER_END = {"M400"}

ALLOWED_BEFORE_START = {
    "EXCLUDE_OBJECT_DEFINE",
    "M73",
    "M117",
    "M118",
    "SET_PRINT_STATS_INFO",
    "START_PRINT",
}

ALLOWED_TRADITIONAL_COMMANDS = {
    "G0",
    "G1",
    "G2",
    "G3",
    "G4",
    "G17",
    "G21",
    "G90",
    "G91",
    "G92",
    "M82",
    "M83",
    "M104",
    "M106",
    "M107",
    "M109",
    "M117",
    "M118",
    "M140",
    "M190",
    "M204",
    "M220",
    "M221",
    "M400",
    "M600",
    "M73",
    "T0",
}


@dataclass
class ScanFinding:
    line: int
    command: str
    message: str


@dataclass
class ScanReport:
    path: str
    sha256: str
    size: int
    lines: int
    object_count: int
    timelapse_frames: int
    start_line: int | None
    end_line: int | None
    policy_sha256: str
    findings: list[ScanFinding]

    @property
    def accepted(self) -> bool:
        return not self.findings

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "lines": self.lines,
            "object_count": self.object_count,
            "timelapse_frames": self.timelapse_frames,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "policy_sha256": self.policy_sha256,
            "findings": [asdict(item) for item in self.findings],
            "scanner_version": SCANNER_VERSION,
        }


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def policy_sha256(path: Path) -> str:
    return sha256_file(path)


def _strip_comments(line: str) -> str:
    # Match pinned Klipper: semicolon starts a comment, while parentheses do
    # not. Treating parentheses as comments here would let Klipper see
    # parameters that the admission scanner had discarded.
    return line.split(";", 1)[0].strip()


def _parse_line(line: str) -> tuple[str, dict[str, str]] | None:
    clean = _strip_comments(line)
    if not clean:
        return None

    # This is the command and traditional-parameter grammar from pinned
    # Klipper v0.13.0's GCodeDispatch._process_commands(). In particular, an
    # apparent exponent such as X0e100 is X=0 plus E=100 to Klipper.
    parts = KLIPPER_ARGS_RE.split(clean.upper())
    if "".join(parts[:2]) == "N":
        command = "".join(parts[3:5]).strip()
    else:
        command = "".join(parts[:3]).strip()
    if re.fullmatch(r"[GMT]\d+(?:\.\d+)?", command):
        return command, {
            parts[index]: parts[index + 1].strip()
            for index in range(1, len(parts), 2)
        }

    # Match GCodeDispatch._get_extended_params(), including its handling of a
    # line-number checksum and shell-style quoted KEY=VALUE arguments.
    param_start = len(command)
    param_end = len(clean)
    if clean[:param_start].upper() != command:
        command_at = clean.upper().find(command)
        if command_at < 0:
            raise PolicyError("could not locate extended command parameters")
        param_start += command_at
        checksum_at = clean.rfind("*")
        if checksum_at >= 0 and clean[checksum_at + 1 :].isdigit():
            param_end = checksum_at
    if clean[param_start : param_start + 1].isspace():
        param_start += 1
    lexer = shlex.shlex(clean[param_start:param_end], posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;"
    params: dict[str, str] = {}
    try:
        for argument in lexer:
            key, value = argument.split("=", 1)
            params[key.upper()] = value
    except (ValueError, TypeError) as exc:
        raise PolicyError("extended command parameters are malformed") from exc
    return command, params


def _number(params: dict[str, str], key: str) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise PolicyError(f"{key} is not numeric") from exc
    if not math.isfinite(result):
        raise PolicyError(f"{key} must be finite")
    return result


def _bounds(policy: GCodePolicy, axis: str) -> AxisLimit:
    return getattr(policy, axis.lower())


def _validate_object(
    params: dict[str, str], policy: GCodePolicy
) -> tuple[int, float, float]:
    name = params.get("NAME")
    if (
        name is None
        or not 1 <= len(name) <= 256
        or name.strip() != name
        or any(ord(character) < 32 for character in name)
    ):
        raise PolicyError("EXCLUDE_OBJECT_DEFINE has an invalid NAME")
    center_raw = params.get("CENTER")
    if center_raw is not None:
        center_parts = center_raw.split(",")
        if len(center_parts) != 2:
            raise PolicyError("object CENTER must be x,y")
        try:
            center_x, center_y = (float(value) for value in center_parts)
        except ValueError as exc:
            raise PolicyError("object CENTER coordinates must be numeric") from exc
        if not math.isfinite(center_x) or not math.isfinite(center_y):
            raise PolicyError("object CENTER coordinates must be finite")
        if not policy.x.minimum <= center_x <= policy.x.maximum:
            raise PolicyError("object CENTER X is outside the configured bed")
        if not policy.y.minimum <= center_y <= policy.y.maximum:
            raise PolicyError("object CENTER Y is outside the configured bed")
    polygon_raw = params.get("POLYGON")
    if polygon_raw is None:
        raise PolicyError("EXCLUDE_OBJECT_DEFINE is missing POLYGON")
    try:
        polygon = json.loads(polygon_raw)
    except json.JSONDecodeError as exc:
        raise PolicyError("object POLYGON is not valid JSON") from exc
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise PolicyError("object POLYGON must contain at least three points")
    if len(polygon) > policy.max_polygon_points:
        raise PolicyError("object POLYGON exceeds the production point limit")
    minimum_x = math.inf
    minimum_y = math.inf
    for point in polygon:
        if not isinstance(point, list) or len(point) != 2:
            raise PolicyError("each object polygon point must be [x,y]")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as exc:
            raise PolicyError("object polygon coordinates must be numeric") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise PolicyError("object polygon coordinates must be finite")
        if not policy.x.minimum <= x <= policy.x.maximum:
            raise PolicyError("object polygon X is outside the configured bed")
        if not policy.y.minimum <= y <= policy.y.maximum:
            raise PolicyError("object polygon Y is outside the configured bed")
        minimum_x = min(minimum_x, x)
        minimum_y = min(minimum_y, y)
    return len(polygon), minimum_x, minimum_y


def _check_limit_command(command: str, params: dict[str, str], policy: GCodePolicy) -> None:
    if command == "SET_VELOCITY_LIMIT":
        ceilings = {
            "VELOCITY": policy.max_velocity,
            "ACCEL": policy.max_accel,
            "SQUARE_CORNER_VELOCITY": policy.max_square_corner_velocity,
        }
        for key, ceiling in ceilings.items():
            value = _number(params, key)
            if value is not None and value > ceiling:
                raise PolicyError(f"{key}={value:g} exceeds the production ceiling {ceiling:g}")
            if value is not None and key != "SQUARE_CORNER_VELOCITY" and value <= 0:
                raise PolicyError(f"{key} must be greater than zero")
            if value is not None and key == "SQUARE_CORNER_VELOCITY" and value < 0:
                raise PolicyError(f"{key} may not be negative")
        ratio = _number(params, "MINIMUM_CRUISE_RATIO")
        if ratio is not None:
            if not 0 <= ratio < 1:
                raise PolicyError("MINIMUM_CRUISE_RATIO must be in [0,1)")
            if ratio < policy.minimum_cruise_ratio_floor:
                raise PolicyError("MINIMUM_CRUISE_RATIO lowers the production cruise-ratio floor")
        accel_to_decel = _number(params, "ACCEL_TO_DECEL")
        accel = _number(params, "ACCEL")
        if accel is None:
            accel = policy.max_accel
        if accel_to_decel is not None:
            if accel_to_decel <= 0:
                raise PolicyError("ACCEL_TO_DECEL must be greater than zero")
            implied = 1.0 - min(1.0, accel_to_decel / accel)
            if implied < policy.minimum_cruise_ratio_floor:
                raise PolicyError("ACCEL_TO_DECEL lowers the production cruise-ratio floor")
    elif command == "M204":
        values = [_number(params, key) for key in ("S", "P", "T")]
        for value in values:
            if value is not None and value <= 0:
                raise PolicyError("M204 acceleration must be greater than zero")
            if value is not None and value > policy.max_accel:
                raise PolicyError(f"M204 acceleration {value:g} exceeds {policy.max_accel:g}")
    elif command == "SET_TMC_CURRENT":
        stepper = params.get("STEPPER", "")
        ceiling = policy.tmc_current_max.get(stepper)
        if ceiling is None:
            raise PolicyError(f"SET_TMC_CURRENT targets unapproved stepper {stepper!r}")
        for key in ("CURRENT", "HOLDCURRENT"):
            value = _number(params, key)
            if value is not None and (
                value < 0 or (key == "HOLDCURRENT" and value == 0)
            ):
                raise PolicyError(f"{key} has an invalid current value")
            if value is not None and value > ceiling:
                raise PolicyError(f"{key}={value:g} exceeds {stepper} ceiling {ceiling:g}")


def _open_regular_readonly(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        current = os.lstat(path)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PolicyError(f"could not open regular file without following links: {path}") from exc
    if not os.path.samestat(info, current):
        os.close(descriptor)
        raise PolicyError(f"file changed while it was opened: {path}")
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise PolicyError(f"path is not a regular file: {path}")
    return descriptor, info


def scan_gcode(path: Path, policy: GCodePolicy, policy_path: Path) -> ScanReport:
    requested = path.expanduser().absolute()
    if requested.is_symlink():
        raise PolicyError(f"G-code path may not be a symlink: {requested}")
    path = requested.resolve(strict=True)
    descriptor, stat_result = _open_regular_readonly(path)
    if stat_result.st_size > policy.max_file_bytes:
        os.close(descriptor)
        raise PolicyError(f"G-code exceeds the {policy.max_file_bytes} byte policy limit")
    digest_state = hashlib.sha256()
    findings: list[ScanFinding] = []
    objects = 0
    object_points = 0
    object_x_min = math.inf
    object_y_min = math.inf
    timelapse_frames = 0
    start_line: int | None = None
    end_line: int | None = None
    absolute_axes = True
    absolute_extruder = True
    # START_PRINT runs KAMP's geometry-dependent purge, so its final physical
    # XYZ position cannot be guessed safely. The scanner learns each axis only
    # from a subsequent absolute move before accepting relative movement from it.
    position: dict[str, float | None] = {"X": None, "Y": None, "Z": None, "E": 0.0}
    lines = 0
    findings_truncated = False

    def add_finding(line: int, command: str, message: str) -> None:
        nonlocal findings_truncated
        if findings_truncated:
            return
        if len(findings) < policy.max_findings - 1:
            findings.append(ScanFinding(line, command, message))
            return
        findings.append(
            ScanFinding(
                line,
                command,
                "additional findings omitted after the production report limit",
            )
        )
        findings_truncated = True

    with os.fdopen(descriptor, "rb") as handle:
        while True:
            raw_line = handle.readline(policy.max_line_bytes + 1)
            if not raw_line:
                break
            lines += 1
            digest_state.update(raw_line)
            line_number = lines
            if lines > policy.max_lines:
                if lines == policy.max_lines + 1:
                    add_finding(
                        line_number,
                        "",
                        "file exceeds the production line-count limit",
                    )
                continue
            if len(raw_line) > policy.max_line_bytes:
                while not raw_line.endswith(b"\n"):
                    raw_line = handle.readline(policy.max_line_bytes + 1)
                    if not raw_line:
                        break
                    digest_state.update(raw_line)
                add_finding(line_number, "", "line exceeds policy length")
                continue
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                add_finding(line_number, "", "line is not valid UTF-8")
                continue
            try:
                parsed = _parse_line(text)
            except PolicyError as exc:
                add_finding(line_number, "", str(exc))
                continue
            if parsed is None:
                continue
            command, params = parsed

            if end_line is not None and command not in ALLOWED_AFTER_END:
                add_finding(line_number, command, "command appears after END_PRINT")

            try:
                if command in FORBIDDEN_COMMANDS:
                    raise PolicyError("command is forbidden in production print files")
                if re.fullmatch(r"[GMT]\d+(?:\.\d+)?", command):
                    if command not in ALLOWED_TRADITIONAL_COMMANDS:
                        raise PolicyError("traditional command is not on the production allowlist")
                else:
                    if command not in ALLOWED_EXTENDED_COMMANDS and command != "SET_TMC_CURRENT":
                        raise PolicyError("extended command is not on the production allowlist")
                if start_line is None and command not in ALLOWED_BEFORE_START:
                    raise PolicyError(
                        "only object definitions and inert progress/display metadata "
                        "may appear before START_PRINT"
                    )
                if start_line is not None and command == "EXCLUDE_OBJECT_DEFINE":
                    raise PolicyError(
                        "all Orca object definitions must appear before START_PRINT"
                    )

                if command == "START_PRINT":
                    if start_line is not None:
                        raise PolicyError("START_PRINT may appear only once")
                    if objects == 0:
                        raise PolicyError("Orca object definitions must appear before START_PRINT")
                    bounded_purge_length = (
                        policy.kamp_purge_amount + policy.kamp_breakaway_distance
                    )
                    has_front_lane = (
                        object_y_min >= policy.kamp_purge_margin
                        and policy.x.maximum >= bounded_purge_length
                    )
                    has_left_lane = (
                        object_x_min >= policy.kamp_purge_margin
                        and policy.y.maximum >= bounded_purge_length
                    )
                    if not has_front_lane and not has_left_lane:
                        raise PolicyError(
                            "objects leave no bounded front or left KAMP purge lane"
                        )
                    if "BED_TEMP" not in params or "EXTRUDER_TEMP" not in params:
                        raise PolicyError("START_PRINT requires BED_TEMP and EXTRUDER_TEMP")
                    bed = _number(params, "BED_TEMP")
                    nozzle = _number(params, "EXTRUDER_TEMP")
                    if bed is None or bed < 0 or bed > policy.bed_temp_max:
                        raise PolicyError("START_PRINT bed temperature is outside policy")
                    if nozzle is None or nozzle < 150 or nozzle > policy.nozzle_temp_max:
                        raise PolicyError("START_PRINT nozzle temperature is outside policy")
                    position.update({"X": None, "Y": None, "Z": None, "E": 0.0})
                    start_line = line_number
                elif command == "END_PRINT":
                    if start_line is None:
                        raise PolicyError("END_PRINT appears before START_PRINT")
                    if end_line is not None:
                        raise PolicyError("END_PRINT may appear only once")
                    end_line = line_number
                elif command == "EXCLUDE_OBJECT_DEFINE":
                    if objects >= policy.max_objects:
                        raise PolicyError("file exceeds the production object-count limit")
                    points, minimum_x, minimum_y = _validate_object(params, policy)
                    if object_points + points > policy.max_total_object_points:
                        raise PolicyError(
                            "file exceeds the production object-point limit"
                        )
                    objects += 1
                    object_points += points
                    object_x_min = min(object_x_min, minimum_x)
                    object_y_min = min(object_y_min, minimum_y)
                elif command == "TIMELAPSE_TAKE_FRAME":
                    timelapse_frames += 1
                    if timelapse_frames > policy.max_timelapse_frames:
                        raise PolicyError(
                            "file exceeds the production timelapse-frame limit"
                        )
                elif command in {"M104", "M109"}:
                    target = _number(params, "S")
                    if target is None:
                        target = _number(params, "R")
                    if target is not None and not 0 <= target <= policy.nozzle_temp_max:
                        raise PolicyError("nozzle temperature exceeds production policy")
                elif command in {"M140", "M190"}:
                    target = _number(params, "S")
                    if target is None:
                        target = _number(params, "R")
                    if target is not None and not 0 <= target <= policy.bed_temp_max:
                        raise PolicyError("bed temperature exceeds production policy")
                elif command in {"SET_VELOCITY_LIMIT", "SET_TMC_CURRENT", "M204"}:
                    _check_limit_command(command, params, policy)
                elif command in {"M220", "M221"}:
                    value = _number(params, "S")
                    if value is None or not 0 < value <= 100:
                        raise PolicyError(f"{command} may reduce, but may not raise, its percentage")
                elif command == "M106":
                    value = _number(params, "S")
                    if value is not None and not 0 <= value <= 255:
                        raise PolicyError("fan power must be between 0 and 255")
                elif command == "G4":
                    milliseconds = _number(params, "P") or 0.0
                    seconds = _number(params, "S") or 0.0
                    if milliseconds < 0 or seconds < 0 or milliseconds / 1000.0 + seconds > 60:
                        raise PolicyError("G4 dwell exceeds the 60 second production limit")
                elif command == "M73":
                    for key in ("P", "R"):
                        value = _number(params, key)
                        if value is not None and value < 0:
                            raise PolicyError("M73 progress values may not be negative")
                elif command == "G20":
                    raise PolicyError("inch-mode G-code is not admitted")
                elif command == "G21":
                    pass
                elif command == "G90":
                    absolute_axes = True
                elif command == "G91":
                    absolute_axes = False
                elif command == "M82":
                    absolute_extruder = True
                elif command == "M83":
                    absolute_extruder = False
                elif command == "G92":
                    if any(
                        _number(params, axis) is not None
                        for axis in ("X", "Y", "Z")
                    ):
                        raise PolicyError(
                            "G92 may reset only the E axis in production"
                        )
                    for axis in position:
                        value = _number(params, axis)
                        if value is not None:
                            position[axis] = value
                elif command in {"G0", "G1", "G2", "G3"}:
                    feed = _number(params, "F")
                    if feed is not None and feed <= 0:
                        raise PolicyError("motion feed rate must be greater than zero")
                    if command in {"G2", "G3"}:
                        for key in ("I", "J", "K", "R"):
                            _number(params, key)
                    before = dict(position)
                    unknown_moving_origin = False
                    moved_axes: set[str] = set()
                    for axis in ("X", "Y", "Z"):
                        value = _number(params, axis)
                        if value is None:
                            continue
                        current = position[axis]
                        if absolute_axes:
                            target = value
                            if current is None:
                                unknown_moving_origin = True
                            if current is None or target != current:
                                moved_axes.add(axis)
                        else:
                            if current is None:
                                if value == 0:
                                    continue
                                raise PolicyError(
                                    f"relative {axis} move has no known absolute origin"
                                )
                            target = current + value
                            if value != 0:
                                moved_axes.add(axis)
                        limit = _bounds(policy, axis)
                        if not limit.minimum <= target <= limit.maximum:
                            raise PolicyError(
                                f"{axis} target {target:g} is outside {limit.minimum:g}..{limit.maximum:g}"
                            )
                        position[axis] = target
                    e_value = _number(params, "E")
                    e_delta = 0.0
                    if e_value is not None:
                        current_e = position["E"] or 0.0
                        e_delta = e_value - current_e if absolute_extruder else e_value
                        position["E"] = e_value if absolute_extruder else current_e + e_value
                    moved_xy = bool(moved_axes.intersection({"X", "Y"}))
                    if e_value is not None and (e_delta < 0 or not moved_xy):
                        if abs(e_delta) > policy.max_extrude_only_distance:
                            raise PolicyError("E-only move exceeds max_extrude_only_distance")
                        if not moved_xy and e_delta > policy.max_stationary_positive_extrude:
                            raise PolicyError("stationary positive extrusion exceeds production policy")
                    if e_delta > 0 and moved_xy:
                        if unknown_moving_origin:
                            raise PolicyError(
                                "extruding travel begins from an unknown post-purge position"
                            )
                        distance = math.sqrt(
                            sum(
                                ((position[axis] or 0.0) - (before[axis] or 0.0)) ** 2
                                for axis in ("X", "Y", "Z")
                            )
                        )
                        if distance <= 0:
                            raise PolicyError("extruding travel has no measurable distance")
                        filament_area = math.pi * (policy.filament_diameter / 2.0) ** 2
                        cross_section = e_delta * filament_area / distance
                        if cross_section > policy.max_extrude_cross_section:
                            raise PolicyError(
                                f"extrusion cross-section {cross_section:.3f} exceeds policy"
                            )
            except PolicyError as exc:
                add_finding(line_number, command, str(exc))

        final_stat = os.fstat(handle.fileno())
    if (
        final_stat.st_dev != stat_result.st_dev
        or final_stat.st_ino != stat_result.st_ino
        or final_stat.st_size != stat_result.st_size
        or final_stat.st_mtime_ns != stat_result.st_mtime_ns
    ):
        raise PolicyError("G-code changed while it was scanned")
    digest = digest_state.hexdigest()

    if start_line is None:
        add_finding(0, "START_PRINT", "required START_PRINT is missing")
    if end_line is None:
        add_finding(0, "END_PRINT", "required END_PRINT is missing")
    return ScanReport(
        path=str(path),
        sha256=digest,
        size=stat_result.st_size,
        lines=lines,
        object_count=objects,
        timelapse_frames=timelapse_frames,
        start_line=start_line,
        end_line=end_line,
        policy_sha256=policy_sha256(policy_path),
        findings=findings,
    )


def approval_id(relative_path: str, digest: str) -> str:
    if not SHA256_RE.fullmatch(digest):
        raise PolicyError("approval digest is malformed")
    path = Path(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise PolicyError("approval path is malformed")
    value = relative_path.encode("utf-8") + b"\0" + digest.encode("ascii")
    return hashlib.sha256(value).hexdigest()


def approval_path(approval_dir: Path, relative_path: str, digest: str) -> Path:
    return approval_dir / f"{approval_id(relative_path, digest)}.json"


def spool_path(spool_dir: Path, digest: str) -> Path:
    if not SHA256_RE.fullmatch(digest):
        raise PolicyError("spool digest is malformed")
    return spool_dir / f"{digest}.gcode"


def _verify_real_directory(path: Path, description: str) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_dir():
        raise PolicyError(f"{description} must be one real directory")
    return path.resolve(strict=True)


def _snapshot_source(source: Path, destination_dir: Path, max_bytes: int) -> Path:
    source_fd, before = _open_regular_readonly(source)
    if before.st_size > max_bytes:
        os.close(source_fd)
        raise PolicyError(f"G-code exceeds the {max_bytes} byte policy limit")
    try:
        destination_fd, temporary_name = tempfile.mkstemp(
            prefix=".gcode-snapshot-", dir=destination_dir
        )
    except Exception:
        os.close(source_fd)
        raise
    temporary = Path(temporary_name)
    copied = 0
    try:
        with os.fdopen(source_fd, "rb") as input_handle, os.fdopen(
            destination_fd, "wb"
        ) as output_handle:
            while True:
                block = input_handle.read(1024 * 1024)
                if not block:
                    break
                copied += len(block)
                if copied > max_bytes:
                    raise PolicyError(
                        f"G-code exceeds the {max_bytes} byte policy limit"
                    )
                output_handle.write(block)
            output_handle.flush()
            os.fsync(output_handle.fileno())
            after = os.fstat(input_handle.fileno())
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or copied != before.st_size
        ):
            raise PolicyError("G-code changed while its protected snapshot was made")
        os.chmod(temporary, 0o440)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _commit_snapshot(temporary: Path, spool_dir: Path, digest: str, size: int) -> Path:
    target = spool_path(spool_dir, digest)
    if target.exists():
        info = target.lstat()
        if (
            target.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or info.st_size != size
            or sha256_file(target) != digest
        ):
            raise PolicyError("existing protected G-code snapshot is corrupt")
        temporary.unlink()
        return target
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        return _commit_snapshot(temporary, spool_dir, digest, size)
    temporary.unlink()
    directory_fd = os.open(spool_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return target


def admit_gcode(
    source: Path,
    policy: GCodePolicy,
    policy_path: Path,
    approval_dir: Path,
    gcode_root: Path,
    spool_dir: Path,
) -> tuple[ScanReport, Path | None]:
    source = source.expanduser().absolute()
    if source.is_symlink():
        raise PolicyError("G-code may not be a symlink")
    source = source.resolve(strict=True)
    root = _verify_real_directory(gcode_root, "G-code root")
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise PolicyError("G-code is outside the configured virtual SD root") from exc
    approval_dir = _verify_real_directory(approval_dir, "approval directory")
    spool_dir = _verify_real_directory(spool_dir, "protected G-code directory")
    source_size = source.stat().st_size
    spool_bytes = 0
    for item in spool_dir.iterdir():
        info = item.lstat()
        if item.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise PolicyError("protected G-code directory contains an unsafe object")
        spool_bytes += info.st_size
    if spool_bytes + source_size > policy.max_spool_bytes:
        raise PolicyError("protected G-code storage ceiling would be exceeded")
    if shutil.disk_usage(spool_dir).free - source_size < policy.min_system_free_bytes:
        raise PolicyError("system free-space reserve would be crossed by admission")
    temporary = _snapshot_source(source, spool_dir, policy.max_file_bytes)
    try:
        report = scan_gcode(temporary, policy, policy_path)
        report.path = str(source)
        if not report.accepted:
            return report, None
        protected = _commit_snapshot(temporary, spool_dir, report.sha256, report.size)
    finally:
        temporary.unlink(missing_ok=True)
    relative_name = relative.as_posix()
    payload = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "scanner_version": SCANNER_VERSION,
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "relative_path": relative_name,
        "sha256": report.sha256,
        "size": report.size,
        "policy_sha256": report.policy_sha256,
        "object_count": report.object_count,
        "spool_file": protected.name,
    }
    target = approval_path(approval_dir, relative_name, report.sha256)
    fd, temporary_name = tempfile.mkstemp(prefix=".approval-", dir=approval_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o440)
        os.replace(temporary, target)
        directory_fd = os.open(approval_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return report, target


def format_findings(findings: Iterable[ScanFinding]) -> str:
    return "\n".join(
        f"line {item.line}: {item.command or '<decode>'}: {item.message}" for item in findings
    )

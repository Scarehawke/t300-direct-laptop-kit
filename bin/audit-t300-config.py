#!/usr/bin/env python3
"""Audit an offline T300 Klipper configuration tree without changing it."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import glob
import json
from pathlib import Path
import re
import sys
from typing import Iterable


HEADER_RE = re.compile(r"^\s*\[\s*([^]]+?)\s*\]\s*(?:[#;].*)?$")
OPTION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*?)\s*$")
PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "INFO": 3}
KLIPPER_ARGUMENT_RE = re.compile(r"([A-Z_]+|[A-Z*/])")


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class Section:
    kind: str
    name: str
    header: str
    path: Path
    line: int
    raw: str
    options: tuple[tuple[str, str, int], ...]

    @property
    def identity(self) -> str:
        return self.header.casefold()

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"

    def values(self, option: str) -> list[tuple[str, int]]:
        target = option.casefold()
        return [(value, line) for key, value, line in self.options if key == target]

    def value(self, option: str) -> str | None:
        values = self.values(option)
        return values[-1][0] if values else None


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    title: str
    explanation: str
    location: str


@dataclass
class ConfigTree:
    root: Path
    entry: Path
    files: list[Path]
    sections: list[Section]
    missing_includes: list[tuple[str, str]]


def clean_value(value: str) -> str:
    return re.split(r"\s+[#;]", value, maxsplit=1)[0].strip()


def command_position(section: Section, command: str) -> int:
    pattern = re.compile(rf"^\s*{re.escape(command)}\b", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(section.raw)
    return match.start() if match else -1


def klipper_parsed_command(name: str) -> str:
    """Mirror Klipper 0.12's legacy first-token parser."""
    parts = KLIPPER_ARGUMENT_RE.split(name.upper())
    if len(parts) >= 3 and parts[1] != "N":
        return parts[1] + parts[2].strip()
    if len(parts) >= 5 and parts[1] == "N":
        return parts[3] + parts[4].strip()
    return ""


def split_header(header: str) -> tuple[str, str]:
    parts = header.strip().split(None, 1)
    return parts[0].casefold(), parts[1].strip() if len(parts) == 2 else ""


def parse_sections(path: Path, display_path: Path) -> list[Section]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditError(f"Could not read {path}: {exc}") from exc
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = HEADER_RE.match(line)
        if match:
            starts.append((index, match.group(1).strip()))
    sections: list[Section] = []
    for position, (start, header) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        kind, name = split_header(header)
        options: list[tuple[str, str, int]] = []
        for offset, line in enumerate(lines[start + 1 : end], start + 2):
            match = OPTION_RE.match(line)
            if match:
                options.append((match.group(1).casefold(), clean_value(match.group(2)), offset))
        sections.append(
            Section(
                kind=kind,
                name=name,
                header=header,
                path=display_path,
                line=start + 1,
                raw="\n".join(lines[start:end]),
                options=tuple(options),
            )
        )
    return sections


def load_tree(value: Path) -> ConfigTree:
    supplied = value.expanduser().resolve()
    if supplied.is_dir():
        if (supplied / "printer.cfg").is_file():
            root = supplied
            entry = supplied / "printer.cfg"
        elif (supplied / "config-root" / "printer.cfg").is_file():
            root = supplied / "config-root"
            entry = root / "printer.cfg"
        else:
            raise AuditError(f"No printer.cfg found below {supplied}")
    elif supplied.is_file():
        root = supplied.parent
        entry = supplied
    else:
        raise AuditError(f"Configuration path does not exist: {supplied}")

    files: list[Path] = []
    sections: list[Section] = []
    missing: list[tuple[str, str]] = []
    visited: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        try:
            display = resolved.relative_to(root)
        except ValueError as exc:
            raise AuditError(f"Include escapes the configuration root: {resolved}") from exc
        if resolved in visited:
            return
        visited.add(resolved)
        files.append(display)
        parsed = parse_sections(resolved, display)
        for section in parsed:
            sections.append(section)
            if section.kind != "include":
                continue
            pattern = section.name
            if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                missing.append((section.location, f"unsafe include {pattern!r}"))
                continue
            matches = sorted(Path(item) for item in glob.glob(str(root / pattern)))
            matches = [item for item in matches if item.is_file()]
            if not matches:
                missing.append((section.location, pattern))
                continue
            for match in matches:
                visit(match)

    visit(entry)
    return ConfigTree(root=root, entry=entry, files=files, sections=sections, missing_includes=missing)


def as_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def truthy(value: str | None) -> bool:
    return bool(value and value.strip().casefold() in {"1", "true", "yes", "on"})


def first_section(tree: ConfigTree, kind: str, name: str = "") -> Section | None:
    target_kind = kind.casefold()
    target_name = name.casefold()
    matches = [
        section
        for section in tree.sections
        if section.kind == target_kind and section.name.casefold() == target_name
    ]
    if not matches:
        return None
    values: dict[str, tuple[str, int]] = {}
    for section in matches:
        for key, value, line in section.options:
            values[key] = (value, line)
    raw_owner = next(
        (section for section in reversed(matches) if section.values("gcode")),
        matches[-1],
    )
    return Section(
        kind=target_kind,
        name=matches[0].name,
        header=matches[0].header,
        path=raw_owner.path,
        line=raw_owner.line,
        raw=raw_owner.raw,
        options=tuple((key, value, line) for key, (value, line) in values.items()),
    )


def option_location(tree: ConfigTree, kind: str, name: str, option: str) -> str:
    for section in reversed(tree.sections):
        if section.kind != kind.casefold() or section.name.casefold() != name.casefold():
            continue
        values = section.values(option)
        if values:
            return f"{section.path}:{values[-1][1]}"
    return "configuration"


def macro(tree: ConfigTree, name: str) -> Section | None:
    return first_section(tree, "gcode_macro", name)


def add(
    findings: list[Finding],
    severity: str,
    code: str,
    title: str,
    explanation: str,
    section: Section | None = None,
    location: str = "configuration",
) -> None:
    findings.append(
        Finding(
            severity=severity,
            code=code,
            title=title,
            explanation=explanation,
            location=section.location if section else location,
        )
    )


def audit(tree: ConfigTree) -> list[Finding]:
    findings: list[Finding] = []
    for location, include in tree.missing_includes:
        add(
            findings,
            "P0",
            "CFG001",
            "Included file is missing",
            f"Klipper cannot load this tree until {include!r} is present.",
            location=location,
        )

    seen_macro_names: set[str] = set()
    for section in reversed(tree.sections):
        if section.kind != "gcode_macro" or section.name.casefold() in seen_macro_names:
            continue
        seen_macro_names.add(section.name.casefold())
        parsed = klipper_parsed_command(section.name)
        if parsed != section.name.upper():
            add(
                findings,
                "P0",
                "MACRO006",
                "Macro name cannot be invoked on Klipper 0.12",
                f"Klipper parses {section.name!r} as {parsed!r}. Avoid digits followed by more letters or underscores.",
                section,
            )
        if re.search(
            r'''(?mi)^\s*RESPOND\b[^\n]*\bMSG\s*=\s*(["']).*;.*\1\s*$''',
            section.raw,
        ):
            add(
                findings,
                "P0",
                "MACRO007",
                "RESPOND message is truncated as a G-code comment",
                "Klipper 0.12 treats a semicolon as the start of a comment even inside a quoted RESPOND message.",
                section,
            )

    by_identity: dict[str, list[Section]] = {}
    for section in tree.sections:
        if section.kind == "include":
            continue
        by_identity.setdefault(section.identity, []).append(section)
        seen_options: dict[str, list[tuple[str, int]]] = {}
        for key, value, line in section.options:
            seen_options.setdefault(key, []).append((value, line))
        for key, entries in seen_options.items():
            if len(entries) > 1:
                values = {value.casefold() for value, _ in entries}
                lines = [line for _, line in entries]
                add(
                    findings,
                    "INFO" if len(values) == 1 else "P1",
                    "CFG005" if len(values) == 1 else "CFG003",
                    (
                        f"Redundant identical {key} setting"
                        if len(values) == 1
                        else f"Conflicting duplicate {key} setting"
                    ),
                    (
                        f"The same effective value is repeated on lines {lines}."
                        if len(values) == 1
                        else f"Only the last value is effective; definitions occur on lines {lines}."
                    ),
                    section,
                )
    managed_overrides: dict[str, list[str]] = {}
    for duplicate in by_identity.values():
        if len(duplicate) > 1:
            has_options = any(item.options for item in duplicate)
            owner = duplicate[-1].path.name
            managed_override = owner in {
                "t300_core.cfg",
                "mainsail_client.cfg",
                "t300_runtime.cfg",
                "macro_z_tilt_via_knob.cfg",
            }
            if managed_override:
                managed_overrides.setdefault(owner, []).append(duplicate[0].header)
                continue
            add(
                findings,
                "INFO" if not has_options else "P1",
                "CFG002",
                f"Merged [{duplicate[0].header}] section",
                "Klipper 0.12 merges repeated sections; later options replace earlier ones. Owners: "
                + ", ".join(item.location for item in duplicate)
                + (
                    ". Review the merged values."
                    if has_options
                    else ". These copies are empty."
                ),
                duplicate[0],
            )
    for owner, headers in managed_overrides.items():
        add(
            findings,
            "INFO",
            "CFG004",
            f"{owner} owns reviewed settings",
            "Klipper merged this later component for: "
            + ", ".join(f"[{name}]" for name in sorted(headers, key=str.casefold)),
            location=owner,
        )

    idle = first_section(tree, "idle_timeout")
    if idle and command_position(idle, "TURN_OFF_HEATERS") < 0:
        add(
            findings,
            "P0",
            "SAFE001",
            "Idle timeout leaves heaters on",
            "Defining custom idle G-code replaces Klipper's default heater shutdown. "
            "A forgotten preheat can therefore remain hot indefinitely.",
            idle,
        )

    extruder = first_section(tree, "extruder")
    if extruder:
        nozzle = as_number(extruder.value("nozzle_diameter")) or 0.4
        normal_guard = 4.0 * nozzle * nozzle
        cross_section = as_number(extruder.value("max_extrude_cross_section"))
        if cross_section is not None and cross_section > max(5.0, normal_guard * 10.0):
            add(
                findings,
                "P0",
                "SAFE002",
                "Extrusion cross-section guard is effectively disabled",
                f"Configured {cross_section:g} mm^2; Klipper's normal 0.4 mm-nozzle "
                f"default is {normal_guard:.2f} mm^2. Malformed G-code could command a grossly over-wide XY extrusion move.",
                location=option_location(tree, "extruder", "", "max_extrude_cross_section"),
            )
        velocity = as_number(extruder.value("max_extrude_only_velocity"))
        if velocity is not None and velocity > 100:
            add(
                findings,
                "P1",
                "SAFE003",
                "Extruder-only velocity is unusually high",
                f"Configured {velocity:g} mm/s. Normal loading and the current Orca retractions "
                "do not need anything close to this limit.",
                location=option_location(tree, "extruder", "", "max_extrude_only_velocity"),
            )
        accel = as_number(extruder.value("max_extrude_only_accel"))
        if accel is not None and accel > 5000:
            add(
                findings,
                "P1",
                "SAFE004",
                "Extruder-only acceleration is unusually high",
                f"Configured {accel:g} mm/s^2; this provides little useful protection from a bad E-only move.",
                location=option_location(tree, "extruder", "", "max_extrude_only_accel"),
            )
        corner = as_number(extruder.value("instantaneous_corner_velocity"))
        if corner is not None and corner > 5:
            add(
                findings,
                "P1",
                "SAFE005",
                "Extruder instantaneous velocity jump is unusually high",
                f"Configured {corner:g} mm/s; upstream Klipper defaults to 1 mm/s.",
                location=option_location(tree, "extruder", "", "instantaneous_corner_velocity"),
            )

    force_move = first_section(tree, "force_move")
    if force_move and truthy(force_move.value("enable_force_move")):
        add(
            findings,
            "P1",
            "PLR001",
            "Force-move commands are enabled",
            "This is needed by the vendor recovery path, but it bypasses normal homing checks. "
            "Keep it only after the complete recovery workflow is audited.",
            location=option_location(tree, "force_move", "", "enable_force_move"),
        )
    interrupted = macro(tree, "RESUME_INTERRUPTED")
    if interrupted and "SET_KINEMATIC_POSITION" in interrupted.raw.upper():
        add(
            findings,
            "P0",
            "PLR002",
            "Power-loss resume invents axis positions",
            "SET_KINEMATIC_POSITION marks axes without measuring them. The external recovery "
            "script and touchscreen call path must be captured before this can be trusted.",
            interrupted,
        )

    sensor = first_section(tree, "filament_switch_sensor", "my_sensor")
    if sensor and truthy(sensor.value("pause_on_runout")):
        runout_match = re.search(
            r"(?ms)^\s*runout_gcode\s*:\s*\n(?P<body>(?:[ \t]+.*\n?)*)",
            sensor.raw,
        )
        runout = runout_match.group("body") if runout_match else ""
        if re.search(r"(?mi)^\s*(?:M600|PAUSE)\b", runout):
            add(
                findings,
                "P0",
                "RUNOUT001",
                "Filament runout pauses twice",
                "pause_on_runout already invokes PAUSE before runout_gcode; the post-action must not invoke PAUSE or M600 again.",
                sensor,
            )

    lifecycle_names = ["START_PRINT", "END_PRINT", "PAUSE", "RESUME", "CANCEL_PRINT", "M600"]
    owners = [item for name in lifecycle_names if (item := macro(tree, name))]
    if owners:
        add(
            findings,
            "INFO",
            "MACRO000",
            "Lifecycle macro owner map",
            ", ".join(f"{item.name}={item.location}" for item in owners),
            owners[0],
        )

    start = macro(tree, "START_PRINT")
    if start:
        upper = start.raw.upper()
        if "PARAMS." not in upper:
            add(
                findings,
                "P1",
                "MACRO001",
                "START_PRINT has no explicit slicer parameters",
                "It guesses from pre-existing heater targets, so command ordering in every G-code file "
                "changes its behavior.",
                start,
            )
        hot_index = command_position(start, "M109")
        mesh_index = command_position(start, "BED_MESH_CALIBRATE")
        if hot_index >= 0 and mesh_index > hot_index:
            add(
                findings,
                "P1",
                "MACRO002",
                "Nozzle reaches print temperature before probing",
                "A hot nozzle can ooze through the mesh cycle and leave a blob for the first travel to hit.",
                start,
            )
        if "_PRINT_START_WAIT" in upper or "VARIABLE_STATE" in upper:
            add(
                findings,
                "P1",
                "MACRO003",
                "START_PRINT uses delayed hidden state",
                "The same command runs in two phases, which makes pause, cancel, and slicer ordering harder to reason about.",
                start,
            )
        if re.search(r"\.enable(?!d)", start.raw, re.IGNORECASE):
            add(
                findings,
                "P1",
                "MACRO004",
                "START_PRINT reads the wrong filament-sensor field",
                "Klipper 0.12 exposes `enabled`; the singular `enable` check cannot reliably guard a missing filament.",
                start,
            )
        for line in start.raw.splitlines():
            move = re.match(r"^\s*G[01]\s+(.+)$", line, re.IGNORECASE)
            if move is None:
                continue
            words = move.group(1).upper()
            extrusion = re.search(r"(?:^|\s)E([0-9.]+)(?:\s|$)", words)
            has_axis = re.search(r"(?:^|\s)[XYZ]-?[0-9.{]", words)
            if extrusion and float(extrusion.group(1)) > 0 and not has_axis:
                add(
                    findings,
                    "P0",
                    "MACRO005",
                    "START_PRINT extrudes while stationary",
                    "A positive E-only move can build a ball on the nozzle before the moving purge begins.",
                    start,
                )
                break

    for name in ("PAUSE", "RESUME"):
        section = macro(tree, name)
        if section and "PARAMS.STATE" in section.raw.upper() and "PARAMS.STATE|DEFAULT" not in section.raw.upper():
            add(
                findings,
                "P0",
                f"MACRO_{name}_PARAM",
                f"{name} assumes a STATE parameter exists",
                f"A normal touchscreen or Mainsail {name.title()} call supplies no STATE value and can fail in the recovery path.",
                section,
            )
    cancel = macro(tree, "CANCEL_PRINT")
    if cancel and re.search(r"\.homed_axe(?!s)", cancel.raw, re.IGNORECASE):
        add(
            findings,
            "P0",
            "MACRO_CANCEL_HOME",
            "CANCEL_PRINT checks a misspelled homing field",
            "Klipper exposes `homed_axes`. The current branch can attempt park moves without a valid homing check.",
            cancel,
        )
    for name in ("END_PRINT", "RESUME"):
        section = macro(tree, name)
        if section and re.search(r"\.enable(?!d)", section.raw, re.IGNORECASE):
            add(
                findings,
                "P1",
                f"MACRO_{name}_SENSOR",
                f"{name} reads the wrong filament-sensor field",
                "Klipper 0.12 exposes `enabled`, not `enable`.",
                section,
            )

    mesh_macro = macro(tree, "BED_MESH_CALIBRATE")
    if mesh_macro and mesh_macro.value("rename_existing"):
        upper = mesh_macro.raw.upper()
        if "PARAMS" not in upper and "BED_MESH_CALIBRATE_BASE" in upper:
            add(
                findings,
                "P1",
                "MESH001",
                "Mesh wrapper discards caller settings",
                "Requested profile names, bounds, and probe counts are not forwarded to Klipper's base command.",
                mesh_macro,
            )
        if "ADAPTIVE=1" in upper:
            add(
                findings,
                "INFO",
                "MESH002",
                "Vendor adaptive-mesh extension is in use",
                "Upstream Klipper 0.12.0 has no ADAPTIVE parameter. Capture the live vendor bed_mesh.py before replacing this wrapper.",
                mesh_macro,
            )

    kamp_settings = macro(tree, "_KAMP_Settings")
    if kamp_settings:
        tip = as_number(kamp_settings.value("variable_tip_distance"))
        if tip is not None and tip > 0:
            add(
                findings,
                "P1",
                "PURGE001",
                "KAMP advances filament while stationary",
                f"tip_distance is {tip:g} mm. This runs as an E-only move immediately before the moving purge line.",
                kamp_settings,
            )

    mesh = first_section(tree, "bed_mesh")
    if mesh:
        speed = as_number(mesh.value("speed"))
        if speed is not None and speed > 250:
            add(
                findings,
                "P2",
                "MESH003",
                "Mesh travel speed needs a repeatability test",
                f"Configured {speed:g} mm/s. Compare PROBE_ACCURACY and repeated meshes before deciding whether to lower it.",
                mesh,
            )
        if mesh.value("fade_target") is not None:
            add(
                findings,
                "P2",
                "MESH004",
                "Mesh fade changes the model's effective Z scale",
                "With a strongly warped mesh, fade_target can shift or stretch Z. Keep it only after the new mesh range is measured.",
                mesh,
            )

    resonance = first_section(tree, "resonance_tester")
    if resonance:
        accel_per_hz = as_number(resonance.value("accel_per_hz"))
        if accel_per_hz is not None and accel_per_hz > 100:
            add(
                findings,
                "P1",
                "MOTION001",
                "Resonance-test excitation is aggressive",
                f"Configured {accel_per_hz:g} mm/s^2 per Hz; upstream Klipper's documented default is 75. "
                "Do not run the test until the accelerometer and machine stability are checked.",
                resonance,
            )
    shaper = first_section(tree, "input_shaper")
    if shaper:
        damping = [
            value
            for key in ("damping_ratio_x", "damping_ratio_y")
            if (value := as_number(shaper.value(key))) is not None
        ]
        if damping and min(damping) < 0.05:
            add(
                findings,
                "P2",
                "MOTION002",
                "Saved input-shaper damping values are unusually low",
                "Treat the current shapers as factory placeholders until fresh ADXL345 data is captured and graphed.",
                shaper,
            )

    for name in ("M109", "M190"):
        section = macro(tree, name)
        native_alias = "M99109" if name == "M109" else "M99190"
        forwards_native = bool(
            section
            and command_position(section, native_alias) >= 0
            and "TEMPERATURE_WAIT" not in section.raw.upper()
        )
        if section and section.value("rename_existing") and not forwards_native:
            add(
                findings,
                "P2",
                f"TEMP_{name}",
                f"Factory macro overrides standard {name}",
                "This narrows the wait to a +/-1 C band and changes standard G-code semantics. "
                "Use the native alias for ordinary heating; a reviewed lifecycle may deliberately "
                "use bounded M190 after the vendor's fixed 65 C mesh to wait for cooling.",
                section,
            )

    if first_section(tree, "exclude_object"):
        add(
            findings,
            "INFO",
            "MESH000",
            "Exclude-object support is configured",
            "Orca files that emit EXCLUDE_OBJECT_DEFINE before the start macro can provide object bounds for adaptive meshing.",
            first_section(tree, "exclude_object"),
        )
    if not first_section(tree, "axis_twist_compensation"):
        add(
            findings,
            "INFO",
            "CAL001",
            "Axis-twist compensation is not configured",
            "That is correct for now. Measure repeatable X-axis probe bias first; do not enable compensation by guesswork.",
            location=str(tree.entry.relative_to(tree.root)),
        )
    return sorted(findings, key=lambda item: (PRIORITY[item.severity], item.code, item.location))


def format_text(tree: ConfigTree, findings: Iterable[Finding]) -> str:
    items = list(findings)
    counts = {severity: sum(item.severity == severity for item in items) for severity in PRIORITY}
    lines = [
        "T300 offline configuration audit",
        f"Root: {tree.root}",
        f"Active files read: {len(tree.files)}",
        "Findings: " + ", ".join(f"{key}={counts[key]}" for key in PRIORITY),
        "",
    ]
    for item in items:
        lines.extend(
            [
                f"[{item.severity}] {item.code}  {item.title}",
                f"  {item.location}",
                f"  {item.explanation}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="printer.cfg, config-root, or backup directory")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return status 1 when P0 findings are present",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        tree = load_tree(args.path)
        findings = audit(tree)
        if args.json:
            print(
                json.dumps(
                    {
                        "root": str(tree.root),
                        "entry": str(tree.entry),
                        "files": [str(path) for path in tree.files],
                        "findings": [asdict(item) for item in findings],
                    },
                    indent=2,
                )
            )
        else:
            print(format_text(tree, findings), end="")
        return 1 if args.strict and any(item.severity == "P0" for item in findings) else 0
    except AuditError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

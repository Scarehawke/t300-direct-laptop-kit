"""Compatibility policy for the stock T300 serial-touchscreen bridge.

The proprietary bridge is treated as an untrusted legacy client.  This module
contains no transport or printer-control code: it only reviews and rewrites
JSON-RPC messages so the gateway can be small and independently tested.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


class TouchscreenPolicyError(ValueError):
    """Raised when a bridge request is malformed or outside the contract."""


@dataclass(frozen=True)
class TouchscreenDecision:
    outcome: str
    reason: str
    request: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"forward", "emulate_success", "reject"}:
            raise ValueError("unknown touchscreen decision outcome")
        if self.outcome == "forward" and self.request is None:
            raise ValueError("forward decisions require a request")
        if self.outcome != "forward" and self.request is not None:
            raise ValueError("local decisions cannot contain a forwarded request")


OBJECT_ALIASES = {
    "filament_switch_sensor my_sensor": "filament_switch_sensor filament_runout",
    "heater_fan fan1": "heater_fan hotend_fan",
    "heater_fan my_nozzle_fan1": "heater_fan hotend_fan",
}

SYNTHETIC_OBJECTS: dict[str, dict[str, Any]] = {
    "output_pin caselight": {"value": 0.0},
    "output_pin fan0": {"value": 0.0},
    "output_pin fan2": {"value": 0.0},
    "output_pin sound": {"value": 0.0},
}

# The stock bridge builds its Macros page from every [gcode_macro] section in
# configfile.settings. Dedicated print and filament controls already expose the
# actuator macros, so keep this page supplemental and read-only.
TOUCHSCREEN_VISIBLE_MACRO_SECTIONS = {
    "gcode_macro printer_status",
}

NO_PARAMETER_METHODS = {
    "printer.info",
    "printer.emergency_stop",
    "printer.firmware_restart",
    "printer.print.pause",
    "printer.print.resume",
    "printer.print.cancel",
    "printer.restart",
    "server.files.roots",
    "server.history.totals",
    "server.info",
}

ALLOWED_OBJECTS = {
    "bed_mesh",
    "configfile",
    "display_status",
    "extruder",
    "fan",
    "filament_switch_sensor filament_runout",
    "firmware_retraction",
    "gcode_move",
    "heater_bed",
    "heater_fan hotend_fan",
    "idle_timeout",
    "pause_resume",
    "print_stats",
    "probe",
    "toolhead",
    "webhooks",
}

BLOCKED_METHOD_PREFIXES = (
    "machine.update.",
    "machine.package.",
    "machine.reboot",
    "machine.shutdown",
    "machine.services.",
    "server.database.",
    "server.files.delete_",
    "server.files.move",
)

NOOP_GCODE = {
    "SDCARD_RESET_FILE": "CANCEL_PRINT owns virtual-SD cleanup on the candidate.",
    "CLEAR_LAST_FILE": "The candidate has no vendor power-loss last-file state.",
    "RUN_SHELL_COMMAND CMD=CLEAR_PLR": "Vendor power-loss shell cleanup is not installed.",
}

MAINTENANCE_ONLY = {
    "Z_TILT_CALIBRATION",
    "PROBE_CALIBRATE",
    "HEATED_BED",
    "TESTZ",
    "ACCEPT",
    "ABORT",
    "SAVE_CONFIG",
    "Z_TILT_ADJUST",
}

DISABLED_FEATURE_GCODE = {
    "RESUME_INTERRUPTED": "Vendor power-loss recovery is intentionally unavailable on the candidate.",
}

DIRECT_GCODE = {
    "G28",
    "G28 X",
    "G28 Y",
    "G28 X Y",
    "G28 Z",
    "M84",
    "PAUSE",
    "RESUME",
    "CANCEL_PRINT",
    "M600",
    "PRINTER_STATUS",
}

_JOG_RE = re.compile(
    r"^G91\s*\n\s*G1\s+([XYZ])([+-]?\d+(?:\.\d+)?)\s+F(\d+(?:\.\d+)?)\s*\n\s*G90$",
    re.IGNORECASE,
)
_EXTRUDE_RE = re.compile(
    r"^G1\s+E([+-]?\d+(?:\.\d+)?)\s+F(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_HEATER_RE = re.compile(
    r"^SET_HEATER_TEMPERATURE\s+HEATER=(EXTRUDER|HEATER_BED)\s+TARGET=([+-]?\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_FAN_RE = re.compile(r"^M106(?:\s+S(\d+(?:\.\d+)?))?$", re.IGNORECASE)
_SPEED_RE = re.compile(r"^M220\s+S(\d+(?:\.\d+)?)$", re.IGNORECASE)
_FLOW_RE = re.compile(r"^M221\s+S(\d+(?:\.\d+)?)$", re.IGNORECASE)
_OFFSET_RE = re.compile(
    r"^SET_GCODE_OFFSET\s+Z_ADJUST=([+-]?\d+(?:\.\d+)?)\s+MOVE=1$",
    re.IGNORECASE,
)
_SENSOR_RE = re.compile(
    r"^SET_FILAMENT_SENSOR\s+SENSOR=MY_SENSOR\s+ENABLE=([01])$",
    re.IGNORECASE,
)


def _normal_script(script: str) -> str:
    return "\n".join(line.strip() for line in script.strip().splitlines() if line.strip())


def _reject(reason: str) -> TouchscreenDecision:
    return TouchscreenDecision("reject", reason)


def _review_envelope(request: Mapping[str, Any]) -> TouchscreenDecision | None:
    if set(request) - {"jsonrpc", "id", "method", "params"}:
        return _reject("Touchscreen JSON-RPC request contains unknown fields.")
    if request.get("jsonrpc") != "2.0":
        return _reject("Touchscreen JSON-RPC version is invalid.")
    if "id" in request:
        request_id = request["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return _reject("Touchscreen JSON-RPC identifier is invalid.")
        if request_id < 0 or request_id > 2**63 - 1:
            return _reject("Touchscreen JSON-RPC identifier is outside the accepted range.")
    method = request.get("method")
    if not isinstance(method, str) or not method or len(method) > 128:
        return _reject("Touchscreen JSON-RPC method is missing or invalid.")
    return None


def _has_empty_params(request: Mapping[str, Any]) -> bool:
    return "params" not in request or request.get("params") == {}


def _review_no_parameter_api(request: Mapping[str, Any]) -> TouchscreenDecision:
    if not _has_empty_params(request):
        return _reject("Touchscreen request contains unexpected parameters.")
    return TouchscreenDecision(
        "forward", "Reviewed no-parameter API preserved.", deepcopy(dict(request))
    )


def _review_filename(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        return None, "Touchscreen file name is missing or too long."
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None, "Touchscreen file name contains control characters."
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return None, "Touchscreen file name is not one canonical relative path."
    parts = path.parts
    if parts and parts[0] == "sda1":
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None, "Touchscreen file name is outside the G-code root."
    canonical = PurePosixPath(*parts).as_posix()
    if PurePosixPath(canonical).suffix.lower() not in {".gcode", ".gco", ".g"}:
        return None, "Touchscreen selected a file that is not G-code."
    return canonical, None


def _review_file_api(request: Mapping[str, Any]) -> TouchscreenDecision:
    method = request["method"]
    params = request.get("params")
    if method == "server.files.list":
        if "params" in request and params not in ({}, {"root": "gcodes"}):
            return _reject("Touchscreen file-list request is outside the G-code root.")
        return TouchscreenDecision(
            "forward", "Read-only G-code listing preserved.", deepcopy(dict(request))
        )
    if not isinstance(params, Mapping) or set(params) != {"filename"}:
        return _reject("Touchscreen file request is malformed.")
    filename, error = _review_filename(params.get("filename"))
    if error is not None:
        return _reject(error)
    rewritten = deepcopy(dict(request))
    rewritten["params"] = {"filename": filename}
    return TouchscreenDecision(
        "forward", "Canonical G-code file request preserved.", rewritten
    )


def _review_history(request: Mapping[str, Any]) -> TouchscreenDecision:
    params = request.get("params", {})
    if not isinstance(params, Mapping):
        return _reject("Touchscreen history request is malformed.")
    allowed = {"before", "since", "limit", "start", "order"}
    if set(params) - allowed:
        return _reject("Touchscreen history request contains unsupported parameters.")
    for name in ("before", "since"):
        if name in params and (
            isinstance(params[name], bool)
            or not isinstance(params[name], (int, float))
            or not math.isfinite(float(params[name]))
            or float(params[name]) < 0
        ):
            return _reject("Touchscreen history time is invalid.")
    for name, ceiling in (("limit", 100), ("start", 10_000)):
        if name in params and (
            isinstance(params[name], bool)
            or not isinstance(params[name], int)
            or params[name] < 0
            or params[name] > ceiling
        ):
            return _reject("Touchscreen history range is outside the reviewed limit.")
    if "order" in params and params["order"] not in {"asc", "desc"}:
        return _reject("Touchscreen history order is invalid.")
    return TouchscreenDecision(
        "forward", "Bounded read-only history request preserved.", deepcopy(dict(request))
    )


def _script_request(request: Mapping[str, Any], script: str) -> dict[str, Any]:
    rewritten = deepcopy(dict(request))
    rewritten["params"] = {"script": script}
    return rewritten


def _finite_number(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise TouchscreenPolicyError("%s is not numeric" % label) from exc
    if not math.isfinite(parsed):
        raise TouchscreenPolicyError("%s must be finite" % label)
    return parsed


def _review_gcode(
    request: Mapping[str, Any], allow_explicit_restart: bool
) -> TouchscreenDecision:
    params = request.get("params")
    if not isinstance(params, Mapping) or set(params) != {"script"}:
        return TouchscreenDecision("reject", "Touchscreen G-code request is malformed.")
    script_value = params.get("script")
    if not isinstance(script_value, str) or not script_value.strip():
        return TouchscreenDecision("reject", "Touchscreen G-code request is empty.")
    script = _normal_script(script_value)
    upper = script.upper()

    if upper == "FIRMWARE_RESTART":
        if allow_explicit_restart:
            return TouchscreenDecision(
                "forward", "Explicit post-startup firmware restart preserved.", deepcopy(dict(request))
            )
        return TouchscreenDecision(
            "emulate_success",
            "The stock bridge startup restart is intentionally suppressed.",
        )
    if upper in NOOP_GCODE:
        return TouchscreenDecision("emulate_success", NOOP_GCODE[upper])
    if upper in DISABLED_FEATURE_GCODE:
        return TouchscreenDecision("reject", DISABLED_FEATURE_GCODE[upper])
    if upper in {"G90", "G91"}:
        return TouchscreenDecision(
            "emulate_success",
            "Standalone coordinate-mode changes from the legacy filament workflow are suppressed.",
        )
    if upper in MAINTENANCE_ONLY or upper.startswith("TESTZ "):
        return TouchscreenDecision(
            "reject",
            "This calibration control is available only in the separately armed maintenance session.",
        )
    if upper in DIRECT_GCODE:
        return TouchscreenDecision("forward", "Stock action preserved.", deepcopy(dict(request)))

    match = _JOG_RE.fullmatch(script)
    if match:
        axis, distance_text, feed_text = match.groups()
        distance = _finite_number(distance_text, "jog distance")
        feed = _finite_number(feed_text, "jog feed rate")
        if axis.upper() not in "XYZ" or abs(distance) not in {1.0, 5.0, 10.0, 50.0}:
            return TouchscreenDecision("reject", "Jog request is outside the traced stock distances.")
        if not 0 < feed <= 7800:
            return TouchscreenDecision("reject", "Jog feed rate is outside the stock ceiling.")
        rewritten = "T_SCREEN_JOG AXIS=%s DISTANCE=%s SPEED=%s" % (
            axis.upper(),
            format(distance, ".6g"),
            format(feed / 60.0, ".6g"),
        )
        return TouchscreenDecision(
            "forward",
            "Jog translated to the bounded attended touchscreen macro.",
            _script_request(request, rewritten),
        )

    match = _EXTRUDE_RE.fullmatch(script)
    if match:
        distance = _finite_number(match.group(1), "extrusion distance")
        feed = _finite_number(match.group(2), "extrusion feed rate")
        if abs(distance) not in {3.0, 5.0, 10.0} or not 0 < feed <= 300:
            return TouchscreenDecision(
                "reject", "Filament motion is outside the traced bounded touchscreen workflow."
            )
        rewritten = "T_SCREEN_FILAMENT DISTANCE=%s SPEED=%s" % (
            format(distance, ".6g"),
            format(feed / 60.0, ".6g"),
        )
        return TouchscreenDecision(
            "forward",
            "Filament motion translated to the bounded attended touchscreen macro.",
            _script_request(request, rewritten),
        )

    match = _HEATER_RE.fullmatch(upper)
    if match:
        heater, target_text = match.groups()
        target = _finite_number(target_text, "heater target")
        ceiling = 300.0 if heater == "EXTRUDER" else 100.0
        if target < 0 or target > ceiling:
            return TouchscreenDecision("reject", "Heater target exceeds the immutable candidate ceiling.")
        return TouchscreenDecision("forward", "Bounded heater target preserved.", deepcopy(dict(request)))

    match = _FAN_RE.fullmatch(upper)
    if match:
        speed = 255.0 if match.group(1) is None else _finite_number(match.group(1), "fan speed")
        if speed < 0 or speed > 255:
            return TouchscreenDecision("reject", "Fan command is outside the standard 0-255 range.")
        return TouchscreenDecision("forward", "Part-cooling fan action preserved.", deepcopy(dict(request)))

    if upper == "M107":
        return TouchscreenDecision("forward", "Part-cooling fan off action preserved.", deepcopy(dict(request)))

    match = _SPEED_RE.fullmatch(upper)
    if match:
        factor = _finite_number(match.group(1), "speed factor")
        if factor < 10 or factor > 500:
            return TouchscreenDecision("reject", "Speed factor is outside the stock 10-500% range.")
        return TouchscreenDecision("forward", "Stock speed factor preserved under hard motion ceilings.", deepcopy(dict(request)))

    match = _FLOW_RE.fullmatch(upper)
    if match:
        factor = _finite_number(match.group(1), "flow factor")
        if factor < 80 or factor > 120:
            return TouchscreenDecision("reject", "Flow factor is outside the stock 80-120% range.")
        return TouchscreenDecision("forward", "Stock flow factor preserved.", deepcopy(dict(request)))

    match = _OFFSET_RE.fullmatch(upper)
    if match:
        adjustment = _finite_number(match.group(1), "live Z adjustment")
        if abs(adjustment) > 0.05:
            return TouchscreenDecision(
                "reject",
                "Live Z changes are limited to 0.05 mm per press; select the 0.05 mm step.",
            )
        return TouchscreenDecision("forward", "Bounded live Z action preserved.", deepcopy(dict(request)))

    match = _SENSOR_RE.fullmatch(upper)
    if match:
        reason = (
            "The legacy bridge may not disable runout protection."
            if match.group(1) == "0"
            else "Runout protection is already forced on by the candidate."
        )
        return TouchscreenDecision(
            "emulate_success",
            reason,
        )

    return TouchscreenDecision(
        "reject", "The touchscreen requested an action outside its reviewed stock contract."
    )


def _merge_fields(existing: Any, incoming: Any) -> Any:
    if existing is None or incoming is None:
        return None
    if isinstance(existing, list) and isinstance(incoming, list):
        return list(dict.fromkeys([*existing, *incoming]))
    return incoming


def _review_objects(request: Mapping[str, Any]) -> TouchscreenDecision:
    params = request.get("params")
    objects = params.get("objects") if isinstance(params, Mapping) else None
    if not isinstance(params, Mapping) or set(params) != {"objects"} or not isinstance(objects, Mapping):
        return TouchscreenDecision("reject", "Touchscreen object request is malformed.")
    if not objects or len(objects) > 32:
        return TouchscreenDecision("reject", "Touchscreen object request is outside the reviewed size.")
    rewritten_objects: dict[str, Any] = {}
    for raw_name, fields in objects.items():
        if not isinstance(raw_name, str):
            return TouchscreenDecision("reject", "Touchscreen object name is malformed.")
        if raw_name in SYNTHETIC_OBJECTS:
            continue
        name = OBJECT_ALIASES.get(raw_name, raw_name)
        if name not in ALLOWED_OBJECTS:
            return TouchscreenDecision("reject", "Touchscreen requested an unreviewed Klipper object.")
        if fields is not None and (
            not isinstance(fields, list)
            or len(fields) > 64
            or any(not isinstance(field, str) or not field or len(field) > 128 for field in fields)
        ):
            return TouchscreenDecision("reject", "Touchscreen object fields are malformed.")
        if name in rewritten_objects:
            rewritten_objects[name] = _merge_fields(rewritten_objects[name], fields)
        else:
            rewritten_objects[name] = deepcopy(fields)
    rewritten = deepcopy(dict(request))
    rewritten["params"] = {"objects": rewritten_objects}
    return TouchscreenDecision(
        "forward", "Legacy Klipper object names translated.", rewritten
    )


def review_request(
    request: Mapping[str, Any], *, allow_explicit_restart: bool = False
) -> TouchscreenDecision:
    """Review one JSON-RPC request emitted by the proprietary bridge."""
    if not isinstance(request, Mapping):
        raise TouchscreenPolicyError("JSON-RPC request must be an object")
    envelope = _review_envelope(request)
    if envelope is not None:
        return envelope
    method = request["method"]
    if method == "printer.gcode.script":
        return _review_gcode(request, allow_explicit_restart)
    if method in {"printer.objects.query", "printer.objects.subscribe"}:
        return _review_objects(request)
    if method in NO_PARAMETER_METHODS:
        return _review_no_parameter_api(request)
    if method in {"printer.print.start", "server.files.metadata", "server.files.list"}:
        return _review_file_api(request)
    if method == "server.history.list":
        return _review_history(request)
    if method.startswith(BLOCKED_METHOD_PREFIXES):
        return TouchscreenDecision(
            "reject", "System changes are available only through the reviewed laptop deployment workflow."
        )
    return TouchscreenDecision(
        "reject", "The touchscreen requested an API outside its reviewed stock contract."
    )


def _legacy_status(status: Mapping[str, Any]) -> dict[str, Any]:
    translated = deepcopy(dict(status))
    for legacy_name, upstream_name in OBJECT_ALIASES.items():
        if upstream_name in translated:
            translated[legacy_name] = deepcopy(translated[upstream_name])
    for name, value in SYNTHETIC_OBJECTS.items():
        translated.setdefault(name, deepcopy(value))

    toolhead = translated.get("toolhead")
    if isinstance(toolhead, dict) and "max_accel_to_decel" not in toolhead:
        accel = toolhead.get("max_accel")
        ratio = toolhead.get("minimum_cruise_ratio")
        if isinstance(accel, (int, float)) and isinstance(ratio, (int, float)):
            toolhead["max_accel_to_decel"] = max(0.0, float(accel) * (1.0 - float(ratio)))

    configfile = translated.get("configfile")
    settings = configfile.get("settings") if isinstance(configfile, dict) else None
    if isinstance(settings, dict):
        for section in tuple(settings):
            if (
                isinstance(section, str)
                and section.lower().startswith("gcode_macro ")
                and section.lower() not in TOUCHSCREEN_VISIBLE_MACRO_SECTIONS
            ):
                del settings[section]
    printer = settings.get("printer") if isinstance(settings, dict) else None
    if isinstance(printer, dict) and "max_accel_to_decel" not in printer:
        accel = printer.get("max_accel")
        ratio = printer.get("minimum_cruise_ratio")
        if isinstance(accel, (int, float)) and isinstance(ratio, (int, float)):
            printer["max_accel_to_decel"] = max(0.0, float(accel) * (1.0 - float(ratio)))
    return translated


def translate_response(message: Mapping[str, Any]) -> dict[str, Any]:
    """Translate query results and asynchronous status notifications."""
    translated = deepcopy(dict(message))
    result = translated.get("result")
    if isinstance(result, dict) and isinstance(result.get("status"), Mapping):
        result["status"] = _legacy_status(result["status"])
    if translated.get("method") == "notify_status_update":
        params = translated.get("params")
        if isinstance(params, list) and params and isinstance(params[0], Mapping):
            params[0] = _legacy_status(params[0])
    return translated


def success_response(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a JSON-RPC success reply for an intentionally local no-op."""
    if "id" not in request:
        return None
    return {"jsonrpc": "2.0", "id": request["id"], "result": "ok"}


def error_response(request: Mapping[str, Any], reason: str) -> dict[str, Any] | None:
    """Build a stable JSON-RPC refusal without forwarding it to Klipper."""
    if "id" not in request:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": -32001, "message": reason},
    }

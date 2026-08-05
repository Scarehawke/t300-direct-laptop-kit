"""T300 production safety boundary for upstream Klipper.

This extra does not implement motion or heater control.  It constrains access
to upstream Klipper commands and verifies that virtual-SD files were admitted
by the host-side policy scanner.

SPDX-License-Identifier: MIT
"""

import ast
import hashlib
import io
import json
import math
import os
import stat


POLICY_VERSION = 1
APPROVAL_VERSION = 2
SHA256_LENGTH = 64
STOCK_TMC_STEPPERS = {"stepper_x", "stepper_y", "stepper_z", "extruder"}


class ApprovedGCodeFile(object):
    """Expose the original display name while reading a protected snapshot."""

    def __init__(self, handle, display_name):
        self._handle = handle
        self.name = display_name

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def close(self):
        return self._handle.close()


class T300Safety(object):
    cmd_T_CONFIRM_STEEL_SHEET_help = (
        "Mark the removable build plate clean, fitted, clipped, and clear"
    )
    cmd_T_RESERVE_PRINT_HOME_help = (
        "Consume build-plate readiness for one admitted print startup home"
    )
    cmd_T_MARK_BUILD_PLATE_DIRTY_help = (
        "Require a fresh build-plate check before the next print or Z home"
    )

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.reactor = self.printer.get_reactor()
        self.commissioning_lock = config.getboolean("commissioning_lock", True)
        self.policy_path = os.path.realpath(os.path.expanduser(
            config.get("policy_path")
        ))
        self.approval_dir = os.path.realpath(os.path.expanduser(
            config.get("approval_dir")
        ))
        self.spool_dir = os.path.realpath(os.path.expanduser(
            config.get("spool_dir")
        ))
        self.policy, self.policy_hash = self._load_policy(self.policy_path)
        # This is deliberately volatile. A Klipper restart always requires a
        # fresh human check, and no macro may persist or restore the value.
        self._plate_ready = False
        self._plate_state_sequence = 0
        self._print_home_file = None
        self._guards_installed = False

        self.virtual_sd = self.printer.lookup_object("virtual_sdcard", None)
        if self.virtual_sd is None:
            raise config.error("t300_safety requires [virtual_sdcard]")
        self.sd_root = os.path.realpath(self.virtual_sd.sdcard_dirname)

        self.gcode.register_command(
            "T_CONFIRM_STEEL_SHEET",
            self.cmd_T_CONFIRM_STEEL_SHEET,
            desc=self.cmd_T_CONFIRM_STEEL_SHEET_help,
        )
        self.gcode.register_command(
            "T_RESERVE_PRINT_HOME",
            self.cmd_T_RESERVE_PRINT_HOME,
            desc=self.cmd_T_RESERVE_PRINT_HOME_help,
        )
        self.gcode.register_command(
            "T_MARK_BUILD_PLATE_DIRTY",
            self.cmd_T_MARK_BUILD_PLATE_DIRTY,
            desc=self.cmd_T_MARK_BUILD_PLATE_DIRTY_help,
        )
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler(
            "virtual_sdcard:reset_file", self._handle_virtual_sd_reset
        )
        self.printer.register_event_handler(
            "gcode:command_error", self._handle_command_error
        )
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

    def _install_command_guards(self):
        if self._guards_installed:
            return
        # Toolhead and its default modules register several core commands only
        # after all config sections have loaded.  Install wrappers at connect,
        # when upstream Klipper has finished constructing those modules.
        # These interfaces bypass normal production abstractions. Replacing
        # them here avoids racing Klipper modules that register commands while
        # the configuration is still being constructed. Registering a rejector
        # for an absent optional command also keeps it unavailable explicitly.
        for command in (
            "FORCE_MOVE",
            "SET_KINEMATIC_POSITION",
            "SET_TMC_FIELD",
            "SET_PIN",
            "SET_HEATER_TEMPERATURE",
            "RUN_SHELL_COMMAND",
            "MANUAL_STEPPER",
            "SET_GCODE_OFFSET",
            "Z_OFFSET_APPLY_PROBE",
            "Z_OFFSET_APPLY_ENDSTOP",
            "STEPPER_BUZZ",
            "PID_CALIBRATE",
            "PROBE_CALIBRATE",
            "Z_ENDSTOP_CALIBRATE",
            "SHAPER_CALIBRATE",
            "AXIS_TWIST_COMPENSATION_CALIBRATE",
            "ENDSTOP_PHASE_CALIBRATE",
            "SET_PRESSURE_ADVANCE",
            "SET_INPUT_SHAPER",
            "SET_EXTRUDER_ROTATION_DISTANCE",
            "SYNC_EXTRUDER_MOTION",
            "INIT_TMC",
            "SET_IDLE_TIMEOUT",
            "SET_STEPPER_ENABLE",
            "SET_FILAMENT_SENSOR",
            "BED_MESH_OFFSET",
            "BED_MESH_PROFILE",
            "TUNING_TOWER",
            "SAVE_CONFIG",
            "M18",
            "M84",
            "MANUAL_PROBE",
            "PROBE",
            "PROBE_ACCURACY",
            "MEASURE_AXES_NOISE",
            "TEST_RESONANCES",
            "ACCELEROMETER_MEASURE",
            "ACCELEROMETER_DEBUG_READ",
            "ACCELEROMETER_DEBUG_WRITE",
            "SDCARD_RESET_FILE",
            "M20",
            "M21",
            "M23",
            "M24",
            "M25",
            "M26",
            "M27",
            "M28",
            "M29",
            "M30",
        ):
            self._forbid(command)
        self._wrap_required("G28", self.cmd_G28)
        self._wrap_required("G0", self.cmd_G0)
        self._wrap_required("G1", self.cmd_G1)
        self._wrap_required("G2", self.cmd_G2)
        self._wrap_required("G3", self.cmd_G3)
        self._wrap_required("G92", self.cmd_G92)
        self._wrap_required("M104", self.cmd_M104)
        self._wrap_required("M109", self.cmd_M109)
        self._wrap_required(
            "SET_VELOCITY_LIMIT", self.cmd_SET_VELOCITY_LIMIT
        )
        self._wrap_required("M204", self.cmd_M204)
        self._wrap_required("M220", self.cmd_M220)
        self._wrap_required("M221", self.cmd_M221)
        self._wrap_required("SET_TMC_CURRENT", self.cmd_SET_TMC_CURRENT)
        self._wrap_required(
            "SET_GCODE_VARIABLE", self.cmd_SET_GCODE_VARIABLE
        )
        self._wrap_required("SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE)
        self._wrap_required("LINE_PURGE", self.cmd_LINE_PURGE)
        self._wrap_required("CANCEL_PRINT", self.cmd_CANCEL_PRINT)
        loader = getattr(self.virtual_sd, "_load_file", None)
        if loader is None or not callable(loader):
            raise self.printer.config_error(
                "t300_safety requires the pinned virtual-SD loader interface"
            )
        self._original_virtual_sd_load_file = loader
        self.virtual_sd._load_file = self._load_approved_file
        if self.commissioning_lock:
            for command in (
                "G0",
                "G1",
                "G2",
                "G3",
                "M104",
                "M109",
                "M140",
                "M190",
                "M24",
                "BED_MESH_CALIBRATE",
            ):
                self._lock_command(command)
        self._guards_installed = True

    def _wrap_required(self, command, handler):
        original = self.gcode.register_command(command, None)
        if original is None:
            raise self.printer.config_error(
                "t300_safety could not find required command %s" % (command,)
            )
        setattr(self, "_original_" + command, original)
        self.gcode.register_command(command, handler)

    def _forbid(self, command):
        self.gcode.register_command(command, None)

        def reject(gcmd, name=command):
            raise gcmd.error(
                "%s is disabled by the T300 production safety policy" % (name,)
            )

        self.gcode.register_command(command, reject)

    def _lock_command(self, command):
        original = self.gcode.register_command(command, None)
        if original is None:
            raise self.printer.config_error(
                "commissioning lock could not find required command %s" % (command,)
            )
        setattr(self, "_commissioning_original_" + command, original)

        def reject(gcmd, name=command):
            raise gcmd.error(
                "%s is disabled while the T300 commissioning lock is active"
                % (name,)
            )

        self.gcode.register_command(command, reject)

    def _verify_read_only_file(self, path, description):
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise self.printer.config_error(
                "%s is unavailable: %s" % (description, exc)
            )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise self.printer.config_error(
                "%s must be one regular, non-symlink file" % (description,)
            )
        if info.st_nlink != 1:
            raise self.printer.config_error(
                "%s must have exactly one filesystem link" % (description,)
            )
        debug_input = bool(self.printer.get_start_args().get("debuginput"))
        if not debug_input and info.st_uid != 0:
            raise self.printer.config_error(
                "%s must be owned by root" % (description,)
            )
        if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise self.printer.config_error(
                "%s may not have any write permission bits" % (description,)
            )

    def _verify_protected_directory(self, path, description):
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise self.printer.config_error(
                "%s is unavailable: %s" % (description, exc)
            )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise self.printer.config_error(
                "%s must be one real directory" % (description,)
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise self.printer.config_error(
                "%s may not be group- or world-writable" % (description,)
            )
        debug_input = bool(self.printer.get_start_args().get("debuginput"))
        if not debug_input and os.access(path, os.W_OK):
            raise self.printer.config_error(
                "Klipper must not be able to write %s" % (description,)
            )
        return info

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    return digest.hexdigest()
                digest.update(block)

    def _load_policy(self, path):
        self._verify_read_only_file(path, "T300 safety policy")
        try:
            with open(path, "r") as handle:
                policy = json.load(handle)
        except (OSError, ValueError) as exc:
            raise self.printer.config_error(
                "T300 safety policy is unreadable: %s" % (exc,)
            )
        if (
            not isinstance(policy, dict)
            or isinstance(policy.get("policy_version"), bool)
            or policy.get("policy_version") != POLICY_VERSION
        ):
            raise self.printer.config_error("unsupported T300 safety policy")
        required_numbers = (
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
        )
        for key in required_numbers:
            value = policy.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise self.printer.config_error(
                    "T300 safety policy field %s is invalid" % (key,)
                )
        for key in (
            "kamp_purge_margin",
            "kamp_purge_amount",
            "kamp_breakaway_distance",
            "nozzle_temp_max",
            "bed_temp_max",
            "min_extrude_temp_floor",
            "max_velocity",
            "max_accel",
            "max_z_velocity",
            "max_z_accel",
            "max_extrude_cross_section",
            "max_extrude_only_distance",
            "max_extrude_only_velocity",
            "max_extrude_only_accel",
            "max_instantaneous_corner_velocity",
        ):
            if policy[key] <= 0.0:
                raise self.printer.config_error(
                    "T300 safety policy field %s must be positive" % (key,)
                )
        for key in ("hotend_max_power", "bed_max_power"):
            if policy[key] <= 0.0 or policy[key] > 1.0:
                raise self.printer.config_error(
                    "T300 safety policy field %s must be in (0,1]" % (key,)
                )
        if policy["max_square_corner_velocity"] < 0.0:
            raise self.printer.config_error(
                "T300 safety policy square-corner velocity is negative"
            )
        if not 0.0 <= policy["minimum_cruise_ratio_floor"] < 1.0:
            raise self.printer.config_error(
                "T300 safety policy cruise-ratio floor is outside [0,1)"
            )
        if policy["min_extrude_temp_floor"] > policy["nozzle_temp_max"]:
            raise self.printer.config_error(
                "T300 safety policy cold-extrusion floor exceeds nozzle temperature"
            )
        currents = policy.get("tmc_current_max")
        if not isinstance(currents, dict) or set(currents) != STOCK_TMC_STEPPERS:
            raise self.printer.config_error(
                "T300 safety policy must cover exactly the stock TMC steppers"
            )
        for name, value in currents.items():
            if (not isinstance(name, str) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0.0):
                raise self.printer.config_error(
                    "T300 safety policy TMC current entry is invalid"
                )
        return policy, self._sha256(path)

    def _handle_connect(self):
        self._install_command_guards()
        self._verify_read_only_file(self.policy_path, "T300 safety policy")
        if self._sha256(self.policy_path) != self.policy_hash:
            raise self.printer.config_error(
                "T300 safety policy changed after Klipper loaded it"
            )
        approval_directory = self._verify_protected_directory(
            self.approval_dir, "the T300 approval directory"
        )
        spool_directory = self._verify_protected_directory(
            self.spool_dir, "the protected T300 G-code directory"
        )
        if approval_directory.st_uid != spool_directory.st_uid:
            raise self.printer.config_error(
                "T300 approval and protected G-code directories need one owner"
            )
        self._admission_owner_uid = approval_directory.st_uid

        toolhead = self.printer.lookup_object("toolhead")
        settings = self.printer.lookup_object("configfile").get_status(
            self.reactor.monotonic()
        )["settings"]
        checks = (
            ("max velocity", toolhead.max_velocity,
             self.policy["max_velocity"], "at_most"),
            ("max acceleration", toolhead.max_accel,
             self.policy["max_accel"], "at_most"),
            ("square-corner velocity", toolhead.square_corner_velocity,
             self.policy["max_square_corner_velocity"], "at_most"),
            ("minimum cruise ratio", toolhead.min_cruise_ratio,
             self.policy["minimum_cruise_ratio_floor"], "at_least"),
        )
        for label, actual, ceiling, direction in checks:
            if ((direction == "at_most" and actual > ceiling)
                    or (direction == "at_least" and actual < ceiling)):
                raise self.printer.config_error(
                    "configured %s violates the T300 safety policy" % (label,)
                )

        printer_settings = settings.get("printer", {})
        for key, ceiling in (
                ("max_z_velocity", self.policy["max_z_velocity"]),
                ("max_z_accel", self.policy["max_z_accel"])):
            value = printer_settings.get(key)
            if value is None or value > ceiling + 1.0e-9:
                raise self.printer.config_error(
                    "configured %s exceeds the T300 safety policy" % (key,)
                )
        for axis in ("x", "y", "z"):
            axis_policy = self.policy.get(axis)
            if not isinstance(axis_policy, dict):
                raise self.printer.config_error(
                    "T300 safety policy lacks %s-axis bounds" % (axis.upper(),)
                )
            minimum = axis_policy.get("minimum")
            maximum = axis_policy.get("maximum")
            if (isinstance(minimum, bool)
                    or isinstance(maximum, bool)
                    or not isinstance(minimum, (int, float))
                    or not isinstance(maximum, (int, float))
                    or not math.isfinite(minimum)
                    or not math.isfinite(maximum)
                    or minimum >= maximum):
                raise self.printer.config_error(
                    "T300 safety policy has invalid %s-axis bounds"
                    % (axis.upper(),)
                )
            stepper = settings.get("stepper_" + axis, {})
            configured_min = stepper.get("position_min")
            configured_max = stepper.get("position_max")
            if (configured_min is None or configured_max is None
                    or configured_min < minimum - 1.0e-9
                    or configured_max > maximum + 1.0e-9):
                raise self.printer.config_error(
                    "configured %s-axis bounds exceed the T300 safety policy"
                    % (axis.upper(),)
                )

        kamp = self.printer.lookup_object("gcode_macro _KAMP_Settings", None)
        if kamp is None:
            raise self.printer.config_error(
                "production KAMP settings macro is unavailable"
            )
        for variable, policy_key in (
                ("purge_margin", "kamp_purge_margin"),
                ("purge_amount", "kamp_purge_amount")):
            value = kamp.variables.get(variable)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or abs(value - self.policy[policy_key]) > 1.0e-9):
                raise self.printer.config_error(
                    "configured KAMP %s differs from safety policy"
                    % (variable,)
                )
        if kamp.variables.get("tip_distance") != 0:
            raise self.printer.config_error(
                "production KAMP tip_distance must remain zero"
            )

        extruder = self.printer.lookup_object("extruder")
        cross_section = extruder.max_extrude_ratio * extruder.filament_area
        if cross_section > self.policy["max_extrude_cross_section"] + 1.0e-9:
            raise self.printer.config_error(
                "configured extrusion cross-section exceeds policy"
            )
        if extruder.max_e_dist > self.policy["max_extrude_only_distance"]:
            raise self.printer.config_error(
                "configured extrusion-only distance exceeds policy"
            )
        if extruder.max_e_velocity > self.policy["max_extrude_only_velocity"]:
            raise self.printer.config_error(
                "configured extrusion-only velocity exceeds policy"
            )
        if extruder.max_e_accel > self.policy["max_extrude_only_accel"]:
            raise self.printer.config_error(
                "configured extrusion-only acceleration exceeds policy"
            )
        if (extruder.instant_corner_v
                > self.policy["max_instantaneous_corner_velocity"]):
            raise self.printer.config_error(
                "configured instantaneous extrusion velocity exceeds policy"
            )
        if extruder.heater.max_temp > self.policy["nozzle_temp_max"]:
            raise self.printer.config_error(
                "configured hotend temperature exceeds policy"
            )
        if extruder.heater.max_power > self.policy["hotend_max_power"]:
            raise self.printer.config_error(
                "configured hotend power exceeds policy"
            )
        if (extruder.heater.min_extrude_temp
                < self.policy["min_extrude_temp_floor"]):
            raise self.printer.config_error(
                "configured cold-extrusion floor is below policy"
            )
        bed = self.printer.lookup_object("heater_bed")
        if bed.heater.max_temp > self.policy["bed_temp_max"]:
            raise self.printer.config_error(
                "configured bed temperature exceeds policy"
            )
        if bed.heater.max_power > self.policy["bed_max_power"]:
            raise self.printer.config_error(
                "configured bed power exceeds policy"
            )

        current_limits = self.policy["tmc_current_max"]
        for object_name, _driver in self.printer.lookup_objects("tmc2209"):
            stepper = object_name.split(" ", 1)[1]
            ceiling = current_limits.get(stepper)
            if ceiling is None:
                raise self.printer.config_error(
                    "no production current ceiling for %s" % (stepper,)
                )
            driver_settings = settings.get(object_name, {})
            configured = driver_settings.get("run_current")
            if configured is None or configured > ceiling + 1.0e-9:
                raise self.printer.config_error(
                    "configured run_current for %s exceeds policy"
                    % (stepper,)
                )

    def _has_loaded_print(self):
        return self.virtual_sd.current_file is not None

    def _clear_print_home_reservation(self):
        self._print_home_file = None

    def _mark_build_plate_dirty(self):
        if self._plate_ready:
            self._plate_state_sequence += 1
        self._plate_ready = False

    def _handle_virtual_sd_reset(self):
        # SDCARD_PRINT_FILE resets the previous file immediately before it
        # loads the new one. Preserve the human plate check across that benign
        # transition, but never preserve an old file-bound home reservation.
        self._clear_print_home_reservation()

    def _handle_command_error(self):
        # If any command in a loaded print fails, the next attempt needs a new
        # plate inspection even when the failure happened before extrusion.
        if self._has_loaded_print():
            self._mark_build_plate_dirty()
        self._clear_print_home_reservation()

    def _handle_shutdown(self):
        self._mark_build_plate_dirty()
        self._clear_print_home_reservation()

    def get_status(self, eventtime):
        del eventtime
        return {
            "build_plate_ready": self._plate_ready,
            "build_plate_status": (
                "ready" if self._plate_ready else "check_required"
            ),
            "build_plate_state_sequence": self._plate_state_sequence,
            "print_home_reserved": self._print_home_file is not None,
            # Compatibility alias for candidate macros created before the
            # human-facing state was renamed.
            "steel_sheet_armed": self._plate_ready,
            "policy_sha256": self.policy_hash,
            "commissioning_lock": self.commissioning_lock,
        }

    def cmd_T_CONFIRM_STEEL_SHEET(self, gcmd):
        if self.commissioning_lock:
            raise gcmd.error(
                "steel-sheet arming is disabled by the commissioning lock"
            )
        confirmation = gcmd.get("CONFIRM", "").strip().upper()
        if confirmation != "YES":
            raise gcmd.error(
                "Fit the magnetic steel sheet, then use "
                "T_CONFIRM_STEEL_SHEET CONFIRM=YES"
            )
        if self._has_loaded_print():
            raise gcmd.error(
                "the build plate cannot be marked ready while a print is loaded"
            )
        extruder = self.printer.lookup_object("extruder")
        heater = extruder.heater
        hotend_temp, _target = heater.get_temp(self.reactor.monotonic())
        if (hotend_temp >= heater.min_extrude_temp
                or heater.target_temp > 0.0):
            raise gcmd.error(
                "cool the hotend and set its target to zero before marking the "
                "build plate ready"
            )
        self._clear_print_home_reservation()
        if not self._plate_ready:
            self._plate_state_sequence += 1
        self._plate_ready = True
        gcmd.respond_info(
            "Build plate marked clean, fitted, clipped, and clear. It remains "
            "ready until a print, purge, hot filament action, cancellation, "
            "error, or Klipper restart."
        )

    def cmd_T_MARK_BUILD_PLATE_DIRTY(self, gcmd):
        self._mark_build_plate_dirty()
        gcmd.respond_info("Build plate check required before the next print or Z home.")

    def cmd_T_RESERVE_PRINT_HOME(self, gcmd):
        if self.commissioning_lock:
            raise gcmd.error(
                "print homing is disabled by the T300 commissioning lock"
            )
        current = self.virtual_sd.current_file
        if (
            current is None
            or not isinstance(current, ApprovedGCodeFile)
            or not self.virtual_sd.is_active()
            or not self.virtual_sd.is_cmd_from_sd()
        ):
            raise gcmd.error(
                "print homing may be reserved only by an active admitted file"
            )
        if not self._plate_ready:
            raise gcmd.error(
                "build plate check required; use Build Plate Ready before printing"
            )
        # Consume readiness before any heater or movement command. The exact
        # immutable open file object is the only authority accepted by G28.
        self._plate_ready = False
        self._plate_state_sequence += 1
        self._print_home_file = current

    def cmd_G28(self, gcmd):
        if self.commissioning_lock:
            raise gcmd.error("G28 is disabled by the T300 commissioning lock")
        params = gcmd.get_command_parameters()
        current = self.virtual_sd.current_file
        if current is not None:
            valid_print_home = (
                not params
                and current is self._print_home_file
                and self.virtual_sd.is_active()
                and self.virtual_sd.is_cmd_from_sd()
            )
            self._clear_print_home_reservation()
            if not valid_print_home:
                raise gcmd.error(
                    "homing is disabled while a print is loaded or paused"
                )
            # Consume the file-bound reservation before upstream homing. A
            # failed probe or home cannot reuse it.
            return self._original_G28(gcmd)
        homes_z = not params or "Z" in params
        if homes_z and not self._plate_ready:
            raise gcmd.error(
                "build plate check required before Z homing; use Home Printer"
            )
        return self._original_G28(gcmd)

    def _mark_if_extrusion_move(self, gcmd):
        if "E" in gcmd.get_command_parameters():
            self._mark_build_plate_dirty()

    def cmd_G0(self, gcmd):
        self._mark_if_extrusion_move(gcmd)
        return self._original_G0(gcmd)

    def cmd_G1(self, gcmd):
        self._mark_if_extrusion_move(gcmd)
        return self._original_G1(gcmd)

    def cmd_G2(self, gcmd):
        self._mark_if_extrusion_move(gcmd)
        return self._original_G2(gcmd)

    def cmd_G3(self, gcmd):
        self._mark_if_extrusion_move(gcmd)
        return self._original_G3(gcmd)

    def _mark_if_hotend_target(self, gcmd):
        values = [self._finite_parameter(gcmd, name) for name in ("S", "R")]
        if any(
            value is not None
            and value >= self.policy["min_extrude_temp_floor"]
            for value in values
        ):
            self._mark_build_plate_dirty()

    def cmd_M104(self, gcmd):
        self._mark_if_hotend_target(gcmd)
        return self._original_M104(gcmd)

    def cmd_M109(self, gcmd):
        self._mark_if_hotend_target(gcmd)
        return self._original_M109(gcmd)

    def cmd_LINE_PURGE(self, gcmd):
        self._mark_build_plate_dirty()
        return self._original_LINE_PURGE(gcmd)

    def cmd_CANCEL_PRINT(self, gcmd):
        self._mark_build_plate_dirty()
        self._clear_print_home_reservation()
        return self._original_CANCEL_PRINT(gcmd)

    def cmd_G92(self, gcmd):
        params = gcmd.get_command_parameters()
        if set(params) != {"E"}:
            raise gcmd.error(
                "T300 production permits G92 only for the slicer's E-axis reset"
            )
        self._finite_parameter(gcmd, "E")
        return self._original_G92(gcmd)

    def cmd_SET_GCODE_VARIABLE(self, gcmd):
        macro = gcmd.get("MACRO").strip().upper()
        variable = gcmd.get("VARIABLE").strip().lower()
        try:
            value = ast.literal_eval(gcmd.get("VALUE"))
        except (SyntaxError, TypeError, ValueError) as exc:
            raise gcmd.error("SET_GCODE_VARIABLE value is invalid: %s" % (exc,))

        if macro == "RESUME":
            if variable == "idle_state":
                valid = isinstance(value, bool)
            elif variable == "last_extruder_temp":
                valid = (
                    isinstance(value, dict)
                    and set(value) == {"restore", "temp"}
                    and isinstance(value.get("restore"), bool)
                    and not isinstance(value.get("temp"), bool)
                    and isinstance(value.get("temp"), (int, float))
                    and math.isfinite(value["temp"])
                    and 0.0 <= value["temp"] <= self.policy["nozzle_temp_max"]
                )
            elif variable == "restore_idle_timeout":
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and 0.0 <= value <= 600.0
                )
            else:
                valid = False
        elif macro == "SET_PRINT_STATS_INFO":
            if variable == "pause_next_layer":
                valid = (
                    isinstance(value, dict)
                    and set(value) == {"enable", "call"}
                    and isinstance(value.get("enable"), bool)
                    and isinstance(value.get("call"), str)
                    and value["call"].upper() in {"PAUSE", "M600"}
                )
            elif variable == "pause_at_layer":
                layer = value.get("layer") if isinstance(value, dict) else None
                valid = (
                    isinstance(value, dict)
                    and set(value) == {"enable", "layer", "call"}
                    and isinstance(value.get("enable"), bool)
                    and isinstance(layer, int)
                    and not isinstance(layer, bool)
                    and 0 <= layer <= 10000000
                    and isinstance(value.get("call"), str)
                    and value["call"].upper() in {"PAUSE", "M600"}
                )
            else:
                valid = False
        else:
            valid = False
        if not valid:
            raise gcmd.error(
                "production permits only bounded Mainsail pause-state variables"
            )
        return self._original_SET_GCODE_VARIABLE(gcmd)

    @staticmethod
    def _finite_parameter(gcmd, name):
        value = gcmd.get_float(name, None)
        if value is not None and not math.isfinite(value):
            raise gcmd.error("%s must be finite" % (name,))
        return value

    def cmd_SET_VELOCITY_LIMIT(self, gcmd):
        ceilings = (
            ("VELOCITY", self.policy["max_velocity"]),
            ("ACCEL", self.policy["max_accel"]),
            ("SQUARE_CORNER_VELOCITY",
             self.policy["max_square_corner_velocity"]),
        )
        for name, ceiling in ceilings:
            value = self._finite_parameter(gcmd, name)
            if value is not None and value > ceiling:
                raise gcmd.error(
                    "%s exceeds the T300 production ceiling %.6g"
                    % (name, ceiling)
                )
        ratio = self._finite_parameter(gcmd, "MINIMUM_CRUISE_RATIO")
        if (ratio is not None
                and ratio < self.policy["minimum_cruise_ratio_floor"]):
            raise gcmd.error(
                "MINIMUM_CRUISE_RATIO is below the T300 production floor"
            )
        accel_to_decel = self._finite_parameter(gcmd, "ACCEL_TO_DECEL")
        if accel_to_decel is not None:
            if accel_to_decel <= 0.0:
                raise gcmd.error("ACCEL_TO_DECEL must be greater than zero")
            accel = self._finite_parameter(gcmd, "ACCEL")
            if accel is None:
                accel = self.printer.lookup_object("toolhead").max_accel
            if accel <= 0.0:
                raise gcmd.error("ACCEL must be greater than zero")
            implied_ratio = 1.0 - min(1.0, accel_to_decel / accel)
            if implied_ratio < self.policy["minimum_cruise_ratio_floor"]:
                raise gcmd.error(
                    "ACCEL_TO_DECEL is below the T300 production cruise floor"
                )
        return self._original_SET_VELOCITY_LIMIT(gcmd)

    def cmd_M204(self, gcmd):
        values = [self._finite_parameter(gcmd, name) for name in ("S", "P", "T")]
        effective = values[0]
        if effective is None and values[1] is not None and values[2] is not None:
            effective = min(values[1], values[2])
        if effective is not None and effective > self.policy["max_accel"]:
            raise gcmd.error("M204 exceeds the T300 production acceleration ceiling")
        return self._original_M204(gcmd)

    def cmd_M220(self, gcmd):
        percentage = self._finite_parameter(gcmd, "S")
        if percentage is not None and percentage > 100.0:
            raise gcmd.error("M220 may reduce, but may not raise, print speed")
        return self._original_M220(gcmd)

    def cmd_M221(self, gcmd):
        percentage = self._finite_parameter(gcmd, "S")
        if percentage is not None and percentage > 100.0:
            raise gcmd.error("M221 may reduce, but may not raise, extrusion flow")
        return self._original_M221(gcmd)

    def cmd_SET_TMC_CURRENT(self, gcmd):
        stepper = gcmd.get("STEPPER", "")
        ceiling = self.policy["tmc_current_max"].get(stepper)
        if ceiling is None:
            raise gcmd.error("no production current policy exists for %s" % (stepper,))
        for name in ("CURRENT", "HOLDCURRENT"):
            value = self._finite_parameter(gcmd, name)
            if value is not None and value > ceiling:
                raise gcmd.error(
                    "%s for %s exceeds the production ceiling %.6g A"
                    % (name, stepper, ceiling)
                )
        return self._original_SET_TMC_CURRENT(gcmd)

    @staticmethod
    def _sha256_fd(descriptor):
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest()

    @staticmethod
    def _approval_id(relative, digest):
        value = relative.encode("utf-8") + b"\0" + digest.encode("ascii")
        return hashlib.sha256(value).hexdigest()

    def _open_regular_nofollow(self, path, gcmd, description):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            current = os.lstat(path)
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise gcmd.error("%s is unavailable: %s" % (description, exc))
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            os.close(descriptor)
            raise gcmd.error("%s is not one stable regular file" % (description,))
        return descriptor, opened

    def _source_identity(self, filename, gcmd):
        filename = filename.lstrip("/")
        normalized = os.path.normpath(filename)
        if (
            not filename
            or "\x00" in filename
            or normalized != filename
            or normalized in (".", "..")
            or normalized.startswith(".." + os.sep)
        ):
            raise gcmd.error("invalid virtual-SD filename")
        candidate = os.path.join(self.sd_root, normalized)
        source = os.path.realpath(candidate)
        if not source.startswith(self.sd_root + os.sep):
            raise gcmd.error("virtual-SD file escapes the approved G-code root")
        descriptor, before = self._open_regular_nofollow(
            source, gcmd, "uploaded G-code"
        )
        try:
            try:
                digest = self._sha256_fd(descriptor)
                after = os.fstat(descriptor)
                current = os.lstat(source)
            except OSError as exc:
                raise gcmd.error(
                    "uploaded G-code changed while it was identified: %s" % (exc,)
                )
        finally:
            os.close(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or current.st_size != before.st_size
            or current.st_mtime_ns != before.st_mtime_ns
        ):
            raise gcmd.error("uploaded G-code changed while it was identified")
        relative = os.path.relpath(source, self.sd_root).replace(os.sep, "/")
        return source, relative, digest, before.st_size

    def _read_approval(self, relative, digest, source_size, gcmd):
        key = self._approval_id(relative, digest)
        path = os.path.join(self.approval_dir, key + ".json")
        descriptor, info = self._open_regular_nofollow(
            path, gcmd, "G-code approval"
        )
        try:
            if (
                info.st_uid != self._admission_owner_uid
                or info.st_nlink != 1
                or info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                or info.st_size > 65536
            ):
                raise gcmd.error("G-code approval permissions are unsafe")
            with io.open(descriptor, "r", encoding="utf-8", closefd=False) as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            raise gcmd.error("G-code has no valid policy approval: %s" % (exc,))
        finally:
            os.close(descriptor)
        if not isinstance(record, dict):
            raise gcmd.error("G-code approval is not one JSON object")
        expected = {
            "schema_version": APPROVAL_VERSION,
            "relative_path": relative,
            "sha256": digest,
            "size": source_size,
            "policy_sha256": self.policy_hash,
            "spool_file": digest + ".gcode",
        }
        for key, value in expected.items():
            if record.get(key) != value:
                raise gcmd.error("G-code approval does not match %s" % (key,))
        return record

    def _open_approved_snapshot(self, filename, gcmd):
        source, relative, digest, source_size = self._source_identity(filename, gcmd)
        record = self._read_approval(relative, digest, source_size, gcmd)
        protected = os.path.join(self.spool_dir, record["spool_file"])
        descriptor, before = self._open_regular_nofollow(
            protected, gcmd, "protected G-code snapshot"
        )
        try:
            if (
                before.st_uid != self._admission_owner_uid
                or before.st_nlink != 1
                or before.st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                or before.st_size != source_size
                or self._sha256_fd(descriptor) != digest
            ):
                raise gcmd.error("protected G-code snapshot failed verification")
            after = os.fstat(descriptor)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise gcmd.error("protected G-code changed during verification")
            handle = io.open(
                descriptor, "r", encoding="utf-8", newline="", closefd=True
            )
            descriptor = None
            return ApprovedGCodeFile(handle, source), relative, source_size
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load_approved_file(self, gcmd, filename, check_subdirs=False):
        del check_subdirs
        handle, relative, file_size = self._open_approved_snapshot(filename, gcmd)
        try:
            gcmd.respond_raw("File opened:%s Size:%d" % (relative, file_size))
            gcmd.respond_raw("File selected")
            self.virtual_sd.current_file = handle
            self.virtual_sd.file_position = 0
            self.virtual_sd.file_size = file_size
            self.virtual_sd.print_stats.set_current_file(relative)
        except Exception:
            handle.close()
            self.virtual_sd.current_file = None
            self.virtual_sd.file_position = self.virtual_sd.file_size = 0
            raise

    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.commissioning_lock:
            raise gcmd.error("printing is disabled by the T300 commissioning lock")
        if not self._plate_ready:
            raise gcmd.error(
                "build plate check required; use Build Plate Ready before selecting a file"
            )
        return self._original_SDCARD_PRINT_FILE(gcmd)


def load_config(config):
    return T300Safety(config)

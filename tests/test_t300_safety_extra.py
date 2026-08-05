from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "t300_safety_under_test",
    ROOT / "mainline/klippy/extras/t300_safety.py",
)
assert SPEC is not None and SPEC.loader is not None
SAFETY_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SAFETY_MODULE)


class FakeGcmd:
    def __init__(self, params=None):
        self.params = dict(params or {})
        self.responses = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_float(self, name, default=None):
        value = self.params.get(name, default)
        return default if value is None else float(value)

    def get_command_parameters(self):
        return dict(self.params)

    def error(self, message):
        return RuntimeError(message)

    def respond_info(self, message):
        self.responses.append(message)


class FakeVirtualSD:
    def __init__(self):
        self.current_file = None
        self.active = False
        self.from_sd = False

    def is_active(self):
        return self.active

    def is_cmd_from_sd(self):
        return self.from_sd


def safety_fixture():
    virtual_sd = FakeVirtualSD()
    heater = SimpleNamespace(
        can_extrude=False,
        current_temp=20.0,
        min_extrude_temp=150.0,
        target_temp=0.0,
    )
    heater.get_temp = lambda _eventtime: (
        heater.current_temp,
        heater.target_temp,
    )
    extruder = SimpleNamespace(
        heater=heater,
    )
    printer = SimpleNamespace(
        lookup_object=lambda name: extruder if name == "extruder" else None
    )
    safety = SAFETY_MODULE.T300Safety.__new__(SAFETY_MODULE.T300Safety)
    safety.virtual_sd = virtual_sd
    safety.printer = printer
    safety.reactor = SimpleNamespace(monotonic=lambda: 0.0)
    safety.commissioning_lock = False
    safety._plate_ready = False
    safety._plate_state_sequence = 0
    safety._print_home_file = None
    safety.policy = {"min_extrude_temp_floor": 150.0}
    return safety, virtual_sd, extruder


class T300SafetyStateTests(unittest.TestCase):
    def test_plate_check_persists_across_repeated_idle_homing(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        confirm = FakeGcmd({"CONFIRM": "YES"})
        safety.cmd_T_CONFIRM_STEEL_SHEET(confirm)
        self.assertTrue(safety._plate_ready)

        calls = []
        safety._original_G28 = lambda gcmd: calls.append(gcmd)
        safety.cmd_G28(FakeGcmd())
        safety.cmd_G28(FakeGcmd())
        self.assertEqual(len(calls), 2)
        self.assertTrue(safety._plate_ready)

    def test_plate_cannot_be_rearmed_while_a_print_is_paused(self):
        safety, virtual_sd, _extruder = safety_fixture()
        virtual_sd.current_file = object()
        virtual_sd.active = False
        with self.assertRaisesRegex(RuntimeError, "print is loaded"):
            safety.cmd_T_CONFIRM_STEEL_SHEET(FakeGcmd({"CONFIRM": "YES"}))

    def test_loaded_or_paused_print_cannot_home_without_reservation(self):
        safety, virtual_sd, _extruder = safety_fixture()
        virtual_sd.current_file = object()
        virtual_sd.active = False
        safety._plate_ready = True
        safety._original_G28 = lambda _gcmd: self.fail("upstream G28 was called")
        with self.assertRaisesRegex(RuntimeError, "loaded or paused"):
            safety.cmd_G28(FakeGcmd())

    def test_active_admitted_file_gets_one_full_home(self):
        safety, virtual_sd, _extruder = safety_fixture()
        approved = SAFETY_MODULE.ApprovedGCodeFile(io.StringIO(""), "print.gcode")
        virtual_sd.current_file = approved
        virtual_sd.active = True
        virtual_sd.from_sd = True
        safety._plate_ready = True

        safety.cmd_T_RESERVE_PRINT_HOME(FakeGcmd())
        self.assertFalse(safety._plate_ready)
        self.assertIs(safety._print_home_file, approved)
        calls = []
        safety._original_G28 = lambda gcmd: calls.append(gcmd)
        safety.cmd_G28(FakeGcmd())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(safety._print_home_file)
        with self.assertRaisesRegex(RuntimeError, "loaded or paused"):
            safety.cmd_G28(FakeGcmd())

    def test_print_reservation_does_not_authorize_partial_home(self):
        safety, virtual_sd, _extruder = safety_fixture()
        approved = SAFETY_MODULE.ApprovedGCodeFile(io.StringIO(""), "print.gcode")
        virtual_sd.current_file = approved
        virtual_sd.active = True
        virtual_sd.from_sd = True
        safety._plate_ready = True
        safety.cmd_T_RESERVE_PRINT_HOME(FakeGcmd())
        safety._original_G28 = lambda _gcmd: self.fail("upstream G28 was called")
        with self.assertRaisesRegex(RuntimeError, "loaded or paused"):
            safety.cmd_G28(FakeGcmd({"X": ""}))
        self.assertIsNone(safety._print_home_file)

    def test_print_reservation_is_bound_to_the_exact_open_file(self):
        safety, virtual_sd, _extruder = safety_fixture()
        approved = SAFETY_MODULE.ApprovedGCodeFile(io.StringIO(""), "one.gcode")
        replacement = SAFETY_MODULE.ApprovedGCodeFile(io.StringIO(""), "two.gcode")
        virtual_sd.current_file = approved
        virtual_sd.active = True
        virtual_sd.from_sd = True
        safety._plate_ready = True
        safety.cmd_T_RESERVE_PRINT_HOME(FakeGcmd())
        virtual_sd.current_file = replacement
        safety._original_G28 = lambda _gcmd: self.fail("upstream G28 was called")
        with self.assertRaisesRegex(RuntimeError, "loaded or paused"):
            safety.cmd_G28(FakeGcmd())
        self.assertIsNone(safety._print_home_file)

    def test_idle_xy_home_does_not_need_plate_but_z_home_does(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        calls = []
        safety._original_G28 = lambda gcmd: calls.append(gcmd)
        safety.cmd_G28(FakeGcmd({"X": "", "Y": ""}))
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(RuntimeError, "build plate check required"):
            safety.cmd_G28(FakeGcmd({"Z": ""}))

    def test_extrusion_hotend_heat_and_cancel_all_invalidate_plate(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._plate_ready = True
        safety._original_G1 = lambda _gcmd: None
        safety.cmd_G1(FakeGcmd({"X": "10", "E": "0.2"}))
        self.assertFalse(safety._plate_ready)

        safety._plate_ready = True
        safety._original_M104 = lambda _gcmd: None
        safety.cmd_M104(FakeGcmd({"S": "215"}))
        self.assertFalse(safety._plate_ready)

        safety._plate_ready = True
        safety._print_home_file = object()

        def cancelled(_gcmd):
            self.assertFalse(safety._plate_ready)
            self.assertIsNone(safety._print_home_file)

        safety._original_CANCEL_PRINT = cancelled
        safety.cmd_CANCEL_PRINT(FakeGcmd())

    def test_hot_or_targeted_hotend_cannot_be_marked_ready(self):
        safety, _virtual_sd, extruder = safety_fixture()
        extruder.heater.target_temp = 215.0
        with self.assertRaisesRegex(RuntimeError, "cool the hotend"):
            safety.cmd_T_CONFIRM_STEEL_SHEET(FakeGcmd({"CONFIRM": "YES"}))
        extruder.heater.target_temp = 0.0
        extruder.heater.current_temp = 150.0
        with self.assertRaisesRegex(RuntimeError, "cool the hotend"):
            safety.cmd_T_CONFIRM_STEEL_SHEET(FakeGcmd({"CONFIRM": "YES"}))

    def test_fileoutput_extrusion_bypass_does_not_look_like_a_hot_nozzle(self):
        safety, _virtual_sd, extruder = safety_fixture()
        # Upstream Klipper deliberately forces can_extrude in file-output
        # regression mode. Readiness is based on the physical temperature and
        # target, so that test-only bypass cannot masquerade as a hot nozzle.
        extruder.heater.can_extrude = True
        safety.cmd_T_CONFIRM_STEEL_SHEET(FakeGcmd({"CONFIRM": "YES"}))
        self.assertTrue(safety._plate_ready)

    def test_cold_hotend_target_and_idle_error_preserve_ready_state(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._plate_ready = True
        safety._original_M104 = lambda _gcmd: None
        safety.cmd_M104(FakeGcmd({"S": "149"}))
        safety._handle_command_error()
        self.assertTrue(safety._plate_ready)

    def test_virtual_sd_reset_preserves_check_but_clears_old_reservation(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._plate_ready = True
        safety._print_home_file = object()
        safety._handle_virtual_sd_reset()
        self.assertTrue(safety._plate_ready)
        self.assertIsNone(safety._print_home_file)

    def test_purge_and_shutdown_invalidate_ready_state(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._plate_ready = True
        safety._original_LINE_PURGE = lambda _gcmd: None
        safety.cmd_LINE_PURGE(FakeGcmd())
        self.assertFalse(safety._plate_ready)

        safety._plate_ready = True
        safety._print_home_file = object()
        safety._handle_shutdown()
        self.assertFalse(safety._plate_ready)
        self.assertIsNone(safety._print_home_file)

    def test_print_command_requires_plate_check_before_loading(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._original_SDCARD_PRINT_FILE = lambda _gcmd: self.fail(
            "upstream print command was called"
        )
        with self.assertRaisesRegex(RuntimeError, "Build Plate Ready"):
            safety.cmd_SDCARD_PRINT_FILE(FakeGcmd())

    def test_print_selection_preserves_ready_until_start_print_reserves_home(self):
        safety, _virtual_sd, _extruder = safety_fixture()
        safety._plate_ready = True
        calls = []
        safety._original_SDCARD_PRINT_FILE = lambda gcmd: calls.append(gcmd)
        safety.cmd_SDCARD_PRINT_FILE(FakeGcmd({"FILENAME": "print.gcode"}))
        self.assertEqual(len(calls), 1)
        self.assertTrue(safety._plate_ready)

    def test_loaded_print_error_invalidates_state_and_reservation(self):
        safety, virtual_sd, _extruder = safety_fixture()
        virtual_sd.current_file = object()
        safety._plate_ready = True
        safety._print_home_file = object()
        safety._handle_command_error()
        self.assertFalse(safety._plate_ready)
        self.assertIsNone(safety._print_home_file)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "audit-t300-config.py"
SPEC = importlib.util.spec_from_file_location("audit_t300_config", MODULE_PATH)
assert SPEC and SPEC.loader
audit_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_config
SPEC.loader.exec_module(audit_config)


class ConfigTreeTests(unittest.TestCase):
    def test_follows_includes_and_records_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "printer.cfg").write_text("[include Macro.cfg]\n[printer]\n", encoding="utf-8")
            (root / "Macro.cfg").write_text(
                "[gcode_macro START_PRINT]\ngcode:\n  G28\n", encoding="utf-8"
            )
            tree = audit_config.load_tree(root)
            self.assertEqual(tree.files, [Path("printer.cfg"), Path("Macro.cfg")])
            section = audit_config.macro(tree, "START_PRINT")
            self.assertIsNotNone(section)
            self.assertEqual(section.location, "Macro.cfg:1")

    def test_expands_includes_in_textual_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "printer.cfg").write_text(
                "[idle_timeout]\ngcode:\n  RESPOND MSG=unsafe\n"
                "[include core.cfg]\n",
                encoding="utf-8",
            )
            (root / "core.cfg").write_text(
                "[idle_timeout]\ngcode:\n  TURN_OFF_HEATERS\n",
                encoding="utf-8",
            )
            tree = audit_config.load_tree(root)
            idle = audit_config.first_section(tree, "idle_timeout")
            self.assertIsNotNone(idle)
            self.assertEqual(idle.location, "core.cfg:1")
            self.assertGreaterEqual(audit_config.command_position(idle, "TURN_OFF_HEATERS"), 0)

    def test_rejects_include_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "printer.cfg").write_text("[include ../secret.cfg]\n", encoding="utf-8")
            tree = audit_config.load_tree(root)
            self.assertEqual(len(tree.missing_includes), 1)


class AuditRulesTests(unittest.TestCase):
    def audit_text(self, printer: str, macro: str = ""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            include = "[include Macro.cfg]\n" if macro else ""
            (root / "printer.cfg").write_text(include + printer, encoding="utf-8")
            if macro:
                (root / "Macro.cfg").write_text(macro, encoding="utf-8")
            return audit_config.audit(audit_config.load_tree(root))

    def codes(self, findings):
        return {finding.code for finding in findings}

    def test_detects_factory_safety_limits(self):
        findings = self.audit_text(
            """
[idle_timeout]
gcode:
  RESPOND MSG=idle
timeout: 600
[extruder]
nozzle_diameter: 0.4
max_extrude_cross_section: 500
instantaneous_corner_velocity: 10
max_extrude_only_velocity: 2000
max_extrude_only_accel: 10000
"""
        )
        self.assertTrue(
            {"SAFE001", "SAFE002", "SAFE003", "SAFE004", "SAFE005"}.issubset(
                self.codes(findings)
            )
        )

    def test_detects_factory_lifecycle_failures(self):
        findings = self.audit_text(
            "[idle_timeout]\ngcode:\n  TURN_OFF_HEATERS\n",
            """
[gcode_macro PAUSE]
gcode:
  {% set state = params.STATE %}
[gcode_macro RESUME]
gcode:
  {% if params.STATE == 'filament_change' %}
  {% endif %}
[gcode_macro CANCEL_PRINT]
gcode:
  {% if "xyz" not in printer.toolhead.homed_axe %}
  {% endif %}
""",
        )
        self.assertTrue(
            {"MACRO_PAUSE_PARAM", "MACRO_RESUME_PARAM", "MACRO_CANCEL_HOME"}.issubset(
                self.codes(findings)
            )
        )

    def test_safe_idle_and_extrusion_values_do_not_trigger(self):
        findings = self.audit_text(
            """
[idle_timeout]
gcode:
  TURN_OFF_HEATERS
timeout: 600
[extruder]
nozzle_diameter: 0.4
max_extrude_cross_section: 1.0
instantaneous_corner_velocity: 1
max_extrude_only_velocity: 60
max_extrude_only_accel: 3000
"""
        )
        unsafe = {"SAFE001", "SAFE002", "SAFE003", "SAFE004", "SAFE005"}
        self.assertTrue(unsafe.isdisjoint(self.codes(findings)))

    def test_duplicate_option_is_reported(self):
        findings = self.audit_text("[probe]\nspeed: 12\nspeed: 5\n")
        self.assertIn("CFG003", self.codes(findings))

    def test_identical_duplicate_option_is_only_informational(self):
        findings = self.audit_text(
            "[extruder]\nmax_extrude_only_distance: 100\n"
            "max_extrude_only_distance: 100\n"
        )
        duplicate = [finding for finding in findings if finding.code == "CFG005"]
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(duplicate[0].severity, "INFO")

    def test_selected_private_level_macro_is_a_managed_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "printer.cfg").write_text(
                "[include Macro.cfg]\n[include macro_z_tilt_via_knob.cfg]\n",
                encoding="utf-8",
            )
            (root / "Macro.cfg").write_text(
                "[gcode_macro Z_TILT_CALIBRATION]\ngcode:\n  G28\n",
                encoding="utf-8",
            )
            (root / "macro_z_tilt_via_knob.cfg").write_text(
                "[gcode_macro Z_TILT_CALIBRATION]\ngcode:\n  M117 reviewed\n",
                encoding="utf-8",
            )
            findings = audit_config.audit(audit_config.load_tree(root))
        ownership = [finding for finding in findings if finding.code == "CFG004"]
        self.assertEqual(len(ownership), 1)
        self.assertEqual(ownership[0].severity, "INFO")
        self.assertIn("macro_z_tilt_via_knob.cfg", ownership[0].title)

    def test_detects_macro_names_truncated_by_klipper_legacy_parser(self):
        findings = self.audit_text(
            "",
            """
[gcode_macro _T300_SAFE_EXIT]
gcode:
  M117 unsafe
[gcode_macro GANTRY_LEVEL_T300]
gcode:
  M117 valid
""",
        )
        truncated = [finding for finding in findings if finding.code == "MACRO006"]
        self.assertEqual(len(truncated), 1)
        self.assertIn("_T300_SAFE_EXIT", truncated[0].explanation)

    def test_detects_semicolon_inside_respond_message(self):
        findings = self.audit_text(
            "",
            """
[gcode_macro TEST]
gcode:
  RESPOND TYPE=echo MSG="First clause; second clause"
""",
        )
        self.assertIn("MACRO007", self.codes(findings))

    def test_vendor_adaptive_mesh_info_reads_mesh_macro_itself(self):
        findings = self.audit_text(
            "",
            """
[gcode_macro BED_MESH_CALIBRATE]
rename_existing: BED_MESH_CALIBRATE_BASE
gcode:
  BED_MESH_CALIBRATE_BASE ADAPTIVE=1
""",
        )
        self.assertIn("MESH002", self.codes(findings))


if __name__ == "__main__":
    unittest.main()

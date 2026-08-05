from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "t300ctl.py"
SPEC = importlib.util.spec_from_file_location("t300ctl", MODULE_PATH)
assert SPEC and SPEC.loader
t300ctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t300ctl)


class NormalizeHostTests(unittest.TestCase):
    def test_plain_ipv4(self):
        self.assertEqual(t300ctl.normalize_base_url("10.42.42.2"), "http://10.42.42.2")

    def test_host_and_port(self):
        self.assertEqual(
            t300ctl.normalize_base_url("http://printer.local:7125/"),
            "http://printer.local:7125",
        )

    def test_rejects_path(self):
        with self.assertRaises(t300ctl.T300Error):
            t300ctl.normalize_base_url("http://printer.local/server/info")

    def test_rejects_credentials(self):
        with self.assertRaises(t300ctl.T300Error):
            t300ctl.normalize_base_url("http://user:secret@printer.local")


class RemotePathTests(unittest.TestCase):
    def test_nested_path(self):
        self.assertEqual(
            str(t300ctl.validate_remote_path("printer_additions/module.cfg")),
            "printer_additions/module.cfg",
        )

    def test_rejects_parent_escape(self):
        with self.assertRaises(t300ctl.T300Error):
            t300ctl.validate_remote_path("../printer.cfg")

    def test_rejects_absolute_path(self):
        with self.assertRaises(t300ctl.T300Error):
            t300ctl.validate_remote_path("/etc/passwd")


class ConfigPatchTests(unittest.TestCase):
    def test_inserts_after_factory_macro_include(self):
        original = "[include mainsail.cfg]\n[include Macro.cfg]\n[mcu]\n"
        expected = (
            "[include mainsail.cfg]\n"
            "[include Macro.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            "[mcu]\n"
        )
        actual, changed = t300ctl.patch_printer_cfg(original)
        self.assertTrue(changed)
        self.assertEqual(actual, expected)

    def test_preserves_crlf(self):
        original = "[include Macro.cfg]\r\n[mcu]\r\n"
        actual, changed = t300ctl.patch_printer_cfg(original)
        self.assertTrue(changed)
        self.assertIn("[include macro_z_tilt_via_knob.cfg]\r\n", actual)

    def test_is_idempotent(self):
        original = "[include Macro.cfg]\n[include macro_z_tilt_via_knob.cfg]\n"
        actual, changed = t300ctl.patch_printer_cfg(original)
        self.assertFalse(changed)
        self.assertEqual(actual, original)

    def test_refuses_unknown_layout(self):
        with self.assertRaises(t300ctl.T300Error):
            t300ctl.patch_printer_cfg("[mcu]\n")

    def test_inserts_open_macro_include(self):
        original = "[include Macro.cfg]\n[mcu]\n"
        actual, changed = t300ctl.patch_printer_cfg(original, t300ctl.OPEN_MACRO_FILENAME)
        self.assertTrue(changed)
        self.assertIn("[include t300_gantry_level.cfg]\n", actual)

    def test_core_include_follows_all_regular_sections(self):
        original = (
            "[include Macro.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            "[mcu]\n"
        )
        actual, changed = t300ctl.patch_core_printer_cfg(original)
        self.assertTrue(changed)
        self.assertEqual(
            actual,
            "[include Macro.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            "[mcu]\n"
            "[include t300_core.cfg]\n",
        )

    def test_core_include_falls_back_to_factory_macro(self):
        original = "[include Macro.cfg]\r\n[mcu]\r\n"
        actual, changed = t300ctl.patch_core_printer_cfg(original)
        self.assertTrue(changed)
        self.assertIn("[include t300_core.cfg]\r\n", actual)

    def test_core_include_is_idempotent(self):
        original = (
            "[include Macro.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            "[mcu]\n"
            "[include t300_core.cfg]\n"
        )
        actual, changed = t300ctl.patch_core_printer_cfg(original)
        self.assertFalse(changed)
        self.assertEqual(actual, original)

    def test_core_include_moves_before_save_config_block(self):
        original = (
            "[include Macro.cfg]\n"
            "[include t300_core.cfg]\n"
            "[extruder]\n"
            "max_extrude_cross_section: 500\n"
            "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
            "#*# [probe]\n"
            "#*# z_offset = 1.970\n"
        )
        actual, changed = t300ctl.patch_core_printer_cfg(original)
        self.assertTrue(changed)
        self.assertLess(
            actual.index("max_extrude_cross_section: 500"),
            actual.index("[include t300_core.cfg]"),
        )
        self.assertLess(
            actual.index("[include t300_core.cfg]"),
            actual.index("#*# <---------------------- SAVE_CONFIG"),
        )

    def test_include_detection_accepts_spacing_and_comments(self):
        text = " [include   macro_z_tilt_via_knob.cfg] # selected workflow\n"
        self.assertTrue(t300ctl.has_config_include(text, t300ctl.GERGO_MACRO_FILENAME))

    def test_open_leveling_refuses_selected_gergo_workflow(self):
        text = "[include Macro.cfg]\n[include macro_z_tilt_via_knob.cfg]\n"
        with self.assertRaisesRegex(t300ctl.T300Error, "competing leveling"):
            t300ctl.validate_leveling_exclusivity(
                text, t300ctl.OPEN_MACRO_FILENAME
            )

    def test_gergo_refuses_selected_open_workflow(self):
        text = "[include Macro.cfg]\n[include t300_gantry_level.cfg]\n"
        with self.assertRaisesRegex(t300ctl.T300Error, "competing leveling"):
            t300ctl.validate_leveling_exclusivity(
                text, t300ctl.GERGO_MACRO_FILENAME
            )

    def test_runtime_includes_are_last_and_ordered(self):
        original = (
            "[include Macro.cfg]\n"
            "[include mainsail_client.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            "[printer]\nmax_velocity: 600\n"
            "[include t300_runtime.cfg]\n"
            "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
        )
        actual, changed = t300ctl.patch_runtime_printer_cfg(original)
        self.assertTrue(changed)
        self.assertLess(actual.index("max_velocity"), actual.index("[include mainsail_client.cfg]"))
        self.assertLess(
            actual.index("[include mainsail_client.cfg]"),
            actual.index("[include t300_runtime.cfg]"),
        )
        self.assertLess(
            actual.index("[include t300_runtime.cfg]"),
            actual.index("#*# <---------------------- SAVE_CONFIG"),
        )

    def test_runtime_include_patch_is_idempotent(self):
        original = (
            "[include Macro.cfg]\n"
            "[printer]\nmax_velocity: 600\n"
            "[include mainsail_client.cfg]\n"
            "[include t300_runtime.cfg]\n"
        )
        actual, changed = t300ctl.patch_runtime_printer_cfg(original)
        self.assertFalse(changed)
        self.assertEqual(actual, original)


class InstallerDryRunTests(unittest.TestCase):
    class FakeClient:
        def get_json(self, path):
            if path == "/printer/info":
                return {"state": "ready", "software_version": "v0.12.0"}
            if path == "/printer/objects/query?print_stats=state,filename":
                return {"status": {"print_stats": {"state": "standby"}}}
            if path == "/server/files/roots":
                return [{"name": "config", "permissions": "rw"}]
            if path == "/server/files/list?root=config":
                return [{"path": "printer.cfg", "permissions": "rw"}]
            raise AssertionError(path)

        def download_bytes(self, root, filename, limit):
            if (root, filename) != ("config", "printer.cfg"):
                raise AssertionError((root, filename, limit))
            return b"[include Macro.cfg]\n[mcu]\n"

        def upload_config(self, filename, content):
            raise AssertionError((filename, content))

    def test_generic_installer_uses_requested_include_filename(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            t300ctl.install_config_macro(
                self.FakeClient(),
                t300ctl.read_open_macro(),
                t300ctl.OPEN_MACRO_FILENAME,
                False,
                None,
            )
        rendered = output.getvalue()
        self.assertIn("+[include t300_gantry_level.cfg]", rendered)
        self.assertNotIn("+[include macro_z_tilt_via_knob.cfg]", rendered)


class MacroReadTests(unittest.TestCase):
    CONTENT = b"[gcode_macro T300_TEST]\ngcode:\n  RESPOND MSG=ok\n"

    FACTORY_END_PRINT = """[gcode_macro END_PRINT]
gcode:
    {% set z_max = printer['gcode_macro global_var'].z_maximum_lifting_distance|int %}
    G91
    {% if (printer.gcode_move.position.z + 10) < z_max %}
        G1 Z+10 F3000
    {% else %}
        G1 Z+{(z_max - printer.gcode_move.position.z)} F3000
    {% endif %}
    G90
    G1 X0 Y300

[gcode_macro CANCEL_PRINT]
rename_existing: CANCEL_PRINT_BASE
gcode:
    {% set x_park = 0|float %}
    {% set y_park = 300|float %}
    {% set z_lift_max = 350 %}
    CANCEL_PRINT_BASE
    {% if printer.pause_resume.is_paused == True %}
        {% if printer.gcode_move.position.x != x_park %}
            G91
            G1 Z+10 F3000
            G90
            G1 X{x_park} Y{y_park} F6000
        {% endif %}
    {% else %}
        {% if "xyz" not in printer.toolhead.homed_axe %}
            G91
            G1 Z+10 F3000
        {% endif %}
        G90
        G1 X{x_park} Y{y_park} F6000
    {% endif %}
    TURN_OFF_HEATERS

[gcode_macro NEXT]
gcode:
    M117 next
"""

    def test_end_clean_height_replaces_only_factory_lift(self):
        actual, changed = t300ctl.patch_end_print_clean_height(
            self.FACTORY_END_PRINT, 200
        )
        self.assertTrue(changed)
        self.assertIn(
            "{% set clean_z = [printer.gcode_move.position.z + 10, 200] | max %}",
            actual,
        )
        self.assertIn("{% set clean_z = [clean_z, z_max] | min %}", actual)
        self.assertIn("G1 Z{clean_z} F3000", actual)
        self.assertNotIn("G1 Z+10 F3000", actual)
        self.assertIn("# T300 laptop cancel-cleaning park", actual)
        self.assertIn("{% set homed_axes = printer.toolhead.homed_axes|lower %}", actual)
        self.assertNotIn("printer.toolhead.homed_axe %}", actual)
        cancel = t300ctl.config_section(actual, "gcode_macro", "CANCEL_PRINT")
        self.assertIsNotNone(cancel)
        assert cancel is not None
        self.assertLess(cancel.index("G1 Z{clean_z}"), cancel.index("G1 X{x_park}"))
        self.assertLess(cancel.index("G1 X{x_park}"), cancel.index("TURN_OFF_HEATERS"))
        self.assertIn("[gcode_macro NEXT]", actual)

    def test_end_clean_height_is_idempotent_and_updateable(self):
        once, _ = t300ctl.patch_end_print_clean_height(self.FACTORY_END_PRINT, 200)
        same, changed = t300ctl.patch_end_print_clean_height(once, 200)
        self.assertFalse(changed)
        self.assertEqual(same, once)
        updated, changed = t300ctl.patch_end_print_clean_height(once, 220)
        self.assertTrue(changed)
        self.assertEqual(updated.count("position.z + 10, 220] | max"), 2)

    def test_end_clean_height_rejects_unbounded_target(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "between 20 and 340"):
            t300ctl.patch_end_print_clean_height(self.FACTORY_END_PRINT, 350)

    def test_reads_cfg(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / t300ctl.GERGO_MACRO_FILENAME
            source.write_bytes(self.CONTENT)
            self.assertEqual(t300ctl.read_macro(source), self.CONTENT)

    def test_reads_one_macro_from_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "macro_v3.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("nested/" + t300ctl.GERGO_MACRO_FILENAME, self.CONTENT)
            self.assertEqual(t300ctl.read_macro(source), self.CONTENT)

    def test_reads_macro_from_official_nested_package(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "official-download.zip"
            inner_data = io.BytesIO()
            with zipfile.ZipFile(inner_data, "w") as inner:
                inner.writestr(t300ctl.GERGO_MACRO_FILENAME, self.CONTENT)
            with zipfile.ZipFile(source, "w") as outer:
                outer.writestr(
                    "gerGoPrint3D/t300/macro_v3(extract!).zip", inner_data.getvalue()
                )
            self.assertEqual(t300ctl.read_macro(source), self.CONTENT)

    def test_rejects_ambiguous_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "macro_v3.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("a/" + t300ctl.GERGO_MACRO_FILENAME, self.CONTENT)
                archive.writestr("b/" + t300ctl.GERGO_MACRO_FILENAME, self.CONTENT)
            with self.assertRaises(t300ctl.T300Error):
                t300ctl.read_macro(source)

    def test_extracts_macro_names(self):
        self.assertEqual(t300ctl.macro_names(self.CONTENT), ["T300_TEST"])

    def test_bundled_open_macro_has_expected_sections(self):
        content = t300ctl.read_open_macro()
        self.assertEqual(
            t300ctl.macro_names(content),
            ["GANTRY_LEVEL_T300", "_TGL_CAPTURE_RIGHT", "_TGL_REPORT"],
        )

    def test_bundled_core_macro_has_expected_public_commands(self):
        content = t300ctl.read_core_macro()
        names = t300ctl.macro_names(content)
        self.assertIn("T_CORE_STATUS", names)
        self.assertNotIn("T300_CORE_STATUS", names)
        self.assertIn("START_PRINT", names)
        self.assertIn("END_PRINT", names)
        self.assertIn("RESUME_INTERRUPTED", names)

    def test_kamp_subset_owns_only_settings_park_and_purge(self):
        content = t300ctl.read_kamp_macro()
        self.assertEqual(
            set(t300ctl.macro_names(content)),
            {"_KAMP_Settings", "LINE_PURGE", "SMART_PARK"},
        )
        self.assertNotIn(b"[gcode_macro BED_MESH_CALIBRATE]", content)
        self.assertIn(t300ctl.KAMP_REVISION.encode("ascii"), content)

    def test_kamp_subset_keeps_reviewed_settings(self):
        text = t300ctl.read_kamp_macro().decode("utf-8")
        settings = t300ctl.config_section(text, "gcode_macro", "_KAMP_Settings")
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertIn(
            f"variable_tip_distance: {t300ctl.KAMP_TIP_DISTANCE}", settings
        )
        self.assertNotIn("variable_tip_distance: 3.5", settings)
        self.assertIn(
            f"variable_purge_margin: {t300ctl.KAMP_PURGE_MARGIN}", settings
        )
        self.assertNotIn("variable_purge_margin: 10", settings)
        self.assertIn("variable_purge_amount: 30", settings)
        self.assertIn("variable_flow_rate: 12", settings)
        self.assertIn("variable_smart_park_height: 10", settings)

    def test_mainsail_client_is_pinned_and_owns_only_recovery_controls(self):
        content = t300ctl.read_mainsail_client()
        names = set(t300ctl.macro_names(content))
        self.assertTrue({"PAUSE", "RESUME", "CANCEL_PRINT"}.issubset(names))
        self.assertNotIn("START_PRINT", names)
        self.assertNotIn("END_PRINT", names)
        self.assertIn(b"path: ~/gcode_files", content)
        self.assertNotIn(b"path: ~/printer_data/gcodes", content)
        self.assertIn(t300ctl.MAINSAIL_CLIENT_REVISION.encode("ascii"), content)

    def test_runtime_addresses_only_reviewed_settings(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        start = t300ctl.config_section(text, "gcode_macro", "START_PRINT")
        idle = t300ctl.config_section(text, "idle_timeout")
        extruder = t300ctl.config_section(text, "extruder")
        force_move = t300ctl.config_section(text, "force_move")
        self.assertIsNotNone(start)
        self.assertIsNotNone(idle)
        self.assertIsNotNone(extruder)
        self.assertIsNotNone(force_move)
        assert start and idle and extruder and force_move
        ordered = [
            "M104 S150",
            "M99190 S{bed_temp}",
            "G28",
            "BED_MESH_CALIBRATE BED_TEMP={bed_temp}",
            "SMART_PARK",
            "M99109 S{nozzle_temp}",
            "LINE_PURGE",
        ]
        positions = [start.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(start.count("M190 S{bed_temp}"), 1)
        self.assertGreater(
            start.index("M190 S{bed_temp}"),
            start.index("BED_MESH_CALIBRATE BED_TEMP={bed_temp}"),
        )
        self.assertNotRegex(start, r"(?mi)^\s*G[01]\s+E[0-9]")
        self.assertIn("TURN_OFF_HEATERS", idle)
        self.assertNotIn("M84", idle)
        self.assertIn("printer.pause_resume.is_paused", idle)
        self.assertIn("M104 S0", idle)
        self.assertIn("VARIABLE=idle_state VALUE=True", idle)
        client = t300ctl.config_section(text, "gcode_macro", "_CLIENT_VARIABLE")
        self.assertIsNotNone(client)
        assert client
        self.assertIn("variable_idle_timeout: 3600", client)
        self.assertIn("max_extrude_cross_section: 5", extruder)
        self.assertNotIn("max_extrude_only_velocity", extruder)
        self.assertIn("enable_force_move: False", force_move)

    def test_runtime_exit_never_targets_below_current_z(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        exit_macro = t300ctl.config_section(text, "gcode_macro", "_T_RUNTIME_SAFE_EXIT")
        self.assertIsNotNone(exit_macro)
        assert exit_macro
        self.assertIn("[current_z + 10.0, 200.0]|max", exit_macro)
        self.assertIn("max_z]|min", exit_macro)
        self.assertIn("if clean_z > current_z", exit_macro)
        self.assertIn('if "xyz" in homed', exit_macro)

    def test_runtime_cancel_completes_native_cancel_before_optional_motion(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        hook = t300ctl.config_section(text, "gcode_macro", "_T_RUNTIME_CANCEL_EXIT")
        post = t300ctl.config_section(text, "delayed_gcode", "_T_RUNTIME_CANCEL_POST")
        self.assertIsNotNone(hook)
        self.assertIsNotNone(post)
        assert hook and post
        self.assertIn("UPDATE_DELAYED_GCODE", hook)
        self.assertNotIn("_T_RUNTIME_SAFE_EXIT", hook)
        self.assertIn("_T_RUNTIME_SAFE_EXIT", post)
        self.assertIn("clear_last_file", post)

    def test_motor_release_uses_authoritative_pause_state(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        release = t300ctl.config_section(text, "gcode_macro", "T_RELEASE_MOTORS")
        self.assertIsNotNone(release)
        assert release
        self.assertIn("printer.pause_resume.is_paused", release)
        self.assertIn('state == "paused"', release)

    def test_filament_helpers_reject_cold_extrusion(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        for name in ("DEFAULT_LOAD_FILAMENT", "DEFAULT_UNLOAD_FILAMENT"):
            section = t300ctl.config_section(text, "gcode_macro", name)
            self.assertIsNotNone(section)
            assert section
            self.assertIn("printer.extruder.can_extrude", section)
            self.assertIn("action_raise_error", section)

    def test_filament_helpers_require_pause_during_a_print(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        for name in ("DEFAULT_LOAD_FILAMENT", "DEFAULT_UNLOAD_FILAMENT"):
            section = t300ctl.config_section(text, "gcode_macro", name)
            self.assertIsNotNone(section)
            assert section
            self.assertIn(
                'printer.print_stats.state|lower == "printing"', section
            )
            self.assertLess(section.index("print_stats.state"), section.index("G1 E"))

    def test_runtime_has_no_semicolon_inside_active_lines(self):
        text = t300ctl.read_runtime_macro().decode("utf-8")
        active_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertNotIn(";", "\n".join(active_lines))

    def test_core_has_no_semicolon_inside_active_lines(self):
        text = t300ctl.read_core_macro().decode("utf-8")
        active_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertNotIn(";", "\n".join(active_lines))

    def test_core_start_keeps_nozzle_cold_until_after_mesh_and_clearance(self):
        text = t300ctl.read_core_macro().decode("utf-8")
        start = t300ctl.config_section(text, "gcode_macro", "START_PRINT")
        self.assertIsNotNone(start)
        assert start is not None
        ordered = [
            "M104 S0",
            "G28",
            'mesh_mode == "FULL"',
            "BED_MESH_CALIBRATE BED_TEMP={bed_temp}",
            "G1 Z10 F600",
            "G1 X20 Y20 F6000",
            "M99109 S{nozzle_temp}",
            "G1 Z0.30 F600",
            "G1 Y100 E7 F1200",
        ]
        positions = [start.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertNotRegex(start, r"(?mi)^\s*G1\s+E(?:\d|\.){2,}")

    def test_core_restores_idle_and_extrusion_guards(self):
        text = t300ctl.read_core_macro().decode("utf-8")
        idle = t300ctl.config_section(text, "idle_timeout")
        extruder = t300ctl.config_section(text, "extruder")
        force_move = t300ctl.config_section(text, "force_move")
        self.assertIsNotNone(idle)
        self.assertIsNotNone(extruder)
        self.assertIsNotNone(force_move)
        assert idle is not None and extruder is not None and force_move is not None
        self.assertLess(idle.index("TURN_OFF_HEATERS"), idle.index("M84"))
        self.assertIn("max_extrude_cross_section: 1.0", extruder)
        self.assertIn("max_extrude_only_velocity: 60", extruder)
        self.assertIn("enable_force_move: False", force_move)

    def test_core_uses_exact_filament_sensor_status_field(self):
        text = t300ctl.read_core_macro().decode("utf-8")
        resume = t300ctl.config_section(text, "gcode_macro", "RESUME")
        self.assertIsNotNone(resume)
        assert resume is not None
        self.assertIn(".enabled", resume)
        self.assertNotIn(".enable and", resume)


class CoreCompatibilityTests(unittest.TestCase):
    ALIASES = {
        "BED_MESH_CALIBRATE": "BED_MESH_CALIBRATE_BASE",
        "PAUSE": "PAUSE_BASE",
        "RESUME": "RESUME_BASE",
        "CANCEL_PRINT": "CANCEL_PRINT_BASE",
        "M109": "M99109",
        "M190": "M99190",
    }
    HELPERS = {
        "START_PRINT",
        "END_PRINT",
        "DEFAULT_LOAD_FILAMENT",
        "DEFAULT_UNLOAD_FILAMENT",
        "PRINTING_UNLOAD_FILAMENT",
        "M600",
        "PAUSE_UNLOAD_FILAMENT",
        "LOAD_FILAMENT_RESUME",
    }

    @classmethod
    def factory_macros(cls, aliases=None):
        selected = cls.ALIASES if aliases is None else aliases
        sections = [
            f"[gcode_macro {name}]\nrename_existing: {alias}\ngcode:\n  M117 test\n"
            for name, alias in selected.items()
        ]
        sections.extend(
            f"[gcode_macro {name}]\ngcode:\n  M117 test\n" for name in cls.HELPERS
        )
        return "\n".join(sections)

    @staticmethod
    def printer_cfg(extra=""):
        return (
            "[include Macro.cfg]\n"
            "[include macro_z_tilt_via_knob.cfg]\n"
            f"{extra}"
        )

    PLR = (
        "[force_move]\nenable_force_move: True\n\n"
        "[gcode_macro RESUME_INTERRUPTED]\ngcode:\n  M117 test\n"
    )

    def test_accepts_tested_vendor_contract(self):
        t300ctl.validate_core_compatibility(
            self.printer_cfg(),
            self.factory_macros(),
            self.PLR,
            "v0.12.0-113-g28f06a10-dirty",
        )

    def test_rejects_another_klipper_family(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "pinned.*0.12.0"):
            t300ctl.validate_core_compatibility(
                self.printer_cfg(), self.factory_macros(), self.PLR, "v0.13.0"
            )

    def test_requires_selected_gergo_include(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "GerGo"):
            t300ctl.validate_core_compatibility(
                "[include Macro.cfg]\n",
                self.factory_macros(),
                self.PLR,
                "v0.12.0",
            )

    def test_rejects_competing_gantry_macro(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "competing"):
            t300ctl.validate_core_compatibility(
                self.printer_cfg("[include t300_gantry_level.cfg]\n"),
                self.factory_macros(),
                self.PLR,
                "v0.12.0",
            )

    def test_rejects_changed_factory_alias(self):
        aliases = dict(self.ALIASES)
        aliases["PAUSE"] = "SOMETHING_ELSE"
        with self.assertRaisesRegex(t300ctl.T300Error, "PAUSE_BASE"):
            t300ctl.validate_core_compatibility(
                self.printer_cfg(),
                self.factory_macros(aliases),
                self.PLR,
                "v0.12.0",
            )

    def test_core_installer_is_quarantined_before_printer_access(self):
        class NoPrinterAccess:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected printer access: {name}")

        with self.assertRaisesRegex(t300ctl.T300Error, "quarantined"):
            t300ctl.install_core_macro(NoPrinterAccess(), False, None, False)


class KampCompatibilityTests(unittest.TestCase):
    PRINTER = (
        "[include Macro.cfg]\n"
        "[include macro_z_tilt_via_knob.cfg]\n"
        "[exclude_object]\n"
        "[extruder]\nmax_extrude_cross_section: 500\n"
    )
    MACRO = (
        "[gcode_macro BED_MESH_CALIBRATE]\n"
        "rename_existing: BED_MESH_CALIBRATE_BASE\n"
        "gcode:\n  BED_MESH_CALIBRATE_BASE ADAPTIVE=1\n"
    )

    def test_accepts_reviewed_native_mesh_contract(self):
        t300ctl.validate_kamp_compatibility(self.PRINTER, self.MACRO, "v0.12.0-test")

    def test_rejects_kamp_when_native_mesh_contract_changes(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "native adaptive"):
            t300ctl.validate_kamp_compatibility(
                self.PRINTER, self.MACRO.replace(" ADAPTIVE=1", ""), "v0.12.0-test"
            )

    def test_rejects_quarantined_core_include(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "quarantined"):
            t300ctl.validate_kamp_compatibility(
                self.PRINTER + "[include t300_core.cfg]\n", self.MACRO, "v0.12.0-test"
            )


class RuntimeCompatibilityTests(unittest.TestCase):
    PRINTER = (
        "[include Macro.cfg]\n"
        "[include kamp_t300.cfg]\n"
        "[include macro_z_tilt_via_knob.cfg]\n"
        "[exclude_object]\n"
        "[filament_switch_sensor my_sensor]\nswitch_pin: PC4\n"
        "[stepper_x]\nposition_min: -2\nposition_max: 302\n"
        "[stepper_y]\nposition_min: -6\nposition_max: 302\n"
        "[stepper_z]\nposition_max: 370\n"
    )
    FACTORY = (
        "[gcode_macro START_PRINT]\nvariable_state: 'Prepare'\ngcode:\n  G28\n\n"
        "[gcode_macro PAUSE]\nrename_existing: PAUSE_BASE\ngcode:\n  PAUSE_BASE\n\n"
        "[gcode_macro RESUME]\nrename_existing: RESUME_BASE\ngcode:\n  RESUME_BASE\n\n"
        "[gcode_macro CANCEL_PRINT]\nrename_existing: CANCEL_PRINT_BASE\ngcode:\n  CANCEL_PRINT_BASE\n\n"
        "[gcode_macro M109]\nrename_existing: M99109\ngcode:\n  M99109 {rawparams}\n\n"
        "[gcode_macro M190]\nrename_existing: M99190\ngcode:\n  M99190 {rawparams}\n"
    )
    PLR = (
        "[force_move]\nenable_force_move: True\n\n"
        "[gcode_macro RESUME_INTERRUPTED]\ngcode:\n  M117 recovery\n\n"
        "[gcode_macro clear_last_file]\ngcode:\n  M117 clear\n"
    )

    def test_accepts_captured_t300_contract(self):
        t300ctl.validate_runtime_compatibility(
            self.PRINTER,
            self.FACTORY,
            self.PLR,
            t300ctl.read_kamp_macro().decode("utf-8"),
            "v0.12.0-113-test",
        )

    def test_rejects_stationary_kamp_tip_advance(self):
        kamp = t300ctl.read_kamp_macro().decode("utf-8").replace(
            "variable_tip_distance: 0", "variable_tip_distance: 3.5"
        )
        with self.assertRaisesRegex(t300ctl.T300Error, "tip advance"):
            t300ctl.validate_runtime_compatibility(
                self.PRINTER, self.FACTORY, self.PLR, kamp, "v0.12.0-test"
            )

    def test_rejects_quarantined_core(self):
        with self.assertRaisesRegex(t300ctl.T300Error, "quarantined"):
            t300ctl.validate_runtime_compatibility(
                self.PRINTER + "[include t300_core.cfg]\n",
                self.FACTORY,
                self.PLR,
                t300ctl.read_kamp_macro().decode("utf-8"),
                "v0.12.0-test",
            )

class BackupTests(unittest.TestCase):
    class FakeClient:
        base_url = "http://10.42.42.2"
        files = {
            "printer.cfg": b"[include Macro.cfg]\n",
            "nested/Macro.cfg": b"[gcode_macro TEST]\ngcode:\n  M117 test\n",
        }

        def get_json(self, path):
            if path != "/server/files/list?root=config":
                raise AssertionError(path)
            return [
                {"path": name, "size": len(content), "permissions": "rw"}
                for name, content in self.files.items()
            ]

        def download_bytes(self, root, filename, limit):
            if root != "config" or len(self.files[filename]) > limit:
                raise AssertionError((root, filename, limit))
            return self.files[filename]

    def test_backup_separates_remote_files_from_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup"
            result = t300ctl.make_backup(self.FakeClient(), target)
            self.assertEqual(result, target.resolve())
            self.assertEqual(
                (target / "config-root" / "printer.cfg").read_bytes(),
                self.FakeClient.files["printer.cfg"],
            )
            self.assertTrue((target / "manifest.json").is_file())
            self.assertTrue((target / "SHA256SUMS").is_file())
            self.assertEqual(t300ctl.verify_backup(target), 2)
            sums = (target / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertIn("  config-root/printer.cfg", sums)

    def test_backup_verifier_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup"
            t300ctl.make_backup(self.FakeClient(), target)
            (target / "config-root" / "printer.cfg").write_text(
                "changed", encoding="utf-8"
            )
            with self.assertRaisesRegex(t300ctl.T300Error, "checksum mismatch"):
                t300ctl.verify_backup(target)

    def test_backup_verifier_accepts_legacy_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup"
            t300ctl.make_backup(self.FakeClient(), target)
            sums_path = target / "SHA256SUMS"
            sums_path.write_text(
                sums_path.read_text(encoding="utf-8").replace("  config-root/", "  "),
                encoding="utf-8",
            )
            self.assertEqual(t300ctl.verify_backup(target), 2)

    def test_backup_refuses_non_directory_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup"
            target.write_text("occupied", encoding="utf-8")
            with self.assertRaises(t300ctl.T300Error):
                t300ctl.make_backup(self.FakeClient(), target)


class ConfigTransactionTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, files, fail_after_write=None, print_states=None):
            self.files = dict(files)
            self.fail_after_write = fail_after_write
            self.print_states = list(print_states or ["standby"])
            self.uploads = []
            self.deletes = []
            self.restarts = 0
            self.restart_poll = 0

        def get_json(self, path):
            if path == "/server/files/list?root=config":
                return [
                    {"path": name, "size": len(content), "permissions": "rw"}
                    for name, content in self.files.items()
                ]
            if path == "/printer/info":
                return {"state": "ready", "state_message": "ready"}
            if path == "/printer/objects/query?print_stats=state,filename":
                if len(self.print_states) > 1:
                    state = self.print_states.pop(0)
                else:
                    state = self.print_states[0]
                filename = "job.gcode" if state in {"printing", "paused"} else ""
                return {"status": {"print_stats": {"state": state, "filename": filename}}}
            if path == "/server/info":
                if self.restart_poll == 1:
                    self.restart_poll = 2
                    return {"klippy_connected": False, "klippy_state": "disconnected"}
                return {"klippy_connected": True, "klippy_state": "ready"}
            raise AssertionError(path)

        def download_bytes(self, root, filename, limit):
            if root != "config" or len(self.files[filename]) > limit:
                raise AssertionError((root, filename, limit))
            return self.files[filename]

        def upload_config(self, filename, content):
            self.uploads.append(filename)
            self.files[filename] = content
            if self.fail_after_write == filename:
                self.fail_after_write = None
                raise t300ctl.T300Error("simulated interrupted upload response")

        def delete_file(self, root, filename):
            self.deletes.append((root, filename))
            self.files.pop(filename, None)

        def post_json(self, path, payload=None):
            if path != "/printer/firmware_restart":
                raise AssertionError((path, payload))
            self.restarts += 1
            self.restart_poll = 1

    def test_success_requires_readback_and_restart(self):
        original = b"[include Macro.cfg]\n"
        proposed = original + b"[include candidate.cfg]\n"
        client = self.FakeClient({"printer.cfg": original})
        t300ctl.apply_config_transaction(
            client,
            {"candidate.cfg": b"macro", "printer.cfg": proposed},
            {"printer.cfg": original, "candidate.cfg": None},
        )
        self.assertEqual(client.files["candidate.cfg"], b"macro")
        self.assertEqual(client.files["printer.cfg"], proposed)
        self.assertEqual(client.restarts, 1)

    def test_concurrent_change_is_rejected_before_upload(self):
        reviewed = b"[include Macro.cfg]\n"
        client = self.FakeClient({"printer.cfg": reviewed + b"# concurrent\n"})
        with self.assertRaisesRegex(t300ctl.T300Error, "Read-back verification"):
            t300ctl.apply_config_transaction(
                client,
                {"candidate.cfg": b"macro"},
                {"printer.cfg": reviewed, "candidate.cfg": None},
            )
        self.assertEqual(client.uploads, [])

    def test_interrupted_new_file_upload_is_deleted_on_rollback(self):
        original = b"[include Macro.cfg]\n"
        client = self.FakeClient(
            {"printer.cfg": original}, fail_after_write="candidate.cfg"
        )
        with self.assertRaisesRegex(t300ctl.T300Error, "previous files were restored"):
            t300ctl.apply_config_transaction(
                client,
                {"candidate.cfg": b"macro"},
                {"printer.cfg": original, "candidate.cfg": None},
            )
        self.assertNotIn("candidate.cfg", client.files)
        self.assertEqual(client.deletes, [("config", "candidate.cfg")])
        self.assertEqual(client.restarts, 0)

    def test_keyboard_interrupt_during_upload_rolls_back_without_restart(self):
        original = b"[include Macro.cfg]\n"
        client = self.FakeClient({"printer.cfg": original})

        def interrupted_upload(filename, content):
            client.uploads.append(filename)
            client.files[filename] = content
            raise KeyboardInterrupt

        client.upload_config = interrupted_upload
        with self.assertRaisesRegex(t300ctl.T300Error, "interrupted by user"):
            t300ctl.apply_config_transaction(
                client,
                {"candidate.cfg": b"macro"},
                {"printer.cfg": original, "candidate.cfg": None},
            )
        self.assertEqual(client.files, {"printer.cfg": original})
        self.assertEqual(client.restarts, 0)

    def test_failed_restart_restores_both_files(self):
        original = b"[include Macro.cfg]\n"
        proposed = original + b"[include candidate.cfg]\n"
        client = self.FakeClient({"printer.cfg": original})
        with mock.patch.object(
            t300ctl,
            "wait_for_restart_ready",
            side_effect=[(False, "bad config"), (True, "ready")],
        ):
            with self.assertRaisesRegex(t300ctl.T300Error, "previous files were restored"):
                t300ctl.apply_config_transaction(
                    client,
                    {"candidate.cfg": b"macro", "printer.cfg": proposed},
                    {"printer.cfg": original, "candidate.cfg": None},
                )
        self.assertEqual(client.files, {"printer.cfg": original})
        self.assertEqual(client.restarts, 2)

    def test_rechecks_idle_state_immediately_before_upload(self):
        client = self.FakeClient({"printer.cfg": b"original"})

        def active_print(path):
            if path == "/server/files/list?root=config":
                return [{"path": "printer.cfg", "size": 8, "permissions": "rw"}]
            if path == "/printer/info":
                return {"state": "ready", "state_message": "ready"}
            if path == "/printer/objects/query?print_stats=state,filename":
                return {"status": {"print_stats": {"state": "printing", "filename": "job.gcode"}}}
            raise AssertionError(path)

        client.get_json = active_print
        with self.assertRaisesRegex(t300ctl.T300Error, "currently printing"):
            t300ctl.apply_config_transaction(
                client,
                {"printer.cfg": b"proposed"},
                {"printer.cfg": b"original"},
            )
        self.assertEqual(client.uploads, [])

    def test_print_started_during_upload_is_restored_without_restart(self):
        original = b"[include Macro.cfg]\n"
        proposed = original + b"[include candidate.cfg]\n"
        client = self.FakeClient(
            {"printer.cfg": original}, print_states=["standby", "printing"]
        )
        with self.assertRaisesRegex(t300ctl.T300Error, "previous files were restored"):
            t300ctl.apply_config_transaction(
                client,
                {"candidate.cfg": b"macro", "printer.cfg": proposed},
                {"printer.cfg": original, "candidate.cfg": None},
            )
        self.assertEqual(client.files, {"printer.cfg": original})
        self.assertEqual(client.restarts, 0)


class RestartWaitTests(unittest.TestCase):
    class SequenceClient:
        def __init__(self, states):
            self.states = list(states)

        def get_json(self, path):
            if path == "/server/info":
                if len(self.states) > 1:
                    return self.states.pop(0)
                return self.states[0]
            if path == "/printer/info":
                return {"state": "ready", "state_message": "Printer is ready"}
            raise AssertionError(path)

    @mock.patch.object(t300ctl.time, "sleep", return_value=None)
    def test_stale_ready_is_ignored_until_restart_transition(self, _sleep):
        client = self.SequenceClient(
            [
                {"klippy_connected": True, "klippy_state": "ready"},
                {"klippy_connected": False, "klippy_state": "disconnected"},
                {"klippy_connected": True, "klippy_state": "startup"},
                {"klippy_connected": True, "klippy_state": "ready"},
            ]
        )
        ready, message = t300ctl.wait_for_restart_ready(client, seconds=1)
        self.assertTrue(ready)
        self.assertEqual(message, "Printer is ready")

    @mock.patch.object(t300ctl.time, "sleep", return_value=None)
    @mock.patch.object(t300ctl.time, "monotonic", side_effect=[0.0, 0.0, 0.3, 0.6, 1.1])
    def test_stale_ready_alone_times_out(self, _monotonic, _sleep):
        client = self.SequenceClient(
            [{"klippy_connected": True, "klippy_state": "ready"}]
        )
        ready, message = t300ctl.wait_for_restart_ready(client, seconds=1)
        self.assertFalse(ready)
        self.assertIn("Klippy state: ready", message)


class ConfigPermissionsTests(unittest.TestCase):
    class ModernClient:
        def get_json(self, path):
            if path != "/server/files/roots":
                raise AssertionError(path)
            return [{"name": "config", "permissions": "rw"}]

    class FactoryClient:
        def get_json(self, path):
            if path == "/server/files/roots":
                raise t300ctl.T300Error("Moonraker returned HTTP 404: Not Found")
            if path == "/server/files/list?root=config":
                return [
                    {"path": "Macro.cfg", "permissions": "rw"},
                    {"path": "printer.cfg", "permissions": "rw"},
                ]
            raise AssertionError(path)

    def test_reads_modern_root_permissions(self):
        self.assertEqual(
            t300ctl.config_permissions(self.ModernClient()),
            "rw",
        )

    def test_falls_back_to_factory_file_permissions(self):
        self.assertEqual(
            t300ctl.config_permissions(self.FactoryClient()),
            "rw",
        )


class UploadTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

    class Opener:
        def __init__(self):
            self.request = None

        def open(self, request, timeout):
            self.request = request
            return UploadTests.Response(b'{"result":{"action":"create_file"}}')

    def test_upload_uses_config_root_and_checksum(self):
        client = t300ctl.Moonraker("10.42.42.2")
        opener = self.Opener()
        client.opener = opener
        content = b"[gcode_macro TEST]\n"
        result = client.upload_config(t300ctl.GERGO_MACRO_FILENAME, content)

        self.assertEqual(result["action"], "create_file")
        self.assertIsNotNone(opener.request)
        request_body = opener.request.data
        self.assertIn(b'name="root"\r\n\r\nconfig\r\n', request_body)
        self.assertIn(t300ctl.GERGO_MACRO_FILENAME.encode(), request_body)
        self.assertIn(hashlib.sha256(content).hexdigest().encode(), request_body)
        self.assertIn(content, request_body)


class OpenGeometryTests(unittest.TestCase):
    class FakeClient:
        def post_json(self, path, payload):
            if path != "/printer/objects/query":
                raise AssertionError(path)
            return {
                "status": {
                    "configfile": {
                        "settings": {
                            "bed_mesh": {"mesh_min": [20.0, 25.0], "mesh_max": [280.0, 275.0]},
                            "probe": {"x_offset": -25.0, "y_offset": 10.0},
                            "stepper_x": {"position_min": 0.0, "position_max": 310.0},
                            "stepper_y": {"position_min": 0.0, "position_max": 300.0},
                            "stepper_z": {"rotation_distance": 8.0},
                        }
                    }
                }
            }

    def test_derives_safe_nozzle_points(self):
        geometry = t300ctl.open_level_geometry(self.FakeClient())
        self.assertAlmostEqual(geometry["probe_left"], 33.0)
        self.assertAlmostEqual(geometry["probe_right"], 267.0)
        self.assertAlmostEqual(geometry["nozzle_left"], 58.0)
        self.assertAlmostEqual(geometry["nozzle_right"], 292.0)
        self.assertAlmostEqual(geometry["nozzle_y"], 140.0)
        self.assertAlmostEqual(geometry["rotation_distance"], 8.0)


if __name__ == "__main__":
    unittest.main()

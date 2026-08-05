from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "update-orca-kamp-startup.py"
SPEC = importlib.util.spec_from_file_location("update_orca_project", MODULE_PATH)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class OrcaProjectUpdateTests(unittest.TestCase):
    def test_reusable_machine_profile_uses_only_reviewed_lifecycle_hooks(self):
        profile_path = (
            MODULE_PATH.parents[1]
            / "orcaslicer/T300 AUDITED Runtime 0.4 - REVIEW ONLY.json"
        )
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile["machine_start_gcode"], updater.RUNTIME_START_GCODE)
        self.assertEqual(profile["machine_end_gcode"], updater.RUNTIME_END_GCODE)
        self.assertEqual(
            profile["before_layer_change_gcode"],
            updater.TIMELAPSE_BEFORE_LAYER_GCODE,
        )
        for key, value in updater.RUNTIME_REQUIRED_SETTINGS.items():
            self.assertEqual(profile[key], value)
        self.assertNotIn("G1 ", profile["machine_start_gcode"])
        self.assertNotIn("BED_MESH", profile["machine_start_gcode"])

    def make_project(self, path: Path, start: str) -> bytes:
        settings = {
            "machine_start_gcode": start,
            "machine_end_gcode": "END_PRINT\n",
            "before_layer_change_gcode": ";BEFORE_LAYER_CHANGE\n;[layer_z]\nG92 E0\n",
            "nozzle_temperature_initial_layer": ["215"],
            "bottom_solid_infill_flow_ratio": "1",
            "filament_retraction_length": ["0.5"],
            "reduce_infill_retraction": "1",
            "print_sequence": "by layer",
            "exclude_object": "0",
            "gcode_label_objects": "0",
            "gcode_flavor": "marlin",
            "enable_power_loss_recovery": "enable",
            "z_hop": ["0.4"],
            "z_hop_types": ["Slope Lift"],
            "retract_restart_extra": ["0"],
            "filament_retract_restart_extra": ["nil"],
            "adaptive_pressure_advance": ["0"],
            "enable_pressure_advance": ["0"],
            "pressure_advance": ["0.02"],
            "filament_settings_id": ["Old PLA"],
            "print_settings_id": "Old process",
        }
        model = b"unchanged-model-data"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                updater.PROJECT_SETTINGS,
                json.dumps(settings).encode("utf-8"),
            )
            archive.writestr("3D/3dmodel.model", model)
        return model

    def test_migrates_only_supported_project_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            model = self.make_project(
                source,
                updater.LEGACY_FULL_MESH_START_GCODE,
            )
            updater.update_project(
                source,
                destination,
                timelapse_per_layer=True,
                use_printer_retraction=True,
                retract_infill_travels=True,
            )
            with zipfile.ZipFile(destination) as archive:
                settings = json.loads(archive.read(updater.PROJECT_SETTINGS))
                self.assertEqual(archive.read("3D/3dmodel.model"), model)
        self.assertEqual(settings["machine_start_gcode"], updater.RUNTIME_START_GCODE)
        self.assertEqual(settings["machine_end_gcode"], updater.RUNTIME_END_GCODE)
        self.assertIn("TIMELAPSE_TAKE_FRAME", settings["before_layer_change_gcode"])
        for key, value in updater.RUNTIME_REQUIRED_SETTINGS.items():
            self.assertEqual(settings[key], value)
        self.assertEqual(settings["filament_retraction_length"], ["nil"])
        self.assertEqual(settings["reduce_infill_retraction"], "0")
        for key, value in updater.RUNTIME_PROJECT_SETTINGS.items():
            self.assertEqual(settings[key], [value])

    def test_stages_named_profiles_without_unverified_pressure_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, updater.RUNTIME_START_GCODE)
            updater.update_project(
                source,
                destination,
                filament_profile_id="T300 PLA - CALIBRATION REQUIRED",
                process_profile_id="T300 Figure - REVIEW ONLY",
                calibration_required=True,
            )
            with zipfile.ZipFile(destination) as archive:
                settings = json.loads(archive.read(updater.PROJECT_SETTINGS))
        self.assertEqual(
            settings["filament_settings_id"],
            ["T300 PLA - CALIBRATION REQUIRED"],
        )
        self.assertEqual(settings["print_settings_id"], "T300 Figure - REVIEW ONLY")
        self.assertEqual(settings["enable_pressure_advance"], ["0"])
        self.assertEqual(settings["adaptive_pressure_advance"], ["0"])
        self.assertEqual(settings["pressure_advance"], ["0"])

    def test_rejects_unrecognized_custom_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, "G28\nG1 E25\n")
            with self.assertRaisesRegex(updater.ProjectError, "unrecognized"):
                updater.update_project(source, destination)

    def test_migrates_exact_legacy_comgrow_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, updater.COMGROW_FACTORY_START_GCODE)
            updater.update_project(source, destination)
            with zipfile.ZipFile(destination) as archive:
                settings = json.loads(archive.read(updater.PROJECT_SETTINGS))
        self.assertEqual(settings["machine_start_gcode"], updater.RUNTIME_START_GCODE)

    def test_rejects_near_match_to_legacy_comgrow_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            changed = updater.COMGROW_FACTORY_START_GCODE.replace("E30", "E29", 1)
            self.make_project(source, changed)
            with self.assertRaisesRegex(updater.ProjectError, "unrecognized"):
                updater.update_project(source, destination)

    def test_rejects_unknown_legacy_mesh_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            changed = updater.LEGACY_FULL_MESH_START_GCODE.replace(
                "MESH=FULL", "MESH=UNREVIEWED"
            )
            self.make_project(source, changed)
            with self.assertRaisesRegex(updater.ProjectError, "unrecognized"):
                updater.update_project(source, destination)

    def test_rejects_unrecognized_custom_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, updater.RUNTIME_START_GCODE)
            with zipfile.ZipFile(source, "r") as archive:
                members = {item.filename: archive.read(item) for item in archive.infolist()}
            settings = json.loads(members[updater.PROJECT_SETTINGS])
            settings["machine_end_gcode"] = "M84\n"
            with zipfile.ZipFile(source, "w") as archive:
                for name, content in members.items():
                    archive.writestr(
                        name,
                        json.dumps(settings).encode("utf-8")
                        if name == updater.PROJECT_SETTINGS
                        else content,
                    )
            with self.assertRaisesRegex(updater.ProjectError, "machine end"):
                updater.update_project(source, destination)

    def test_rejects_sequential_by_object_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, updater.RUNTIME_START_GCODE)
            with zipfile.ZipFile(source, "r") as archive:
                members = {item.filename: archive.read(item) for item in archive.infolist()}
            settings = json.loads(members[updater.PROJECT_SETTINGS])
            settings["print_sequence"] = "by object"
            with zipfile.ZipFile(source, "w") as archive:
                for name, content in members.items():
                    archive.writestr(
                        name,
                        json.dumps(settings).encode("utf-8")
                        if name == updater.PROJECT_SETTINGS
                        else content,
                    )
            with self.assertRaisesRegex(updater.ProjectError, "by-layer"):
                updater.update_project(source, destination)

    def test_failed_output_validation_publishes_no_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.3mf"
            destination = root / "destination.3mf"
            self.make_project(source, updater.RUNTIME_START_GCODE)
            with mock.patch.object(
                updater.zipfile.ZipFile, "testzip", return_value="broken-member"
            ):
                with self.assertRaisesRegex(updater.ProjectError, "integrity"):
                    updater.update_project(source, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".destination.3mf.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

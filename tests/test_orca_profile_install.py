from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "install-orca-runtime-profile.py"
SPEC = importlib.util.spec_from_file_location("install_orca_runtime_profile", MODULE_PATH)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class OrcaProfileInstallTests(unittest.TestCase):
    def test_profile_source_contains_required_runtime_contract(self):
        profile = installer.load_profile(installer.DEFAULT_PROFILE)
        self.assertEqual(profile["name"], installer.PROFILE_NAME)
        self.assertEqual(profile["gcode_flavor"], "klipper")
        self.assertEqual(profile["enable_power_loss_recovery"], "printer_configuration")
        self.assertEqual(profile["print_sequence"], "by layer")
        self.assertEqual(profile["gcode_label_objects"], "1")
        self.assertEqual(profile["exclude_object"], "1")
        self.assertEqual(profile["z_hop"], ["0"])
        self.assertEqual(profile["retract_restart_extra"], ["0"])
        self.assertEqual(
            profile["machine_start_gcode"],
            "START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] "
            "EXTRUDER_TEMP=[nozzle_temperature_initial_layer]\n",
        )
        self.assertEqual(profile["machine_end_gcode"], "END_PRINT\n")

    def test_dry_run_does_not_require_existing_orca_config(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = installer.install_profile(
                config_root=Path(directory) / "missing",
                profile_path=installer.DEFAULT_PROFILE,
                apply=False,
            )
        self.assertEqual(len(actions), 9)
        self.assertIn(installer.PROFILE_NAME, actions[-1])

    def test_apply_installs_machine_preset_and_updates_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config_root = Path(directory) / "OrcaSlicer"
            config_root.mkdir()
            config_file = config_root / "OrcaSlicer.conf"
            config_file.write_text(
                json.dumps({"presets": {"machine": "Default Printer"}}),
                encoding="utf-8",
            )
            installer.install_profile(
                config_root=config_root,
                profile_path=installer.DEFAULT_PROFILE,
                apply=True,
            )
            target = config_root / "user" / "default" / "machine" / f"{installer.PROFILE_NAME}.json"
            self.assertTrue(target.is_file())
            written_profile = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written_profile["name"], installer.PROFILE_NAME)
            self.assertEqual(
                len(list((config_root / "user" / "default").glob("*/*.json"))),
                8,
            )
            written_config = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(written_config["presets"]["machine"], installer.PROFILE_NAME)
            self.assertEqual(len(list(config_root.glob("OrcaSlicer.conf.bak-*"))), 1)

    def test_bundle_rejects_enabled_or_adaptive_pressure_advance(self):
        source = installer.load_bundled_profile(
            installer.REPO_ROOT
            / "orcaslicer/filament/T300 ELEGOO PLA Orange - CALIBRATION REQUIRED.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.json"
            source["enable_pressure_advance"] = ["1"]
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "pressure_advance"):
                installer.load_bundled_profile(path)

    def test_windows_default_uses_appdata(self):
        with mock.patch.object(installer.sys, "platform", "win32"):
            with mock.patch.dict(installer.os.environ, {"APPDATA": "C:/Users/A/Roaming"}):
                self.assertEqual(
                    installer.default_config_root(),
                    Path("C:/Users/A/Roaming") / "OrcaSlicer",
                )

    def test_linux_default_honors_xdg_config_home(self):
        with mock.patch.object(installer.sys, "platform", "linux"):
            with mock.patch.dict(installer.os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
                self.assertEqual(
                    installer.default_config_root(),
                    Path("/tmp/xdg") / "OrcaSlicer",
                )


if __name__ == "__main__":
    unittest.main()

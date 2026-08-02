from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
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


class MacroReadTests(unittest.TestCase):
    CONTENT = b"[gcode_macro T300_TEST]\ngcode:\n  RESPOND MSG=ok\n"

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

    def test_backup_refuses_non_directory_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup"
            target.write_text("occupied", encoding="utf-8")
            with self.assertRaises(t300ctl.T300Error):
                t300ctl.make_backup(self.FakeClient(), target)


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

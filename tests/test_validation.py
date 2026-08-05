from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from t300_mainline.validation import (
    review_host_network_boundary,
    review_klipper_lifecycle,
    review_operator_ui,
    review_systemd_units,
    scan_publishable_tree,
)


ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    MAINSAIL_CANCEL = """[gcode_macro CANCEL_PRINT]
gcode:
  _CLIENT_RETRACT LENGTH={retract}
  TURN_OFF_HEATERS
  M106 S0
  {client.user_cancel_macro|default("")}
  CANCEL_PRINT_BASE
"""

    def lifecycle_fixture(self, root: Path) -> Path:
        config = root / "etc/t300/klipper"
        maintenance = root / "etc/t300/maintenance"
        (config / "vendor/mainsail").mkdir(parents=True)
        (config / "vendor/kamp").mkdir(parents=True)
        maintenance.mkdir(parents=True)
        for name in (
            "lifecycle.cfg",
            "machine.cfg",
            "printer.cfg",
            "safety.cfg",
            "timelapse.cfg",
        ):
            shutil.copy2(ROOT / "mainline/config/production" / name, config / name)
        shutil.copy2(
            ROOT / "mainline/config/maintenance/printer.cfg",
            maintenance / "printer.cfg",
        )
        (config / "vendor/mainsail/client.cfg").write_text(
            self.MAINSAIL_CANCEL, encoding="utf-8"
        )
        (config / "vendor/kamp/Line_Purge.cfg").write_text(
            """[gcode_macro LINE_PURGE]
gcode:
  {% set breakaway_distance = 10.0 %}
  {% set max_x_start = printer.toolhead.axis_maximum.x - purge_amount - breakaway_distance %}
  {% set max_y_start = printer.toolhead.axis_maximum.y - purge_amount - breakaway_distance %}
  {% set has_front_lane = purge_y_min >= purge_margin and max_x_start >= 0 %}
  {% set has_left_lane = purge_x_min >= purge_margin and max_y_start >= 0 %}
  {% set purge_on_x = has_front_lane %}
  {% if not has_front_lane and not has_left_lane %}
    {action_raise_error("objects leave no bounded front or left purge lane")}
  {% endif %}
  SAVE_GCODE_STATE NAME=Prepurge_State
  G0 X{purge_x_center + purge_amount + breakaway_distance}
  G0 Y{purge_y_center + purge_amount + breakaway_distance}
""",
            encoding="utf-8",
        )
        return config

    def host_fixture(self, root: Path) -> Path:
        host = ROOT / "mainline/config/host"
        destinations = {
            "moonraker": root / "etc/t300/moonraker/moonraker.conf",
            "nginx": root / "etc/t300/nginx/nginx.conf",
            "crowsnest": root / "etc/t300/crowsnest/crowsnest.conf",
            "klipperscreen": root / "etc/t300/klipperscreen/KlipperScreen.conf",
        }
        for path in destinations.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        destinations["moonraker"].write_text(
            (host / "moonraker.conf.in")
            .read_text(encoding="utf-8")
            .replace("@PRINTER_HOSTNAME@", "t300"),
            encoding="utf-8",
        )
        destinations["nginx"].write_text(
            (host / "nginx.conf.in")
            .read_text(encoding="utf-8")
            .replace("@TRUSTED_LAPTOP_CIDR@", "10.42.42.0/24"),
            encoding="utf-8",
        )
        shutil.copy2(host / "crowsnest.conf", destinations["crowsnest"])
        shutil.copy2(host / "KlipperScreen.conf", destinations["klipperscreen"])
        return root

    def operator_ui_fixture(self, root: Path) -> Path:
        self.host_fixture(root)
        defaults = root / "etc/t300/mainsail/default.json"
        defaults.parent.mkdir(parents=True)
        shutil.copy2(
            ROOT / "mainline/config/host/mainsail-default.json", defaults
        )
        unit = root / "etc/systemd/system/mainsail.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "mainline/systemd/mainsail.service", unit)
        web = root / "opt/t300/www/mainsail"
        (web / "assets").mkdir(parents=True)
        (web / ".version").write_text("v2.18.2\n", encoding="ascii")
        (web / "index.html").write_text(
            '<script type="module" src="/assets/index.js"></script>'
            '<link rel="stylesheet" href="/assets/index.css">',
            encoding="utf-8",
        )
        (web / "assets/index.js").write_text("compiled\n", encoding="ascii")
        (web / "assets/index.css").write_text("body{}\n", encoding="ascii")
        return root

    def test_current_host_configs_pass_network_boundary_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.host_fixture(Path(directory))
            result = review_host_network_boundary(root)
            self.assertTrue(result["passed"], result["failures"])

    def test_current_operator_interfaces_pass_constrained_ui_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            result = review_operator_ui(root)
            self.assertTrue(result["passed"], result["failures"])

    def test_operator_ui_review_rejects_estop_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            screen = root / "etc/t300/klipperscreen/KlipperScreen.conf"
            screen.write_text(
                screen.read_text(encoding="utf-8").replace(
                    "confirm_estop: False", "confirm_estop: True"
                ),
                encoding="utf-8",
            )
            result = review_operator_ui(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("confirm_estop" in item for item in result["failures"]))

    def test_operator_ui_review_rejects_misleading_plate_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            screen = root / "etc/t300/klipperscreen/KlipperScreen.conf"
            screen.write_text(
                screen.read_text(encoding="utf-8").replace(
                    "name: Clean & Rearm Plate", "name: Plate Ready"
                ),
                encoding="utf-8",
            )
            result = review_operator_ui(root)
            self.assertFalse(result["passed"])
            self.assertTrue(
                any("phrased as an action" in item for item in result["failures"])
            )

    def test_operator_ui_review_rejects_wrong_status_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            screen = root / "etc/t300/klipperscreen/KlipperScreen.conf"
            screen.write_text(
                screen.read_text(encoding="utf-8").replace(
                    '{"script":"T_STATUS"}',
                    '{"script":"STATUS"}',
                    1,
                ),
                encoding="utf-8",
            )
            result = review_operator_ui(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("T_STATUS" in item for item in result["failures"]))

    def test_operator_ui_review_rejects_raw_touch_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            defaults = root / "etc/t300/mainsail/default.json"
            value = json.loads(defaults.read_text(encoding="utf-8"))
            panel = next(
                item
                for item in value["dashboard"]["mobileLayout"]
                if item["name"] == "toolhead-control"
            )
            panel["visible"] = True
            defaults.write_text(json.dumps(value), encoding="utf-8")
            result = review_operator_ui(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("raw controls" in item for item in result["failures"]))

    def test_operator_ui_review_rejects_development_mainsail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.operator_ui_fixture(Path(directory))
            index = root / "opt/t300/www/mainsail/index.html"
            index.write_text(
                '<script type="module" src="/src/main.ts"></script>',
                encoding="utf-8",
            )
            result = review_operator_ui(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("compiled" in item for item in result["failures"]))

    def test_host_review_rejects_remotely_bound_moonraker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.host_fixture(Path(directory))
            moonraker = root / "etc/t300/moonraker/moonraker.conf"
            moonraker.write_text(
                moonraker.read_text(encoding="utf-8").replace(
                    "host: 127.0.0.1", "host: 0.0.0.0"
                ),
                encoding="utf-8",
            )
            result = review_host_network_boundary(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("Moonraker" in item for item in result["failures"]))

    def test_host_review_rejects_broad_nginx_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.host_fixture(Path(directory))
            nginx = root / "etc/t300/nginx/nginx.conf"
            nginx.write_text(
                nginx.read_text(encoding="utf-8").replace(
                    "allow 10.42.42.0/24;", "allow 10.0.0.0/8;"
                ),
                encoding="utf-8",
            )
            result = review_host_network_boundary(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("narrow" in item for item in result["failures"]))

    def test_current_lifecycle_passes_cross_file_safety_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.lifecycle_fixture(root)
            result = review_klipper_lifecycle(root)
            self.assertTrue(result["passed"], result["failures"])

    def test_lifecycle_review_rejects_pre_shutdown_cancel_retraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            lifecycle = config / "lifecycle.cfg"
            lifecycle.write_text(
                lifecycle.read_text(encoding="utf-8").replace(
                    "variable_cancel_retract: 0.0", "variable_cancel_retract: 1.0"
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("retraction" in item for item in result["failures"]))

    def test_lifecycle_review_rejects_client_hook_before_heater_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            client = config / "vendor/mainsail/client.cfg"
            client.write_text(
                self.MAINSAIL_CANCEL.replace(
                    "  TURN_OFF_HEATERS\n  M106 S0\n  {client.user_cancel_macro",
                    "  {client.user_cancel_macro",
                )
                .replace(
                    '|default("")}\n  CANCEL_PRINT_BASE',
                    '|default("")}\n  TURN_OFF_HEATERS\n  CANCEL_PRINT_BASE',
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("shuts heaters" in item for item in result["failures"]))

    def test_lifecycle_review_requires_cancel_pressure_advance_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            lifecycle = config / "lifecycle.cfg"
            lifecycle.write_text(
                lifecycle.read_text(encoding="utf-8").replace(
                    "  SET_PRESSURE_ADVANCE ADVANCE=0\n  _T_SAFE_PARK Z_MIN=200",
                    "  _T_SAFE_PARK Z_MIN=200",
                    1,
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(
                any("reset pressure advance" in item for item in result["failures"])
            )

    def test_lifecycle_review_requires_clearance_before_xy_park(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            lifecycle = config / "lifecycle.cfg"
            lifecycle.write_text(
                lifecycle.read_text(encoding="utf-8").replace(
                    "target_z - current_z >= 2.0", "target_z >= current_z"
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(
                any("target_z - current_z" in item for item in result["failures"])
            )

    def test_lifecycle_review_rejects_unbounded_kamp_breakaway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            purge = config / "vendor/kamp/Line_Purge.cfg"
            purge.write_text(
                purge.read_text(encoding="utf-8")
                .replace(
                    "purge_x_center + purge_amount + breakaway_distance",
                    "purge_x_center + purge_amount + 10",
                )
                .replace(
                    "purge_y_center + purge_amount + breakaway_distance",
                    "purge_y_center + purge_amount + 10",
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("breakaway" in item for item in result["failures"]))

    def test_lifecycle_review_rejects_motion_capable_timelapse_macro(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            timelapse = config / "timelapse.cfg"
            timelapse.write_text(
                timelapse.read_text(encoding="utf-8") + "  G0 X10\n",
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("timelapse" in item for item in result["failures"]))

    def test_lifecycle_review_keeps_private_workflow_out_of_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.lifecycle_fixture(root)
            printer = config / "printer.cfg"
            printer.write_text(
                printer.read_text(encoding="utf-8").replace(
                    "[include kamp-settings.cfg]",
                    "[include private/*.cfg]\n[include kamp-settings.cfg]",
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("private" in item for item in result["failures"]))

    def test_lifecycle_review_requires_private_workflow_in_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.lifecycle_fixture(root)
            maintenance = root / "etc/t300/maintenance/printer.cfg"
            maintenance.write_text(
                maintenance.read_text(encoding="utf-8").replace(
                    "[include ../klipper/private/*.cfg]", ""
                ),
                encoding="utf-8",
            )
            result = review_klipper_lifecycle(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("private" in item for item in result["failures"]))

    def test_current_staged_unit_templates_pass_static_boundary_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "etc/systemd/system"
            shutil.copytree(ROOT / "mainline/systemd", destination)
            serial = "/dev/serial/by-id/usb-Klipper_stm32f401xc_TEST-if00"
            dropin = (
                "[Unit]\nConditionPathExists=%s\n\n"
                "[Service]\nDevicePolicy=closed\nDeviceAllow=%s rw\n"
                % (serial, serial)
            )
            for unit in ("klipper.service", "klipper-maintenance.service"):
                path = destination / (unit + ".d/10-mcu-device.conf")
                path.parent.mkdir()
                path.write_text(dropin, encoding="utf-8")
            data_mount = (
                ROOT / "mainline/config/templates/t300-data.mount.in"
            ).read_text(encoding="utf-8")
            (destination / r"mnt-t300\x2ddata.mount").write_text(
                data_mount.replace("@DATA_USB_UUID@", "C66C-ADD5"),
                encoding="utf-8",
            )
            ssh_config = root / "etc/ssh/sshd_config.d/60-t300.conf"
            ssh_config.parent.mkdir(parents=True)
            text = (ROOT / "mainline/config/templates/sshd-t300.conf.in").read_text()
            ssh_config.write_text(
                text.replace("@TRUSTED_LAPTOP_CIDR@", "10.42.42.0/24"),
                encoding="utf-8",
            )
            authorized = root / "etc/t300/deploy_authorized_keys"
            authorized.parent.mkdir(parents=True)
            authorized.write_text(
                "# No deployment key was staged; restricted transport cannot be armed.\n",
                encoding="ascii",
            )
            result = review_systemd_units(root)
            self.assertTrue(result["passed"], result["failures"])

    def test_xorg_cannot_isolate_the_socket_needed_by_klipperscreen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "etc/systemd/system"
            shutil.copytree(ROOT / "mainline/systemd", destination)
            xorg = destination / "t300-xorg.service"
            xorg.write_text(
                xorg.read_text(encoding="utf-8").replace(
                    "NoNewPrivileges=yes\n",
                    "NoNewPrivileges=yes\nPrivateNetwork=yes\n",
                ),
                encoding="utf-8",
            )
            serial = "/dev/serial/by-id/usb-Klipper_stm32f401xc_TEST-if00"
            dropin = (
                "[Unit]\nConditionPathExists=%s\n\n"
                "[Service]\nDevicePolicy=closed\nDeviceAllow=%s rw\n"
                % (serial, serial)
            )
            for unit in ("klipper.service", "klipper-maintenance.service"):
                path = destination / (unit + ".d/10-mcu-device.conf")
                path.parent.mkdir()
                path.write_text(dropin, encoding="utf-8")
            data_mount = (
                ROOT / "mainline/config/templates/t300-data.mount.in"
            ).read_text(encoding="utf-8")
            (destination / r"mnt-t300\x2ddata.mount").write_text(
                data_mount.replace("@DATA_USB_UUID@", "C66C-ADD5"),
                encoding="utf-8",
            )
            ssh_config = root / "etc/ssh/sshd_config.d/60-t300.conf"
            ssh_config.parent.mkdir(parents=True)
            ssh_config.write_text(
                (ROOT / "mainline/config/templates/sshd-t300.conf.in")
                .read_text(encoding="utf-8")
                .replace("@TRUSTED_LAPTOP_CIDR@", "10.42.42.0/24"),
                encoding="utf-8",
            )
            authorized = root / "etc/t300/deploy_authorized_keys"
            authorized.parent.mkdir(parents=True)
            authorized.write_text(
                "# No deployment key was staged; restricted transport cannot be armed.\n",
                encoding="ascii",
            )
            result = review_systemd_units(root)
            self.assertFalse(result["passed"])
            self.assertTrue(
                any("Unix-socket namespace" in item for item in result["failures"])
            )

    def test_host_service_cannot_pull_in_moonraker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "etc/systemd/system"
            shutil.copytree(ROOT / "mainline/systemd", destination)
            screen = destination / "klipperscreen.service"
            screen.write_text(
                screen.read_text(encoding="utf-8").replace(
                    "Requires=t300-xorg.service\n",
                    "Requires=t300-xorg.service\nWants=moonraker.service\n",
                ),
                encoding="utf-8",
            )
            serial = "/dev/serial/by-id/usb-Klipper_stm32f401xc_TEST-if00"
            dropin = (
                "[Unit]\nConditionPathExists=%s\n\n"
                "[Service]\nDevicePolicy=closed\nDeviceAllow=%s rw\n"
                % (serial, serial)
            )
            for unit in ("klipper.service", "klipper-maintenance.service"):
                path = destination / (unit + ".d/10-mcu-device.conf")
                path.parent.mkdir()
                path.write_text(dropin, encoding="utf-8")
            data_mount = (
                ROOT / "mainline/config/templates/t300-data.mount.in"
            ).read_text(encoding="utf-8")
            (destination / r"mnt-t300\x2ddata.mount").write_text(
                data_mount.replace("@DATA_USB_UUID@", "C66C-ADD5"),
                encoding="utf-8",
            )
            ssh_config = root / "etc/ssh/sshd_config.d/60-t300.conf"
            ssh_config.parent.mkdir(parents=True)
            ssh_config.write_text(
                (ROOT / "mainline/config/templates/sshd-t300.conf.in")
                .read_text(encoding="utf-8")
                .replace("@TRUSTED_LAPTOP_CIDR@", "10.42.42.0/24"),
                encoding="utf-8",
            )
            authorized = root / "etc/t300/deploy_authorized_keys"
            authorized.parent.mkdir(parents=True)
            authorized.write_text(
                "# No deployment key was staged; restricted transport cannot be armed.\n",
                encoding="ascii",
            )
            result = review_systemd_units(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any("pulls in" in item for item in result["failures"]))

    def test_secret_scan_rejects_private_macro_and_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public.py").write_text("print('ok')\n", encoding="utf-8")
            self.assertTrue(scan_publishable_tree(root)["passed"])
            (root / "macro_z_tilt_via_knob.cfg").write_text("private", encoding="utf-8")
            (root / "package.zip").write_bytes(b"zip")
            result = scan_publishable_tree(root)
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["failures"]), 2)


if __name__ == "__main__":
    unittest.main()

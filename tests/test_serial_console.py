from __future__ import annotations

import termios
import unittest
from unittest import mock

from t300_mainline.serial_console import (
    SerialConsoleError,
    _allowed_device_name,
    _serial_attributes,
    list_serial_devices,
    serial_device_access,
    validate_serial_device,
)


class SerialConsoleTests(unittest.TestCase):
    def test_only_expected_linux_usb_serial_names_are_accepted(self):
        for value in (
            "/dev/serial/by-id/usb-Rockchip_TEST-if00",
            "/dev/ttyACM0",
            "/dev/ttyUSB12",
        ):
            self.assertTrue(_allowed_device_name(value))
        for value in ("/dev/ttyS0", "/dev/mem", "relative", "/dev/ttyUSB0/extra"):
            self.assertFalse(_allowed_device_name(value))
            with self.assertRaises(SerialConsoleError):
                validate_serial_device(value)

    def test_serial_attributes_are_1500000_8n1_without_flow_control(self):
        current = [1, 2, 3, 4, 5, 6, [b"\0"] * 32]
        value = _serial_attributes(current)
        self.assertEqual(value[0], 0)
        self.assertEqual(value[1], 0)
        self.assertEqual(value[2], termios.CLOCAL | termios.CREAD | termios.CS8)
        self.assertEqual(value[3], 0)
        self.assertEqual(value[4], termios.B1500000)
        self.assertEqual(value[5], termios.B1500000)
        self.assertEqual(value[6][termios.VMIN], 1)
        self.assertEqual(value[6][termios.VTIME], 0)

    def test_missing_serial_device_reports_unavailable(self):
        self.assertEqual(serial_device_access("/dev/ttyUSB999999"), "unavailable")

    def test_listing_filters_glob_results_through_the_same_name_policy(self):
        with mock.patch(
            "t300_mainline.serial_console.glob.glob",
            side_effect=(
                ["/dev/serial/by-id/usb-TEST"],
                ["/dev/ttyACM0", "/dev/ttyACM0unexpected"],
                ["/dev/ttyUSB2"],
            ),
        ):
            self.assertEqual(
                list_serial_devices(),
                ["/dev/serial/by-id/usb-TEST", "/dev/ttyACM0", "/dev/ttyUSB2"],
            )


if __name__ == "__main__":
    unittest.main()

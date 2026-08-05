from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from t300_mainline.gcode_policy import GCodePolicy, scan_gcode


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "mainline/policy/gcode-policy.json"
CONTRACT_PATH = ROOT / "orcaslicer/orca-v2.4.2-contract.json"


def calibration_gcode(commands: str) -> str:
    return (
        "EXCLUDE_OBJECT_DEFINE NAME=calibration CENTER=35,35 "
        "POLYGON=[[30,30],[40,30],[40,40],[30,40]]\n"
        "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220\n"
        "G21\n"
        "G90\n"
        "M82\n"
        "G92 E0\n"
        "G1 X10 Y10 Z0.2 F3000\n"
        "G1 X20 Y10 E0.2 F1200\n"
        f"{commands}"
        "END_PRINT\n"
    )


class OrcaV242CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = GCodePolicy.from_json(POLICY_PATH)
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def scan(self, commands: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orca-calibration.gcode"
            path.write_text(calibration_gcode(commands), encoding="ascii")
            return scan_gcode(path, self.policy, POLICY_PATH)

    def test_contract_is_pinned_to_exact_orca_release(self):
        self.assertEqual(self.contract["orcaslicer_version"], "2.4.2")
        self.assertEqual(
            self.contract["source_commit"],
            "8500fcdccaa10b5099ac20d252af3a7c560046f1",
        )
        self.assertEqual(
            self.contract["calibration_order"],
            [
                "temperature",
                "maximum_volumetric_speed",
                "pressure_advance",
                "flow_ratio",
                "retraction",
            ],
        )

    def test_requested_orca_calibration_command_forms_are_admitted(self):
        cases = {
            "temperature": "M104 S215\nM109 S215\n",
            "maximum_volumetric_speed": "G1 X30 Y10 E0.4 F12000\n",
            "pressure_advance": (
                "SET_PRESSURE_ADVANCE ADVANCE=0\n"
                "SET_PRESSURE_ADVANCE ADVANCE=0.02\n"
                "SET_PRESSURE_ADVANCE ADVANCE=0.2\n"
            ),
            "flow_ratio": "G1 X30 Y10 E0.45 F1200\n",
            "retraction": "M83\nG1 E-0.8 F1800\nG1 X30 Y10 F12000\nG1 E0.8 F1800\n",
        }
        for calibration, commands in cases.items():
            with self.subTest(calibration=calibration):
                report = self.scan(commands)
                self.assertTrue(report.accepted, report.to_json())

    def test_unsafe_calibration_variants_still_fail_closed(self):
        cases = {
            "temperature_ceiling": "M104 S301\n",
            "motion_axis_escape": "G1 X303 Y10 E0.4 F12000\n",
            "pressure_advance_ceiling": "SET_PRESSURE_ADVANCE ADVANCE=0.2001\n",
            "pressure_advance_smooth_time": (
                "SET_PRESSURE_ADVANCE ADVANCE=0.02 SMOOTH_TIME=0.04\n"
            ),
            "pressure_advance_alternate_extruder": (
                "SET_PRESSURE_ADVANCE ADVANCE=0.02 EXTRUDER=extruder1\n"
            ),
            "tuning_tower": (
                "TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE "
                "PARAMETER=ADVANCE START=0 FACTOR=0.005\n"
            ),
            "flow_override_increase": "M221 S101\n",
            "unmatched_stationary_recovery": "M83\nG1 E0.01\n",
        }
        for calibration, commands in cases.items():
            with self.subTest(calibration=calibration):
                report = self.scan(commands)
                self.assertFalse(report.accepted, report.to_json())


if __name__ == "__main__":
    unittest.main()

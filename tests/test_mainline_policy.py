from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from t300_mainline.admission_daemon import AdmissionDaemon
from t300_mainline.gcode_policy import (
    GCodePolicy,
    PolicyError,
    admit_gcode,
    scan_gcode,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "mainline/policy/gcode-policy.json"


def valid_gcode(extra: str = "") -> str:
    return """EXCLUDE_OBJECT_DEFINE NAME=part CENTER=35,35 POLYGON=[[30,30],[40,30],[40,40],[30,40]]
START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220
G21
G90
M82
G92 E0
G1 X10 Y10 Z0.2 F3000
G1 X20 Y10 E0.2 F1200
M73 P10 R5
TIMELAPSE_TAKE_FRAME
%sEND_PRINT
""" % (extra,)


class GCodePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = GCodePolicy.from_json(POLICY_PATH)

    def scan(self, text: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "print.gcode"
        path.write_text(text, encoding="ascii")
        return scan_gcode(path, self.policy, POLICY_PATH)

    def test_accepts_minimal_orca_lifecycle(self):
        report = self.scan(valid_gcode())
        self.assertTrue(report.accepted, report.to_json())
        self.assertEqual(report.object_count, 1)

    def test_requires_object_before_start(self):
        report = self.scan("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220\nEND_PRINT\n")
        self.assertFalse(report.accepted)
        self.assertIn("object definitions", report.findings[0].message)

    def test_rejects_objects_without_a_safe_kamp_purge_lane(self):
        unsafe = valid_gcode().replace(
            "CENTER=35,35 POLYGON=[[30,30],[40,30],[40,40],[30,40]]",
            "CENTER=10,10 POLYGON=[[5,5],[15,5],[15,15],[5,15]]",
        )
        report = self.scan(unsafe)
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(any("purge lane" in item.message for item in report.findings))

    def test_rejects_motor_disable_and_debug_commands(self):
        for command in ("M84", "FORCE_MOVE STEPPER=stepper_x DISTANCE=1"):
            with self.subTest(command=command):
                report = self.scan(valid_gcode(command + "\n"))
                self.assertFalse(report.accepted)

    def test_rejects_limit_increases(self):
        for command in (
            "M204 S12001",
            "M220 S101",
            "M221 S101",
            "SET_VELOCITY_LIMIT ACCEL=12001",
            "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.5",
            "SET_TMC_CURRENT STEPPER=stepper_x CURRENT=1.2",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.scan(valid_gcode(command + "\n")).accepted)

    def test_rejects_malformed_nonpositive_limits(self):
        for command in (
            "SET_VELOCITY_LIMIT VELOCITY=0",
            "SET_VELOCITY_LIMIT ACCEL=0 ACCEL_TO_DECEL=1",
            "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=1",
            "M204 S=0",
            "SET_TMC_CURRENT STEPPER=stepper_x CURRENT=-0.1",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.scan(valid_gcode(command + "\n")).accepted)

    def test_rejects_temperature_and_motion_escape(self):
        for command in (
            "M104 S301",
            "M140 S101",
            "G1 X303",
            "G4 S61",
            "G92 Z100",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.scan(valid_gcode(command + "\n")).accepted)

    def test_allows_only_extruder_g92_reset(self):
        self.assertTrue(self.scan(valid_gcode()).accepted)
        report = self.scan(valid_gcode().replace("G92 E0", "G92 X0"))
        self.assertFalse(report.accepted)
        self.assertTrue(any("G92 may reset only" in item.message for item in report.findings))

    def test_rejects_stationary_purge_but_allows_retraction_recovery(self):
        self.assertFalse(self.scan(valid_gcode("M83\nG1 E0.01\n")).accepted)
        self.assertTrue(
            self.scan(valid_gcode("M83\nG1 E-0.8\nG92 E0\nG1 E0.8\n")).accepted
        )

    def test_accepts_only_bounded_orca_rounding_reconciliation(self):
        orca_wipe = (
            "M83\n"
            "G1 E-.35\n"
            "G1 X20.1 Y10 E-.02704\n"
            "G1 X20.2 Y10 E-.02443\n"
            "G1 X20.3 Y10 E-.02378\n"
            "G1 X20.4 Y10 E-.07472\n"
            "G1 E.5\n"
        )
        self.assertTrue(self.scan(valid_gcode(orca_wipe)).accepted)
        self.assertFalse(self.scan(valid_gcode(orca_wipe + "G1 E.00001\n")).accepted)
        self.assertFalse(
            self.scan(valid_gcode("M83\nG1 E-.5\nG1 E.50011\n")).accepted
        )
        repeated_without_printing = (
            "M83\n"
            "G1 E-.49999\nG1 E.5\n"
            "G1 E-.49999\nG1 E.5\n"
        )
        self.assertFalse(self.scan(valid_gcode(repeated_without_printing)).accepted)

    def test_orca_rounding_accounting_scales_with_real_printing(self):
        cycles = ["M83\n"]
        for index in range(3000):
            x = 20 if index % 2 else 21
            cycles.extend(
                (
                    "G1 E-.35\n",
                    "G1 X20 Y10 E-.14999\n",
                    "G1 E.5\n",
                    f"G1 X{x} Y11 E1\n",
                )
            )
        report = self.scan(valid_gcode("".join(cycles)))
        self.assertTrue(report.accepted, report.to_json())

    def test_tiny_print_moves_cannot_farm_rounding_allowance(self):
        cycles = ["M83\n"]
        for index in range(20):
            x = 20 if index % 2 else 21
            cycles.extend(
                (
                    "G1 E-.49999\n",
                    "G1 E.5\n",
                    f"G1 X{x} Y11 E.00001\n",
                )
            )
        report = self.scan(valid_gcode("".join(cycles)))
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(
            any("retraction credit" in item.message for item in report.findings),
            report.to_json(),
        )

    def test_accepts_only_bounded_orca_zero_length_xy_rounding(self):
        rounded_path = (
            "M83\n"
            "G1 X20 Y10 E.00002\n"
            "G1 X21 Y10 E.01\n"
            "G1 X21 Y10 E.00002\n"
        )
        self.assertTrue(self.scan(valid_gcode(rounded_path)).accepted)
        for unsafe in (
            "M83\nG1 X20 Y10 E.00011\n",
            "M83\nG1 X20 E.00001\n",
            "M83\nG1 X20 Y10 E.00002\nG1 X20 Y10 E.00002\n",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertFalse(self.scan(valid_gcode(unsafe)).accepted)

    def test_retraction_credit_is_bounded_consumed_and_cleared_by_printing(self):
        for commands in (
            "M83\nG1 E-0.8\nG1 E0.81\n",
            "M83\nG1 E-0.8\nG1 E0.4\nG1 E0.41\n",
            "M83\nG1 E-0.8\nG1 X30 Y10 E0.2\nG1 E0.1\n",
        ):
            with self.subTest(commands=commands):
                report = self.scan(valid_gcode(commands))
                self.assertFalse(report.accepted, report.to_json())
                self.assertTrue(
                    any("retraction credit" in item.message for item in report.findings),
                    report.to_json(),
                )

    def test_accepts_bounded_orca_pressure_advance(self):
        commands = (
            "SET_PRESSURE_ADVANCE ADVANCE=0\n"
            "SET_PRESSURE_ADVANCE ADVANCE=0.02\n"
            "SET_PRESSURE_ADVANCE ADVANCE=0.2\n"
            "SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.02\n"
        )
        report = self.scan(valid_gcode(commands))
        self.assertTrue(report.accepted, report.to_json())

    def test_rejects_unbounded_or_extended_pressure_advance(self):
        for command in (
            "SET_PRESSURE_ADVANCE ADVANCE=-0.01",
            "SET_PRESSURE_ADVANCE ADVANCE=0.20001",
            "SET_PRESSURE_ADVANCE ADVANCE=nan",
            "SET_PRESSURE_ADVANCE",
            "SET_PRESSURE_ADVANCE ADVANCE=0.02 SMOOTH_TIME=0.04",
            "SET_PRESSURE_ADVANCE ADVANCE=0.02 EXTRUDER=extruder1",
            "TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0",
        ):
            with self.subTest(command=command):
                report = self.scan(valid_gcode(command + "\n"))
                self.assertFalse(report.accepted, report.to_json())

    def test_rejects_pressure_advance_before_start(self):
        report = self.scan(
            valid_gcode().replace(
                "START_PRINT",
                "SET_PRESSURE_ADVANCE ADVANCE=0.02\nSTART_PRINT",
            )
        )
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(
            any("before START_PRINT" in item.message for item in report.findings),
            report.to_json(),
        )

    def test_parser_matches_klipper_for_parentheses_exponents_and_stars(self):
        # Pinned Klipper does not treat parentheses as comments and tokenizes
        # E in an apparent exponent as an extrusion parameter.
        for command in (
            "M83\nG1 (E100\n",
            "M83\nG1 X20e100\n",
            "M83\nG1 X20 * E100\n",
        ):
            with self.subTest(command=command):
                report = self.scan(valid_gcode(command))
                self.assertFalse(report.accepted, report.to_json())

    def test_rejects_motion_or_heat_before_start_and_late_objects(self):
        for line in ("M104 S200\n", "G1 X10\n"):
            with self.subTest(line=line):
                report = self.scan(valid_gcode().replace("START_PRINT", line + "START_PRINT"))
                self.assertFalse(report.accepted, report.to_json())
                self.assertTrue(
                    any("before START_PRINT" in item.message for item in report.findings)
                )
        report = self.scan(
            valid_gcode().replace(
                "G21\n",
                "EXCLUDE_OBJECT_DEFINE NAME=late POLYGON=[[1,1],[2,1],[2,2]]\nG21\n",
            )
        )
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(any("all Orca object" in item.message for item in report.findings))

    def test_post_purge_position_must_be_learned_before_relative_or_extruding_travel(self):
        first_travel = "G1 X10 Y10 Z0.2 F3000\n"
        relative = valid_gcode().replace(first_travel, "G91\nG1 X1 F3000\n")
        report = self.scan(relative)
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(any("known absolute origin" in item.message for item in report.findings))

        extruding = valid_gcode().replace(
            first_travel + "G1 X20 Y10 E0.2 F1200\n",
            "G1 X20 Y10 Z0.2 E0.2 F1200\n",
        )
        report = self.scan(extruding)
        self.assertFalse(report.accepted, report.to_json())
        self.assertTrue(any("unknown post-purge" in item.message for item in report.findings))

    def test_z_only_positive_extrusion_and_travelling_retraction_use_e_only_limit(self):
        self.assertFalse(self.scan(valid_gcode("M83\nG1 Z1 E5.1\n")).accepted)
        self.assertFalse(self.scan(valid_gcode("M83\nG1 X30 E-101\n")).accepted)

    def test_policy_rejects_wrong_numeric_types_and_missing_stepper_ceilings(self):
        source = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        variants = []
        wrong_type = dict(source)
        wrong_type["max_velocity"] = "600"
        variants.append(wrong_type)
        bool_bound = json.loads(json.dumps(source))
        bool_bound["x"]["minimum"] = True
        variants.append(bool_bound)
        missing_current = json.loads(json.dumps(source))
        del missing_current["tmc_current_max"]["extruder"]
        variants.append(missing_current)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, value in enumerate(variants):
                with self.subTest(index=index):
                    path = root / f"{index}.json"
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(PolicyError):
                        GCodePolicy.from_json(path)

    def test_timelapse_frame_count_is_bounded(self):
        policy = replace(self.policy, max_timelapse_frames=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "print.gcode"
            path.write_text(
                valid_gcode("TIMELAPSE_TAKE_FRAME\nTIMELAPSE_TAKE_FRAME\n"),
                encoding="ascii",
            )
            report = scan_gcode(path, policy, POLICY_PATH)
        self.assertFalse(report.accepted, report.to_json())
        self.assertEqual(report.timelapse_frames, 3)
        self.assertTrue(any("timelapse-frame" in item.message for item in report.findings))

    def test_object_name_and_center_are_validated(self):
        for definition in (
            "EXCLUDE_OBJECT_DEFINE POLYGON=[[10,10],[20,10],[20,20]]",
            "EXCLUDE_OBJECT_DEFINE NAME=part CENTER=bad POLYGON=[[10,10],[20,10],[20,20]]",
            "EXCLUDE_OBJECT_DEFINE NAME=part CENTER=999,10 POLYGON=[[10,10],[20,10],[20,20]]",
        ):
            with self.subTest(definition=definition):
                report = self.scan(valid_gcode().replace(valid_gcode().splitlines()[0], definition))
                self.assertFalse(report.accepted, report.to_json())

    def test_rejects_commands_after_end(self):
        report = self.scan(valid_gcode() + "G1 X30\n")
        self.assertFalse(report.accepted)
        self.assertTrue(any("after END_PRINT" in item.message for item in report.findings))

    def test_approval_is_bound_to_path_hash_size_policy_and_protected_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcode_root = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            gcode_root.mkdir()
            approvals.mkdir()
            spool.mkdir()
            source = gcode_root / "test.gcode"
            source.write_text(valid_gcode(), encoding="ascii")
            report, target = admit_gcode(
                source, self.policy, POLICY_PATH, approvals, gcode_root, spool
            )
            self.assertIsNotNone(target)
            record = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(record["relative_path"], "test.gcode")
            self.assertEqual(record["sha256"], report.sha256)
            self.assertEqual(record["policy_sha256"], report.policy_sha256)
            protected = spool / record["spool_file"]
            self.assertEqual(protected.read_bytes(), source.read_bytes())
            self.assertEqual(protected.stat().st_mode & 0o777, 0o440)

    def test_identical_content_at_two_paths_gets_two_approvals_one_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            for path in (gcodes, approvals, spool):
                path.mkdir()
            for name in ("one.gcode", "two.gcode"):
                source = gcodes / name
                source.write_text(valid_gcode(), encoding="ascii")
                report, target = admit_gcode(
                    source, self.policy, POLICY_PATH, approvals, gcodes, spool
                )
                self.assertTrue(report.accepted)
                self.assertIsNotNone(target)
            self.assertEqual(len(list(approvals.glob("*.json"))), 2)
            self.assertEqual(len(list(spool.glob("*.gcode"))), 1)

    def test_scan_rejects_symlink_and_bounded_overlong_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gcode"
            source.write_text(valid_gcode(), encoding="ascii")
            link = root / "link.gcode"
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                scan_gcode(link, self.policy, POLICY_PATH)
            overlong = root / "long.gcode"
            overlong.write_bytes(b";" + b"x" * (self.policy.max_line_bytes * 2) + b"\n")
            report = scan_gcode(overlong, self.policy, POLICY_PATH)
            self.assertTrue(any("line exceeds" in item.message for item in report.findings))

    def test_finding_report_is_bounded_and_marks_truncation(self):
        policy = replace(self.policy, max_findings=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "print.gcode"
            path.write_text(valid_gcode("M84\n" * 20), encoding="ascii")
            report = scan_gcode(path, policy, POLICY_PATH)
        self.assertEqual(len(report.findings), 3)
        self.assertIn("omitted", report.findings[-1].message)

    def test_object_count_and_total_polygon_points_are_bounded(self):
        definitions = (
            "EXCLUDE_OBJECT_DEFINE NAME=a POLYGON=[[10,10],[20,10],[20,20],[10,20]]\n"
            "EXCLUDE_OBJECT_DEFINE NAME=b POLYGON=[[30,30],[40,30],[40,40],[30,40]]\n"
        )
        body = valid_gcode().split("\n", 1)[1]
        for policy, message in (
            (replace(self.policy, max_objects=1), "object-count"),
            (
                replace(
                    self.policy,
                    max_polygon_points=4,
                    max_total_object_points=4,
                ),
                "object-point",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "print.gcode"
                path.write_text(definitions + body, encoding="ascii")
                report = scan_gcode(path, policy, POLICY_PATH)
                self.assertFalse(report.accepted)
                self.assertTrue(
                    any(message in item.message for item in report.findings),
                    report.to_json(),
                )

    def test_spool_quota_and_system_reserve_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            for path in (gcodes, approvals, spool):
                path.mkdir()
            source = gcodes / "print.gcode"
            source.write_text(valid_gcode(), encoding="ascii")
            with self.assertRaisesRegex(PolicyError, "storage ceiling"):
                admit_gcode(
                    source,
                    replace(self.policy, max_spool_bytes=1),
                    POLICY_PATH,
                    approvals,
                    gcodes,
                    spool,
                )
            with self.assertRaisesRegex(PolicyError, "free-space reserve"):
                admit_gcode(
                    source,
                    replace(
                        self.policy,
                        max_spool_bytes=10**12,
                        min_system_free_bytes=10**18,
                    ),
                    POLICY_PATH,
                    approvals,
                    gcodes,
                    spool,
                )


class AdmissionDaemonTests(unittest.TestCase):
    @staticmethod
    def fixture(root: Path) -> tuple[Path, Path, Path, Path, AdmissionDaemon]:
        gcodes = root / "gcodes"
        approvals = root / "approvals"
        spool = root / "spool"
        rejected = root / "rejected"
        for path in (gcodes, approvals, spool, rejected):
            path.mkdir()
        return (
            gcodes,
            approvals,
            spool,
            rejected,
            AdmissionDaemon(gcodes, approvals, spool, rejected, POLICY_PATH),
        )

    def test_waits_for_stability_then_admits_and_records_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            rejected = root / "rejected"
            for path in (gcodes, approvals, spool, rejected):
                path.mkdir()
            good = gcodes / "good.gcode"
            bad = gcodes / "bad.gcode"
            good.write_text(valid_gcode(), encoding="ascii")
            bad.write_text(valid_gcode("M84\n"), encoding="ascii")
            daemon = AdmissionDaemon(gcodes, approvals, spool, rejected, POLICY_PATH)
            self.assertEqual(daemon.scan_once(), (0, 0))
            admitted, rejected_count = daemon.scan_once()
            self.assertEqual((admitted, rejected_count), (1, 1))
            self.assertEqual(len(list(approvals.glob("*.json"))), 1)
            self.assertEqual(len(list(spool.glob("*.gcode"))), 1)
            self.assertEqual(len(list(rejected.glob("*.json"))), 1)
            self.assertEqual(daemon.scan_once(), (0, 0))

    def test_startup_preflight_revokes_old_authorizations_and_partials(self):
        with tempfile.TemporaryDirectory() as directory:
            gcodes, approvals, spool, rejected, daemon = self.fixture(Path(directory))
            (approvals / "old.json").write_text("{}", encoding="ascii")
            (approvals / ".approval-partial").write_text("partial", encoding="ascii")
            (spool / ("a" * 64 + ".gcode")).write_text("old", encoding="ascii")
            (spool / ".gcode-snapshot-partial").write_text("partial", encoding="ascii")
            self.assertEqual(daemon.startup_preflight(), (0, 0))
            self.assertFalse(list(approvals.iterdir()))
            self.assertFalse(list(spool.iterdir()))
            self.assertFalse(list(rejected.iterdir()))

    def test_candidate_flood_revokes_every_approval_and_records_one_error(self):
        with tempfile.TemporaryDirectory() as directory:
            gcodes, approvals, spool, rejected, daemon = self.fixture(Path(directory))
            daemon.policy = replace(daemon.policy, max_candidate_files=2)
            (approvals / "old.json").write_text("{}", encoding="ascii")
            for index in range(3):
                (gcodes / ("%d.gcode" % index)).write_text(valid_gcode(), encoding="ascii")
            self.assertEqual(daemon.scan_once(), (0, 1))
            self.assertFalse(list(approvals.glob("*.json")))
            records = list(rejected.glob("*.json"))
            self.assertEqual(len(records), 1)
            payload = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["scope"], "gcode-root")

    def test_policy_exception_is_processed_once_and_revokes_old_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            gcodes, approvals, spool, rejected, daemon = self.fixture(Path(directory))
            source = gcodes / "print.gcode"
            source.write_text(valid_gcode(), encoding="ascii")
            daemon.scan_once()
            self.assertEqual(daemon.scan_once(), (1, 0))
            self.assertEqual(len(list(approvals.glob("*.json"))), 1)
            source.write_text(valid_gcode("; changed\n"), encoding="ascii")
            daemon.policy = replace(daemon.policy, max_file_bytes=1)
            self.assertEqual(daemon.scan_once(), (0, 0))
            self.assertEqual(daemon.scan_once(), (0, 1))
            self.assertEqual(daemon.scan_once(), (0, 0))
            self.assertFalse(list(approvals.glob("*.json")))
            self.assertEqual(len(list(rejected.glob("*.json"))), 1)

    def test_rejection_records_are_bounded_by_oldest_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            _gcodes, _approvals, _spool, rejected, daemon = self.fixture(Path(directory))
            daemon.policy = replace(daemon.policy, max_rejection_records=2)
            for index in range(5):
                path = rejected / ("%d.json" % index)
                path.write_text("{}", encoding="ascii")
                os.utime(path, ns=(index + 1, index + 1))
            daemon._prune_rejections()
            self.assertEqual(
                sorted(path.name for path in rejected.glob("*.json")),
                ["3.json", "4.json"],
            )

    def test_policy_change_revokes_approvals_and_requires_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            rejected = root / "rejected"
            for path in (gcodes, approvals, spool, rejected):
                path.mkdir()
            policy = root / "policy.json"
            policy.write_bytes(POLICY_PATH.read_bytes())
            source = gcodes / "print.gcode"
            source.write_text(valid_gcode(), encoding="ascii")
            daemon = AdmissionDaemon(gcodes, approvals, spool, rejected, policy)
            self.assertEqual(daemon.scan_once(), (0, 0))
            self.assertEqual(daemon.scan_once(), (1, 0))
            self.assertEqual(len(list(approvals.glob("*.json"))), 1)
            policy.write_text("{}\n", encoding="ascii")
            self.assertEqual(daemon.scan_once(), (0, 1))
            self.assertFalse(list(approvals.glob("*.json")))
            self.assertFalse(list(spool.glob("*.gcode")))
            records = list(rejected.glob("*.json"))
            self.assertEqual(len(records), 1)
            payload = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertIn("restart required", payload["policy_error"])

    def test_admission_directories_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            rejected = root / "rejected"
            for path in (gcodes, approvals, rejected):
                path.mkdir()
            with self.assertRaisesRegex(PolicyError, "non-overlapping"):
                AdmissionDaemon(
                    gcodes, approvals, approvals, rejected, POLICY_PATH
                )

    def test_policy_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gcodes = root / "gcodes"
            approvals = root / "approvals"
            spool = root / "spool"
            rejected = root / "rejected"
            for path in (gcodes, approvals, spool, rejected):
                path.mkdir()
            policy = root / "policy.json"
            policy.symlink_to(POLICY_PATH)
            with self.assertRaisesRegex(PolicyError, "real regular file"):
                AdmissionDaemon(gcodes, approvals, spool, rejected, policy)


if __name__ == "__main__":
    unittest.main()

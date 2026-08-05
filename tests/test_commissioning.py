from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import types
import unittest
import datetime as dt
from unittest import mock

from t300_mainline.commissioning import (
    CandidateController,
    CommissioningError,
    HOST_MARKER,
    HOST_UNITS,
    MAINTENANCE_MARKER,
    PRODUCTION_MARKER,
    PRODUCTION_UNITS,
    RELEASE_MARKER,
    REQUIRED_FINAL_RELEASE_EVIDENCE,
    REQUIRED_HOST_EVIDENCE,
    REQUIRED_MAINTENANCE_EVIDENCE,
    REQUIRED_RELEASE_EVIDENCE,
    SSH_UNIT,
    STORAGE_MARKER,
    STORAGE_UNITS,
    TRANSPORT_MARKER,
)
from t300_mainline.lockfile import sha256_file


class CandidateFixture:
    def __init__(self, root: Path):
        self.root = root
        self.active: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.controller = CandidateController(root)
        stage_path = root / "opt/t300/stage.manifest.json"
        stage_path.parent.mkdir(parents=True)
        stage_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "metadata": {"data_usb_uuid": "C66C-ADD5"},
                    "files": [{"path": "placeholder"}],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        candidate_path = root / "opt/t300/candidate.manifest.json"
        candidate_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release_ready": False,
                    "production_enabled": False,
                    "host_validated": False,
                    "transport_host_key_fingerprint": "SHA256:" + "A" * 43,
                    "stage_manifest_sha256": sha256_file(stage_path),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        safety = root / "etc/t300/klipper/safety.cfg"
        safety.parent.mkdir(parents=True)
        safety.write_text(
            "[t300_safety]\ncommissioning_lock: True\n", encoding="utf-8"
        )
        (root / "etc/t300/commissioning").mkdir()
        stage = json.loads(stage_path.read_text())
        stage["metadata"]["deploy_transport_present"] = True
        stage_path.write_text(json.dumps(stage, sort_keys=True), encoding="utf-8")
        candidate = json.loads(candidate_path.read_text())
        candidate["stage_manifest_sha256"] = sha256_file(stage_path)
        candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding="utf-8")

        def regular_file(path, description):
            try:
                return os.lstat(path)
            except OSError as exc:
                raise CommissioningError("%s is unavailable" % description) from exc

        self.controller._regular_root_file = regular_file
        configuration_digest = self.controller.configuration_digest
        self.controller.configuration_digest = (
            lambda strict_owner=True: configuration_digest(False)
        )
        self.controller._require_live_root = lambda: None
        self.controller._unit_active = lambda unit: unit in self.active
        self.controller._run_systemctl = self.run_systemctl
        self.controller._data_usb_connected = lambda uuid: uuid == "C66C-ADD5"

    def run_systemctl(self, *arguments, check=True):
        self.calls.append(tuple(arguments))
        if arguments[0] == "start":
            self.active.update(arguments[1:])
        elif arguments[0] == "stop":
            self.active.difference_update(arguments[1:])
        elif arguments[0] == "disable" and "--now" in arguments:
            self.active.difference_update(
                argument for argument in arguments[1:] if not argument.startswith("-")
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def marker(self, name: str, state: str, **extra):
        if name == STORAGE_MARKER:
            extra.setdefault("data_usb_uuid", "C66C-ADD5")
        value = self.controller._marker_value(name, state, **extra)
        path = self.controller.marker(name)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def host_evidence(self, path: Path, false_check: str | None = None):
        checks = {name: True for name in REQUIRED_HOST_EVIDENCE}
        if false_check is not None:
            checks[false_check] = False
        value = {
            "schema_version": 1,
            "candidate_sha256": self.controller.candidate_identity(False)[
                "candidate_sha256"
            ],
            "config_sha256": self.controller.configuration_digest(False),
            "checks": checks,
        }
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def maintenance_evidence(self, path: Path, captured_at: str | None = None):
        value = {
            "schema_version": 1,
            "candidate_sha256": self.controller.candidate_identity(False)[
                "candidate_sha256"
            ],
            "config_sha256": self.controller.configuration_digest(False),
            "captured_at": captured_at
            or dt.datetime.now(dt.timezone.utc).isoformat(),
            "checks": {name: True for name in REQUIRED_MAINTENANCE_EVIDENCE},
        }
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def release_evidence(self, path: Path):
        value = {
            "schema_version": 1,
            "candidate_sha256": self.controller.candidate_identity(False)[
                "candidate_sha256"
            ],
            "config_sha256": self.controller.configuration_digest(False),
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "checks": {name: True for name in REQUIRED_RELEASE_EVIDENCE},
        }
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path

    def final_release_evidence(
        self, path: Path, false_check: str | None = None
    ):
        checks = {name: True for name in REQUIRED_FINAL_RELEASE_EVIDENCE}
        if false_check is not None:
            checks[false_check] = False
        value = {
            "schema_version": 1,
            "candidate_sha256": self.controller.candidate_identity(False)[
                "candidate_sha256"
            ],
            "config_sha256": self.controller.configuration_digest(False),
            "checks": checks,
        }
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path


class CommissioningTests(unittest.TestCase):
    def test_configuration_inventory_rejects_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            source = config / "safety.cfg"
            source.write_text("value\n", encoding="ascii")
            os.link(source, root / "other-name")
            with self.assertRaisesRegex(CommissioningError, "hard-linked"):
                from t300_mainline.commissioning import inventory_configuration_tree

                inventory_configuration_tree(config, strict_owner=False)

    def test_storage_layout_is_created_only_under_the_mounted_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            mount = root / "mnt/t300-data"
            mount.mkdir(parents=True)
            fixture.controller._prepare_data_gcode_directory()
            for relative in (
                "gcodes",
                "timelapse/frames",
                "timelapse/videos",
                "timelapse/retained",
            ):
                self.assertTrue((mount / relative).is_dir())

    def test_storage_layout_rejects_a_symlinked_usb_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            mount = root / "mnt/t300-data"
            outside = root / "outside"
            mount.mkdir(parents=True)
            outside.mkdir()
            (mount / "gcodes").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(CommissioningError, "not one real directory"):
                fixture.controller._prepare_data_gcode_directory()

    @mock.patch("t300_mainline.commissioning.subprocess.run")
    def test_storage_enable_mounts_data_usb_then_gcode_bind(self, run):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.controller._prepare_data_gcode_directory = lambda: None
            run.return_value = types.SimpleNamespace(
                returncode=0,
                stdout="UUID=C66C-ADD5\nTYPE=vfat\n",
                stderr="",
            )
            preview = fixture.controller.storage_enable(False, None)
            result = fixture.controller.storage_enable(
                True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "enabled")
            self.assertEqual(fixture.active, set(STORAGE_UNITS))
            marker = json.loads(
                fixture.controller.marker(STORAGE_MARKER).read_text()
            )
            self.assertEqual(marker["data_usb_uuid"], "C66C-ADD5")

    @mock.patch("t300_mainline.commissioning.subprocess.run")
    def test_storage_enable_rolls_back_both_mounts_when_bind_fails(self, run):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.controller._prepare_data_gcode_directory = lambda: None
            run.return_value = types.SimpleNamespace(
                returncode=0,
                stdout="UUID=C66C-ADD5\nTYPE=vfat\n",
                stderr="",
            )

            def fail_bind(*arguments, check=True):
                fixture.calls.append(tuple(arguments))
                if arguments[:2] == ("start", STORAGE_UNITS[0]):
                    fixture.active.add(STORAGE_UNITS[0])
                elif arguments[0] == "disable" and "--now" in arguments:
                    fixture.active.discard(arguments[-1])
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            fixture.controller._run_systemctl = fail_bind
            preview = fixture.controller.storage_enable(False, None)
            with self.assertRaisesRegex(CommissioningError, "bind mount"):
                fixture.controller.storage_enable(
                    True, preview["expected_confirmation"]
                )
            self.assertFalse(fixture.active)
            self.assertFalse(fixture.controller.marker(STORAGE_MARKER).exists())

    def test_host_test_never_starts_a_printer_control_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            preview = fixture.controller.host_test_start(False, None)
            fixture.controller.host_test_start(
                True, preview["expected_confirmation"]
            )
            self.assertEqual(fixture.active, set(HOST_UNITS))
            started = [call for call in fixture.calls if call[0] == "start"]
            self.assertEqual(started, [("start", *HOST_UNITS)])
            for unit in PRODUCTION_UNITS:
                self.assertNotIn(unit, fixture.active)

    def test_host_test_aborts_if_a_control_unit_becomes_active(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))

            def contaminated_start(*arguments, check=True):
                fixture.calls.append(tuple(arguments))
                if arguments[0] == "start":
                    fixture.active.update(arguments[1:])
                    fixture.active.add("moonraker.service")
                elif arguments[0] == "stop":
                    fixture.active.difference_update(arguments[1:])
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            fixture.controller._run_systemctl = contaminated_start
            preview = fixture.controller.host_test_start(False, None)
            with self.assertRaisesRegex(CommissioningError, "activated printer-control"):
                fixture.controller.host_test_start(
                    True, preview["expected_confirmation"]
                )
            self.assertFalse(fixture.active)
            self.assertFalse(fixture.controller.marker(HOST_MARKER).exists())

    def test_host_accept_requires_every_physical_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "testing")
            fixture.active.update(HOST_UNITS)
            evidence = fixture.host_evidence(
                root / "evidence.json", false_check="touch_works"
            )
            with self.assertRaisesRegex(CommissioningError, "touch_works"):
                fixture.controller.host_test_accept(evidence, False, None)

    def test_failed_host_enable_revokes_validation_and_stops_host_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "testing")
            fixture.active.update(HOST_UNITS)
            evidence = fixture.host_evidence(root / "evidence.json")

            def fail_enable(*arguments, check=True):
                fixture.calls.append(tuple(arguments))
                if arguments[0] == "enable":
                    raise CommissioningError("enable failed")
                if arguments[0] == "disable" and "--now" in arguments:
                    fixture.active.difference_update(HOST_UNITS)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            fixture.controller._run_systemctl = fail_enable
            preview = fixture.controller.host_test_accept(evidence, False, None)
            with self.assertRaisesRegex(CommissioningError, "enable failed"):
                fixture.controller.host_test_accept(
                    evidence, True, preview["expected_confirmation"]
                )
            self.assertFalse(fixture.controller.marker(HOST_MARKER).exists())
            self.assertFalse(fixture.active)

    def test_validated_host_and_storage_can_arm_only_locked_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            preview = fixture.controller.production_arm(False, None)
            result = fixture.controller.production_arm(
                True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "armed")
            marker = json.loads(
                fixture.controller.marker(PRODUCTION_MARKER).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(marker["commissioning_lock"])
            self.assertNotIn(("start", *PRODUCTION_UNITS), fixture.calls)

    def test_failed_production_enable_revokes_marker_and_unit_enablement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)

            def fail_enable(*arguments, check=True):
                fixture.calls.append(tuple(arguments))
                if arguments[0] == "enable":
                    raise CommissioningError("enable failed")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            fixture.controller._run_systemctl = fail_enable
            preview = fixture.controller.production_arm(False, None)
            with self.assertRaisesRegex(CommissioningError, "enable failed"):
                fixture.controller.production_arm(
                    True, preview["expected_confirmation"]
                )
            self.assertFalse(fixture.controller.marker(PRODUCTION_MARKER).exists())
            self.assertIn(("disable", "--now", *PRODUCTION_UNITS), fixture.calls)

    def test_unlocked_initial_config_cannot_be_armed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            (root / "etc/t300/klipper/safety.cfg").write_text(
                "[t300_safety]\ncommissioning_lock: False\n", encoding="utf-8"
            )
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            with self.assertRaisesRegex(CommissioningError, "commissioning lock"):
                fixture.controller.production_arm(False, None)

    def test_calibrated_deployment_can_arm_unlocked_release_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            (root / "etc/t300/klipper/safety.cfg").write_text(
                "[t300_safety]\ncommissioning_lock: False\n", encoding="utf-8"
            )
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            identity = fixture.controller.candidate_identity(False)
            journal = root / "var/lib/t300/config-deploy-journal.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "complete-revalidation-required",
                        "candidate_sha256": identity["candidate_sha256"],
                        "new_config_sha256": fixture.controller.configuration_digest(False),
                        "calibration_ready": True,
                        "commissioning_lock": False,
                        "bundle_sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            evidence = fixture.release_evidence(root / "release-evidence.json")
            preview = fixture.controller.release_arm(evidence, False, None)
            result = fixture.controller.release_arm(
                evidence, True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "armed")
            marker = json.loads(
                fixture.controller.marker(PRODUCTION_MARKER).read_text()
            )
            self.assertFalse(marker["commissioning_lock"])
            self.assertNotIn(("start", *PRODUCTION_UNITS), fixture.calls)

    def test_hand_edited_unlock_without_deployment_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            (root / "etc/t300/klipper/safety.cfg").write_text(
                "[t300_safety]\ncommissioning_lock: False\n", encoding="utf-8"
            )
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            evidence = fixture.release_evidence(root / "release-evidence.json")
            with self.assertRaisesRegex(CommissioningError, "journal"):
                fixture.controller.release_arm(evidence, False, None)

    def test_marker_is_invalidated_by_configuration_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "validated")
            (root / "etc/t300/klipper/safety.cfg").write_text(
                "[t300_safety]\ncommissioning_lock: True\n# changed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CommissioningError, "Configuration changed|configuration changed"):
                fixture.controller._read_marker(HOST_MARKER, "validated")

    def test_maintenance_marker_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(MAINTENANCE_MARKER, "armed-once")
            result = fixture.controller.consume_maintenance_marker()
            self.assertEqual(result["state"], "consumed")
            self.assertFalse(fixture.controller.marker(MAINTENANCE_MARKER).exists())
            with self.assertRaises(CommissioningError):
                fixture.controller.consume_maintenance_marker()

    def test_service_gate_rejects_stale_marker_state(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "revalidation-required")
            with self.assertRaisesRegex(CommissioningError, "gate is closed"):
                fixture.controller.check_service_gate("host")

    def test_production_service_gate_requires_validated_host(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "testing")
            fixture.marker(PRODUCTION_MARKER, "started")
            with self.assertRaisesRegex(CommissioningError, "not validated"):
                fixture.controller.check_service_gate("production")

    def test_armed_production_marker_cannot_start_services(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(PRODUCTION_MARKER, "armed", commissioning_lock=True)
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            with self.assertRaisesRegex(CommissioningError, "gate is closed"):
                fixture.controller.check_service_gate("production")

    def test_owner_confirmed_production_start_promotes_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(PRODUCTION_MARKER, "armed", commissioning_lock=True)
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            preview = fixture.controller.production_start(False, None)
            result = fixture.controller.production_start(
                True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "started")
            marker = json.loads(
                fixture.controller.marker(PRODUCTION_MARKER).read_text()
            )
            self.assertEqual(marker["state"], "started")
            self.assertEqual(fixture.active, set((*STORAGE_UNITS, *PRODUCTION_UNITS)))

    def test_failed_production_start_stops_units_and_restores_armed_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(PRODUCTION_MARKER, "armed", commissioning_lock=True)
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.active.update(STORAGE_UNITS)
            preview = fixture.controller.production_start(False, None)

            def incomplete_start(*arguments, check=True):
                fixture.calls.append(tuple(arguments))
                if arguments[0] == "start":
                    fixture.active.add(PRODUCTION_UNITS[0])
                elif arguments[0] == "stop":
                    fixture.active.difference_update(arguments[1:])
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            fixture.controller._run_systemctl = incomplete_start
            with self.assertRaisesRegex(CommissioningError, "services failed"):
                fixture.controller.production_start(
                    True, preview["expected_confirmation"]
                )
            self.assertEqual(fixture.active, set(STORAGE_UNITS))
            marker = json.loads(
                fixture.controller.marker(PRODUCTION_MARKER).read_text()
            )
            self.assertEqual(marker["state"], "armed")

    def test_final_release_requires_every_real_world_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.marker(
                PRODUCTION_MARKER, "started", commissioning_lock=False
            )
            fixture.active.update((*STORAGE_UNITS, *HOST_UNITS, *PRODUCTION_UNITS))
            evidence = fixture.final_release_evidence(
                root / "release.json", false_check="vendor_rollback_drill_passed"
            )
            with self.assertRaisesRegex(
                CommissioningError, "vendor_rollback_drill_passed"
            ):
                fixture.controller.release_accept(evidence, False, None)
            self.assertFalse(fixture.controller.marker(RELEASE_MARKER).exists())

    def test_owner_can_accept_only_a_healthy_tested_unlocked_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.marker(
                PRODUCTION_MARKER, "started", commissioning_lock=False
            )
            fixture.active.update((*STORAGE_UNITS, *HOST_UNITS, *PRODUCTION_UNITS))
            evidence = fixture.final_release_evidence(root / "release.json")
            preview = fixture.controller.release_accept(evidence, False, None)
            result = fixture.controller.release_accept(
                evidence, True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "validated")
            marker = json.loads(
                fixture.controller.marker(RELEASE_MARKER).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["state"], "validated")
            self.assertEqual(fixture.active, set((*STORAGE_UNITS, *HOST_UNITS, *PRODUCTION_UNITS)))

    def test_production_gate_rejects_missing_data_usb(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.marker(PRODUCTION_MARKER, "started", commissioning_lock=True)
            fixture.active.update(STORAGE_UNITS)
            fixture.controller._data_usb_connected = lambda uuid: False
            with self.assertRaisesRegex(CommissioningError, "USB identity"):
                fixture.controller.check_service_gate("production")

    def test_production_gate_rejects_inactive_gcode_bind_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(STORAGE_MARKER, "enabled")
            fixture.marker(PRODUCTION_MARKER, "started", commissioning_lock=True)
            fixture.active.add(STORAGE_UNITS[0])
            with self.assertRaisesRegex(CommissioningError, "mounts are inactive"):
                fixture.controller.check_service_gate("production")

    def test_maintenance_evidence_must_be_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            stale = fixture.maintenance_evidence(
                root / "maintenance.json",
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(),
            )
            with self.assertRaisesRegex(CommissioningError, "not recent"):
                fixture.controller.maintenance_arm(stale, False, None)

    def test_evidence_with_another_hard_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = CandidateFixture(root)
            evidence = fixture.maintenance_evidence(root / "maintenance.json")
            os.link(evidence, root / "same-evidence-elsewhere.json")
            with self.assertRaisesRegex(CommissioningError, "bounded regular"):
                fixture.controller.maintenance_arm(evidence, False, None)

    def test_restricted_transport_is_temporary_and_starts_no_printer_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            preview = fixture.controller.transport_enable(False, None)
            result = fixture.controller.transport_enable(
                True, preview["expected_confirmation"]
            )
            self.assertEqual(result["state"], "enabled-temporarily")
            self.assertEqual(fixture.active, {SSH_UNIT})
            self.assertTrue(fixture.controller.marker(TRANSPORT_MARKER).exists())
            preview = fixture.controller.transport_disable(False, None)
            fixture.controller.transport_disable(
                True, preview["expected_confirmation"]
            )
            self.assertFalse(fixture.active)
            self.assertFalse(fixture.controller.marker(TRANSPORT_MARKER).exists())

    def test_production_gate_refuses_active_restricted_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CandidateFixture(Path(directory))
            fixture.marker(HOST_MARKER, "validated")
            fixture.marker(PRODUCTION_MARKER, "armed")
            fixture.marker(TRANSPORT_MARKER, "enabled")
            fixture.active.add(SSH_UNIT)
            with self.assertRaisesRegex(CommissioningError, "services must already be stopped"):
                fixture.controller.check_service_gate("production")


if __name__ == "__main__":
    unittest.main()

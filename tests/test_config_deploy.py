from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from t300_mainline.commissioning import (
    CandidateController,
    HOST_MARKER,
    PRODUCTION_MARKER,
    RELEASE_MARKER,
    REQUIRED_HOST_EVIDENCE,
    STORAGE_MARKER,
    inventory_configuration_tree,
)
from t300_mainline.config_deploy import (
    ConfigDeployError,
    REQUIRED_IDLE_CHECKS,
    REQUIRED_VALIDATION_CHECKS,
    _extract_verified_bundle,
    apply_bundle,
    inspect_bundle,
    prepare_bundle,
)
from t300_mainline.lockfile import sha256_file


STAGE_SHA = "a" * 64
CANDIDATE_SHA = "b" * 64


def write_config(root: Path, value: str, commissioning_lock: bool = True) -> Path:
    config = root / "etc/t300"
    (config / "klipper").mkdir(parents=True)
    (config / "klipper/safety.cfg").write_text(
        "[t300_safety]\ncommissioning_lock: %s\nvalue: %s\n"
        % ("True" if commissioning_lock else "False", value),
        encoding="utf-8",
    )
    (config / "gcode-policy.json").write_text(
        json.dumps({"value": value}, sort_keys=True), encoding="utf-8"
    )
    for path in sorted(config.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    config.chmod(0o555)
    return config


def inventory_for(config: Path, candidate_sha: str = CANDIDATE_SHA):
    value = inventory_configuration_tree(config, strict_owner=False)
    value.update(
        {
            "kind": "t300-config-inventory",
            "candidate_sha256": candidate_sha,
            "stage_sha256": "c" * 64,
        }
    )
    return value


def validation_report():
    return {
        "schema_version": 1,
        "generated_by": "t300-validation-v1",
        "stage_manifest_sha256": STAGE_SHA,
        "checks": {name: True for name in REQUIRED_VALIDATION_CHECKS},
        "evidence": {
            name: {"passed": True} for name in REQUIRED_VALIDATION_CHECKS
        },
    }


class ConfigDeployTests(unittest.TestCase):
    def prepare_fixture_bundle(self, root: Path, old_config: Path, new_config: Path):
        base_path = root / "base.json"
        base_path.write_text(
            json.dumps(inventory_for(old_config), sort_keys=True), encoding="utf-8"
        )
        report_path = root / "report.json"
        report_path.write_text(
            json.dumps(validation_report(), sort_keys=True), encoding="utf-8"
        )
        stage_root = root / "stage"
        (stage_root / "etc").mkdir(parents=True)
        shutil.copytree(new_config, stage_root / "etc/t300")
        stage_info = {
            "root": stage_root,
            "manifest": {
                "metadata": {"calibration_ready": False},
            },
        }
        output = root / "deployment.tar"
        with mock.patch(
            "t300_mainline.config_deploy.verify_stage", return_value=stage_info
        ):
            result = prepare_bundle(
                stage_root,
                STAGE_SHA,
                base_path,
                report_path,
                output,
            )
        return output, result, stage_info, base_path, report_path

    def test_prepared_bundle_is_deterministic_and_self_verifying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = write_config(root / "old", "old")
            new = write_config(root / "new", "new")
            bundle, first, stage_info, base, report = self.prepare_fixture_bundle(
                root, old, new
            )
            inspected = inspect_bundle(bundle, first["bundle_sha256"])
            self.assertEqual(
                inspected["manifest"]["new_config_sha256"],
                first["manifest"]["new_config_sha256"],
            )
            second = root / "deployment-2.tar"
            with mock.patch(
                "t300_mainline.config_deploy.verify_stage", return_value=stage_info
            ):
                other = prepare_bundle(
                    stage_info["root"], STAGE_SHA, base, report, second
                )
            self.assertEqual(first["bundle_sha256"], other["bundle_sha256"])

    def test_external_bundle_hash_rejects_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, result, _stage, _base, _report = self.prepare_fixture_bundle(
                root,
                write_config(root / "old", "old"),
                write_config(root / "new", "new"),
            )
            bundle.chmod(0o600)
            with bundle.open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(ConfigDeployError, "reviewed SHA"):
                inspect_bundle(bundle, result["bundle_sha256"])

    def test_bad_outer_hash_removes_privileged_verification_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, _result, _stage, _base, _report = self.prepare_fixture_bundle(
                root,
                write_config(root / "old", "old"),
                write_config(root / "new", "new"),
            )
            destination = root / "extract"
            destination.mkdir()
            with self.assertRaisesRegex(ConfigDeployError, "reviewed SHA"):
                _extract_verified_bundle(bundle, "0" * 64, destination)
            self.assertFalse((destination / ".verified-bundle-snapshot").exists())
            self.assertEqual(list(destination.iterdir()), [])

    def test_idle_evidence_with_another_hard_link_is_rejected(self):
        from t300_mainline.config_deploy import _validate_idle_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "idle.json"
            evidence.write_text("{}\n", encoding="ascii")
            os.link(evidence, root / "same-evidence-elsewhere.json")
            with self.assertRaisesRegex(ConfigDeployError, "bounded regular"):
                _validate_idle_evidence(evidence, "a" * 64, "b" * 64)

    def test_archive_rejects_traversal_and_links_even_with_matching_outer_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case, unsafe_member in (
                ("traversal", "config/../outside"),
                ("symlink", "config/unsafe-link"),
            ):
                with self.subTest(case=case):
                    bundle = root / (case + ".tar")
                    with tarfile.open(bundle, "w") as archive:
                        for name in ("deployment.json", "validation-report.json"):
                            payload = b"{}\n"
                            info = tarfile.TarInfo(name)
                            info.size = len(payload)
                            archive.addfile(info, io.BytesIO(payload))
                        info = tarfile.TarInfo(unsafe_member)
                        if case == "symlink":
                            info.type = tarfile.SYMTYPE
                            info.linkname = "/etc/shadow"
                        archive.addfile(info)
                    with self.assertRaises(ConfigDeployError):
                        inspect_bundle(bundle, sha256_file(bundle))

    def test_archive_rejects_excessive_member_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "many.tar"
            with tarfile.open(bundle, "w") as archive:
                for name in ("deployment.json", "validation-report.json", "config/value.cfg"):
                    payload = b"{}\n" if name.endswith(".json") else b"[printer]\n"
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            destination = root / "extract"
            destination.mkdir()
            with mock.patch("t300_mainline.config_deploy.MAX_BUNDLE_MEMBERS", 2):
                with self.assertRaisesRegex(ConfigDeployError, "member count"):
                    _extract_verified_bundle(bundle, sha256_file(bundle), destination)

    def test_apply_swaps_config_but_removes_old_host_and_production_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = write_config(root, "old")
            new = write_config(root / "new-source", "new")

            stage_manifest = root / "opt/t300/stage.manifest.json"
            stage_manifest.parent.mkdir(parents=True)
            stage_manifest.write_text("{}\n", encoding="utf-8")
            candidate = root / "opt/t300/candidate.manifest.json"
            candidate.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "release_ready": False,
                        "production_enabled": False,
                        "host_validated": False,
                        "stage_manifest_sha256": sha256_file(stage_manifest),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            candidate_sha = sha256_file(candidate)
            base_inventory = inventory_for(installed, candidate_sha)
            base_path = root / "base.json"
            base_path.write_text(json.dumps(base_inventory), encoding="utf-8")
            report = root / "report.json"
            report.write_text(json.dumps(validation_report()), encoding="utf-8")
            stage_root = root / "stage"
            (stage_root / "etc").mkdir(parents=True)
            shutil.copytree(new, stage_root / "etc/t300")
            stage_info = {
                "root": stage_root,
                "manifest": {"metadata": {"calibration_ready": False}},
            }
            bundle = root / "deployment.tar"
            with mock.patch(
                "t300_mainline.config_deploy.verify_stage", return_value=stage_info
            ):
                prepared = prepare_bundle(
                    stage_root, STAGE_SHA, base_path, report, bundle
                )
            quarantine = root / "var/lib/t300/incoming/config-bundle.tar"
            quarantine.parent.mkdir(parents=True)
            shutil.copyfile(bundle, quarantine)
            bundle = quarantine

            marker_dir = installed / "commissioning"
            installed.chmod(0o755)
            marker_dir.mkdir(mode=0o700)
            installed.chmod(0o555)
            controller = CandidateController(root)
            controller._regular_root_file = lambda path, description: os.lstat(path)
            original_digest = controller.configuration_digest
            controller.configuration_digest = (
                lambda strict_owner=True: original_digest(False)
            )
            controller._require_live_root = lambda: None
            controller._require_units_inactive = lambda units: None
            for name, state in (
                (HOST_MARKER, "validated"),
                (STORAGE_MARKER, "enabled"),
                (PRODUCTION_MARKER, "started"),
                (RELEASE_MARKER, "validated"),
            ):
                value = controller._marker_value(name, state)
                (marker_dir / name).write_text(json.dumps(value), encoding="utf-8")
                (marker_dir / name).chmod(0o400)

            evidence = root / "idle.json"
            evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidate_sha256": candidate_sha,
                        "config_sha256": base_inventory["config_sha256"],
                        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "checks": {name: True for name in REQUIRED_IDLE_CHECKS},
                    }
                ),
                encoding="utf-8",
            )
            preview = apply_bundle(
                bundle,
                prepared["bundle_sha256"],
                evidence,
                False,
                None,
                root=root,
                controller=controller,
            )
            real_inventory = inventory_configuration_tree
            with mock.patch(
                "t300_mainline.config_deploy._validate_commissioning_directory"
            ), mock.patch(
                "t300_mainline.config_deploy._chown_root_tree"
            ), mock.patch(
                "t300_mainline.config_deploy.inventory_configuration_tree",
                side_effect=lambda path, strict_owner=True: real_inventory(path, False),
            ):
                result = apply_bundle(
                    bundle,
                    prepared["bundle_sha256"],
                    evidence,
                    True,
                    preview["expected_confirmation"],
                    root=root,
                    controller=controller,
                )
            self.assertEqual(result["state"], "revalidation-required")
            self.assertIn("value: new", (installed / "klipper/safety.cfg").read_text())
            self.assertFalse((installed / "commissioning" / HOST_MARKER).exists())
            self.assertFalse((installed / "commissioning" / PRODUCTION_MARKER).exists())
            self.assertFalse((installed / "commissioning" / RELEASE_MARKER).exists())
            self.assertTrue((installed / "commissioning" / STORAGE_MARKER).exists())
            self.assertEqual(result["services_started"], [])
            self.assertTrue(Path(result["atomic_previous_tree"]).is_dir())
            self.assertTrue(result["quarantine_consumed"])
            self.assertFalse(quarantine.exists())

    def test_stale_base_is_rejected_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = write_config(root, "old")
            bundle, prepared, _stage, _base, _report = self.prepare_fixture_bundle(
                root,
                installed,
                write_config(root / "new-source", "new"),
            )
            safety = installed / "klipper/safety.cfg"
            safety.chmod(0o644)
            with safety.open("a", encoding="utf-8") as handle:
                handle.write("# changed after bundle review\n")
            safety.chmod(0o444)

            controller = mock.Mock()
            controller.candidate_identity.return_value = {
                "candidate_sha256": CANDIDATE_SHA
            }
            controller.configuration_digest.return_value = inventory_configuration_tree(
                installed, strict_owner=False
            )["config_sha256"]
            with self.assertRaisesRegex(ConfigDeployError, "base does not match"):
                apply_bundle(
                    bundle,
                    prepared["bundle_sha256"],
                    root / "unused-evidence.json",
                    False,
                    None,
                    root=root,
                    controller=controller,
                )

    def test_post_exchange_verification_failure_restores_old_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = write_config(root, "old")
            new = write_config(root / "new-source", "new")

            stage_manifest = root / "opt/t300/stage.manifest.json"
            stage_manifest.parent.mkdir(parents=True)
            stage_manifest.write_text("{}\n", encoding="utf-8")
            candidate = root / "opt/t300/candidate.manifest.json"
            candidate.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "release_ready": False,
                        "production_enabled": False,
                        "host_validated": False,
                        "stage_manifest_sha256": sha256_file(stage_manifest),
                    }
                ),
                encoding="utf-8",
            )
            candidate_sha = sha256_file(candidate)
            base_inventory = inventory_for(installed, candidate_sha)
            base = root / "base.json"
            base.write_text(json.dumps(base_inventory), encoding="utf-8")
            report = root / "report.json"
            report.write_text(json.dumps(validation_report()), encoding="utf-8")
            stage_root = root / "stage"
            (stage_root / "etc").mkdir(parents=True)
            shutil.copytree(new, stage_root / "etc/t300")
            bundle = root / "deployment.tar"
            with mock.patch(
                "t300_mainline.config_deploy.verify_stage",
                return_value={
                    "root": stage_root,
                    "manifest": {"metadata": {"calibration_ready": False}},
                },
            ):
                prepared = prepare_bundle(stage_root, STAGE_SHA, base, report, bundle)

            installed.chmod(0o755)
            marker_dir = installed / "commissioning"
            marker_dir.mkdir(mode=0o700)
            installed.chmod(0o555)
            controller = CandidateController(root)
            controller._regular_root_file = lambda path, description: os.lstat(path)
            original_digest = controller.configuration_digest
            controller.configuration_digest = (
                lambda strict_owner=True: original_digest(False)
            )
            controller._require_live_root = lambda: None
            controller._require_units_inactive = lambda units: None
            for name, state in (
                (HOST_MARKER, "validated"),
                (STORAGE_MARKER, "enabled"),
                (PRODUCTION_MARKER, "started"),
            ):
                value = controller._marker_value(name, state)
                (marker_dir / name).write_text(json.dumps(value), encoding="utf-8")
                (marker_dir / name).chmod(0o400)
            evidence = root / "idle.json"
            evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidate_sha256": candidate_sha,
                        "config_sha256": base_inventory["config_sha256"],
                        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "checks": {name: True for name in REQUIRED_IDLE_CHECKS},
                    }
                ),
                encoding="utf-8",
            )
            preview = apply_bundle(
                bundle,
                prepared["bundle_sha256"],
                evidence,
                False,
                None,
                root=root,
                controller=controller,
            )

            digest_calls = 0

            def fail_post_exchange(strict_owner=True):
                nonlocal digest_calls
                digest_calls += 1
                if digest_calls == 3:
                    return "f" * 64
                return original_digest(False)

            controller.configuration_digest = fail_post_exchange
            real_inventory = inventory_configuration_tree
            with mock.patch(
                "t300_mainline.config_deploy._validate_commissioning_directory"
            ), mock.patch(
                "t300_mainline.config_deploy._chown_root_tree"
            ), mock.patch(
                "t300_mainline.config_deploy.inventory_configuration_tree",
                side_effect=lambda path, strict_owner=True: real_inventory(path, False),
            ):
                with self.assertRaisesRegex(
                    ConfigDeployError, "post-exchange configuration verification"
                ):
                    apply_bundle(
                        bundle,
                        prepared["bundle_sha256"],
                        evidence,
                        True,
                        preview["expected_confirmation"],
                        root=root,
                        controller=controller,
                    )
            self.assertIn("value: old", (installed / "klipper/safety.cfg").read_text())
            journal = json.loads(
                (root / "var/lib/t300/config-deploy-journal.json").read_text()
            )
            self.assertEqual(journal["status"], "failed")
            self.assertEqual(journal["rollback"], "complete")
            self.assertTrue(Path(journal["failed_candidate_tree"]).is_dir())


if __name__ == "__main__":
    unittest.main()

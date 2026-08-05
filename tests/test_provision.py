from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from t300_mainline.display_auth import DisplayAuthError, create_authority
from t300_mainline.lockfile import sha256_file
from t300_mainline.provision import (
    BUILD_WORKSPACE_BYTES,
    MIN_POST_PROVISION_FREE_BYTES,
    T300_UNITS,
    ProvisionError,
    _copy_exact_stage_file,
    _ensure_dir,
    _install_mainsail_defaults,
    _validate_effective_sshd,
    capacity_budget,
    provision,
    verify_stage,
)


ROOT = Path(__file__).resolve().parents[1]


def make_stage(root: Path) -> tuple[Path, str]:
    stage = root / "stage"
    files = {
        "opt/t300/stack.lock.json": ROOT / "stack.lock.json",
        "opt/t300/debian-artifacts.lock.json": ROOT / "mainline/build/debian-artifacts.lock.json",
        "opt/t300/debian-root-packages.json": ROOT / "mainline/build/debian-root-packages.json",
        "opt/t300/python-artifacts.lock.json": ROOT / "mainline/build/python-artifacts.lock.json",
    }
    records = []
    for name, source in files.items():
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        records.append(
            {
                "path": name,
                "size": destination.stat().st_size,
                "mode": oct(destination.stat().st_mode & 0o777),
                "sha256": sha256_file(destination),
            }
        )
    manifest = {
        "schema_version": 1,
        "metadata": {
            "release_ready": False,
            "stage_kind": "source-and-configuration-overlay",
            "deploy_transport_present": False,
            "deploy_public_key_fingerprint": None,
        },
        "files": sorted(records, key=lambda item: item["path"]),
    }
    manifest_path = stage / "stage.manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return stage, sha256_file(manifest_path)


class ProvisionTests(unittest.TestCase):
    def test_verified_stage_binds_external_manifest_and_internal_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            value = verify_stage(stage, digest)
            self.assertEqual(value["manifest_sha256"], digest)
            self.assertEqual(len(value["debian"]["artifacts"]), 353)

    def test_stage_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            with (stage / "opt/t300/stack.lock.json").open("ab") as handle:
                handle.write(b"\n")
            with self.assertRaisesRegex(ProvisionError, "changed after manifest"):
                verify_stage(stage, digest)

    def test_stage_symlink_is_rejected_even_when_unlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            os.symlink("stack.lock.json", stage / "opt/t300/alias")
            with self.assertRaisesRegex(ProvisionError, "contains a symlink"):
                verify_stage(stage, digest)

    def test_stage_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, digest = make_stage(root)
            alias = root / "stage-alias"
            alias.symlink_to(stage, target_is_directory=True)
            with self.assertRaisesRegex(ProvisionError, "one real directory"):
                verify_stage(alias, digest)

    def test_stage_manifest_needs_laptop_supplied_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, _digest = make_stage(Path(directory))
            with self.assertRaisesRegex(ProvisionError, "laptop-supplied"):
                verify_stage(stage, "a" * 64)

    def test_stage_only_audit_does_not_inspect_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            with mock.patch(
                "t300_mainline.provision.inspect_candidate",
                side_effect=AssertionError("target inspection must not run"),
            ):
                value = provision(
                    stage,
                    digest,
                    apply=False,
                    confirmation=None,
                    verify_stage_only=True,
                )
            self.assertTrue(value["stage_verified"])
            self.assertEqual(value["stage_manifest_sha256"], digest)
            self.assertEqual(value["file_count"], 4)

    def test_stage_only_audit_rejects_apply_options(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            with self.assertRaisesRegex(ProvisionError, "cannot be combined"):
                provision(
                    stage,
                    digest,
                    apply=True,
                    confirmation="PROVISION T300 USB test",
                    verify_stage_only=True,
                )

    def test_capacity_budget_reserves_install_and_build_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            stage_info = verify_stage(stage, digest)
            total = 8 * 1024**3
            expected_stage = sum(
                record["size"] for record in stage_info["files"].values()
            ) + (stage / "stage.manifest.json").stat().st_size
            expected_debian = (
                stage_info["debian"]["solver"]["total_installed_size_kib"]
                * 1024
            )
            budget = capacity_budget(stage_info, total, total)
            self.assertEqual(budget["stage_copy_bytes"], expected_stage)
            self.assertEqual(budget["debian_installed_bytes"], expected_debian)
            self.assertEqual(budget["build_workspace_bytes"], BUILD_WORKSPACE_BYTES)
            self.assertGreaterEqual(
                budget["protected_free_bytes"], MIN_POST_PROVISION_FREE_BYTES
            )
            self.assertTrue(budget["ready"])

    def test_capacity_budget_fails_closed_without_headroom(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            stage_info = verify_stage(stage, digest)
            budget = capacity_budget(stage_info, 2 * 1024**3, 512 * 1024**2)
            self.assertFalse(budget["ready"])
            self.assertLess(budget["headroom_bytes"], 0)

    def test_capacity_budget_rejects_impossible_filesystem_values(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, digest = make_stage(Path(directory))
            stage_info = verify_stage(stage, digest)
            with self.assertRaisesRegex(ProvisionError, "capacity values"):
                capacity_budget(stage_info, 100, 101)

    def test_exact_stage_copy_binds_destination_to_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"reviewed stage bytes")
            source.chmod(0o444)
            destination = root / "installed/candidate"
            digest = sha256_file(source)
            _copy_exact_stage_file(
                source,
                destination,
                source.stat().st_size,
                0o444,
                digest,
            )
            self.assertEqual(destination.read_bytes(), b"reviewed stage bytes")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o444)

            bad_destination = root / "installed/rejected"
            with self.assertRaisesRegex(ProvisionError, "changed while copied"):
                _copy_exact_stage_file(
                    source,
                    bad_destination,
                    source.stat().st_size,
                    0o444,
                    "0" * 64,
                )
            self.assertFalse(bad_destination.exists())

    def test_runtime_directory_rejects_final_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ProvisionError, "unsafe or unavailable"):
                _ensure_dir(str(alias), 0o755, "root", "root")

    def test_mainsail_defaults_are_installed_once_and_read_only(self):
        defaults = {
            "general": {},
            "navigation": {},
            "uiSettings": {},
            "macros": {},
            "dashboard": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            theme = Path(directory) / ".theme"
            theme.mkdir()
            with mock.patch(
                "t300_mainline.provision._read_json", return_value=defaults
            ), mock.patch(
                "t300_mainline.provision._ensure_dir", return_value=theme
            ), mock.patch("t300_mainline.provision.os.chown"):
                _install_mainsail_defaults()
            destination = theme / "default.json"
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), defaults)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(theme.stat().st_mode), 0o555)

            with mock.patch(
                "t300_mainline.provision._read_json", return_value=defaults
            ), mock.patch(
                "t300_mainline.provision._ensure_dir", return_value=theme
            ), self.assertRaisesRegex(ProvisionError, "refusing to replace"):
                _install_mainsail_defaults()

    def test_mainsail_defaults_reject_unreviewed_top_level_sections(self):
        with mock.patch(
            "t300_mainline.provision._read_json", return_value={"general": {}}
        ), self.assertRaisesRegex(ProvisionError, "reviewed UI sections"):
            _install_mainsail_defaults()

    def test_every_printer_facing_unit_includes_klipperscreen(self):
        self.assertIn("klipperscreen.service", T300_UNITS)
        self.assertIn(r"var-lib-t300-moonraker\x2ddata-gcodes.mount", T300_UNITS)

    def test_effective_sshd_policy_is_checked_for_the_staged_source_network(self):
        required = {
            "permitrootlogin": "no",
            "pubkeyauthentication": "yes",
            "authenticationmethods": "publickey",
            "passwordauthentication": "no",
            "kbdinteractiveauthentication": "no",
            "permitemptypasswords": "no",
            "hostbasedauthentication": "no",
            "x11forwarding": "no",
            "allowagentforwarding": "no",
            "allowtcpforwarding": "no",
            "permittunnel": "no",
            "permittty": "no",
            "permituserenvironment": "no",
            "permituserrc": "no",
            "authorizedkeysfile": "/etc/t300/deploy_authorized_keys",
            "forcecommand": (
                "/opt/t300/venvs/control/bin/python "
                "/opt/t300/control/bin/t300-transfer-receive.py"
            ),
            "disableforwarding": "yes",
            "allowusers": "t300-deploy@10.42.42.0/24",
        }
        output = "\n".join("%s %s" % item for item in required.items()) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "60-t300.conf"
            config.write_text(
                "AllowUsers t300-deploy@10.42.42.0/24\n", encoding="utf-8"
            )
            completed = mock.Mock(stdout=output)
            with mock.patch("t300_mainline.provision._run", return_value=completed) as run:
                settings = _validate_effective_sshd(config)
            self.assertEqual(settings["forcecommand"], required["forcecommand"])
            command = run.call_args.args[0]
            self.assertIn("user=t300-deploy,host=t300-candidate,addr=10.42.42.1", command)

    def test_effective_sshd_policy_rejects_base_config_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "60-t300.conf"
            config.write_text(
                "AllowUsers t300-deploy@10.42.42.0/24\n", encoding="utf-8"
            )
            completed = mock.Mock(
                stdout="permitrootlogin yes\nallowusers t300-deploy@10.42.42.0/24\n"
            )
            with mock.patch("t300_mainline.provision._run", return_value=completed):
                with self.assertRaisesRegex(ProvisionError, "effective restricted SSH"):
                    _validate_effective_sshd(config)

    def test_display_cookie_is_atomically_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "Xauthority"
            xauth = root / "xauth"
            xauth.write_bytes(b"binary")
            xauth.chmod(0o755)
            real_stat = Path.stat

            def root_owned(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path in (root, xauth):
                    values = list(result)
                    # os.stat_result stores uid at index 4.
                    values[4] = 0
                    return os.stat_result(values)
                return result

            completed = mock.Mock(returncode=0, stderr=b"")
            with mock.patch.object(Path, "stat", autospec=True, side_effect=root_owned), mock.patch(
                "t300_mainline.display_auth.subprocess.run", return_value=completed
            ):
                self.assertEqual(create_authority(authority, xauth), authority)
            self.assertTrue(authority.is_file())
            self.assertEqual(stat.S_IMODE(authority.stat().st_mode), 0o640)

    def test_display_cookie_rejects_other_display(self):
        with self.assertRaisesRegex(DisplayAuthError, "fixed at :0"):
            create_authority(Path("/tmp/Xauthority"), Path("/bin/false"), ":1")


if __name__ == "__main__":
    unittest.main()

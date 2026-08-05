from __future__ import annotations

import io
import base64
import hashlib
import json
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from t300_mainline.lockfile import LockfileError, load_lock, sha256_file
from t300_mainline.debian_artifacts import DebianArtifactError, load_debian_lock
from t300_mainline.staging import (
    StagingError,
    _verify_base_signature,
    extract_web_release,
    extract_source,
    stage_recovery_overlay,
    validate_calibration,
    validate_trusted_laptop_network,
)
from t300_mainline.private_config import (
    PrivateConfigError,
    load_purchased_gergo,
)


ROOT = Path(__file__).resolve().parents[1]


VALID_CALIBRATION = """[extruder]
rotation_distance: 3.5
control: pid
pid_Kp: 1
pid_Ki: 1
pid_Kd: 1
[heater_bed]
control: pid
pid_Kp: 1
pid_Ki: 1
pid_Kd: 1
[probe]
z_offset: 1
[input_shaper]
shaper_type_x: mzv
shaper_freq_x: 40
shaper_type_y: mzv
shaper_freq_y: 40
"""


def ed25519_public_key(path: Path) -> str:
    def ssh_string(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value

    blob = ssh_string(b"ssh-ed25519") + ssh_string(b"r" * 32)
    encoded = base64.b64encode(blob).decode("ascii")
    path.write_text("ssh-ed25519 %s recovery-laptop\n" % encoded, encoding="ascii")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode(
        "ascii"
    ).rstrip("=")


def private_gergo_fixture(root: Path, macro: bytes) -> Path:
    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("macro_z_tilt_via_knob.cfg", macro)
    outer = root / "purchased.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("private/macro_v3(extract!).zip", inner_bytes.getvalue())
    return outer


def web_release_fixture(root: Path, *, index: str | None = None) -> Path:
    archive = root / "mainsail.zip"
    if index is None:
        index = (
            '<script type="module" src="/assets/index.js"></script>'
            '<link rel="stylesheet" href="/assets/index.css">'
        )
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(".version", "v2.18.2\n")
        bundle.writestr("index.html", index)
        bundle.writestr("assets/index.js", "console.log('compiled');\n")
        bundle.writestr("assets/index.css", "body { color: black; }\n")
    return archive


class StagingTests(unittest.TestCase):
    def test_repository_lock_is_structurally_valid(self):
        lock = load_lock(ROOT / "stack.lock.json")
        self.assertEqual(
            lock["base_image"]["signing_key"]["fingerprint"],
            "DF00FAF1C577104B50BF1D0093D6889F9F0E78D5",
        )
        self.assertIn("ustreamer", {item["name"] for item in lock["components"]})
        mainsail = next(
            item for item in lock["components"] if item["name"] == "mainsail"
        )
        self.assertEqual(mainsail["release_asset"]["version"], "v2.18.2")
        self.assertEqual(
            mainsail["release_asset"]["sha256"],
            "df2ba7c301f7bfc8ac9f122741a6ba08356d679ecfa1f62f898d0337802d5de5",
        )
        kamp_patch = next(
            item
            for item in lock["compatibility_patches"]
            if item["name"] == "kamp-line-purge-bounds"
        )
        self.assertEqual(kamp_patch["component"], "kamp")
        self.assertEqual(kamp_patch["origin"], "local")
        self.assertEqual(
            sha256_file(ROOT / kamp_patch["path"]), kamp_patch["sha256"]
        )
        self.assertEqual(
            lock["recovery_boot"],
            {
                "method": "interactive-serial-u-boot-usb0",
                "serial_baud": 1500000,
                "root_uuid": "3a703405-2025-4c62-aae4-7fb9accdb996",
                "fdtfile": "rockchip/rk3328-mksklipad50.dtb",
                "dtb_sha256": "8db9862998cbe698201afd8f0e86a65859a54b3c190500873a22633626b15fa1",
                "image_sha256": "b6cc9395839e4cd99c45b0e083e47e1df0768823e7aaa505af30c1f57ae297ab",
                "uinitrd_sha256": "dd76210a666302dbc1d9536e0d23fa063388da20d85bb994c6c3072c0496fbca",
                "boot_cmd_sha256": "91b5e22d036bb2defcfeb1612d502510b6ebe9e19d2f1eeeebad4dc08a1fad37",
                "boot_scr_sha256": "96a8d7cd67f4040be96117c5caee8bb9152ea20b836ff2c6763591d157e14080",
            },
        )

    def test_recovery_boot_lock_rejects_route_or_device_tree_drift(self):
        original = json.loads(
            (ROOT / "stack.lock.json").read_text(encoding="utf-8")
        )
        mutations = (
            ("method", "automatic-kexec", "interactive serial U-Boot"),
            ("serial_baud", 115200, "Klipad50 console"),
            ("fdtfile", "rockchip/rk3328-roc-cc.dtb", "Klipad50 device tree"),
        )
        for key, value, message in mutations:
            lock = json.loads(json.dumps(original))
            lock["recovery_boot"][key] = value
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "stack.lock.json"
                path.write_text(json.dumps(lock), encoding="utf-8")
                with self.subTest(key=key), self.assertRaisesRegex(
                    LockfileError, message
                ):
                    load_lock(path)

    def test_repository_debian_artifact_lock_is_valid(self):
        lock = load_debian_lock(ROOT / "mainline/build/debian-artifacts.lock.json")
        self.assertEqual(len(lock["artifacts"]), 353)
        self.assertEqual(lock["solver"]["upgrades_from_signed_base"], 7)
        self.assertGreater(lock["solver"]["total_installed_size_kib"], 0)
        self.assertEqual(
            lock["solver"]["total_installed_size_kib"],
            sum(item["installed_size_kib"] for item in lock["artifacts"]),
        )

    def test_debian_artifact_lock_rejects_nonofficial_url(self):
        value = json.loads(
            (ROOT / "mainline/build/debian-artifacts.lock.json").read_text(encoding="utf-8")
        )
        value["artifacts"][0]["url"] = "https://example.invalid/package.deb"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "debian.lock.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(DebianArtifactError, "not official HTTPS"):
                load_debian_lock(path)

    def test_lock_rejects_short_signing_key_id(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        lock["base_image"]["signing_key"]["fingerprint"] = "9F0E78D5"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.lock.json"
            path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaisesRegex(LockfileError, "full uppercase fingerprint"):
                load_lock(path)

    def test_local_patch_cannot_claim_an_upstream_commit(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        local = next(
            item
            for item in lock["compatibility_patches"]
            if item.get("origin") == "local"
        )
        local["upstream_commit"] = local["base_commit"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.lock.json"
            path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaisesRegex(LockfileError, "must not claim"):
                load_lock(path)

    def test_lock_requires_exactly_one_compiled_mainsail_release(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        mainsail = next(
            item for item in lock["components"] if item["name"] == "mainsail"
        )
        mainsail.pop("release_asset")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.lock.json"
            path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaisesRegex(LockfileError, "compiled Mainsail"):
                load_lock(path)

    def test_signature_gate_requires_exact_validsig_fingerprint(self):
        fingerprint = "DF00FAF1C577104B50BF1D0093D6889F9F0E78D5"
        base = {
            "name": "image.img.xz",
            "sha256": "a" * 64,
            "signing_key": {"name": "signing.key", "fingerprint": fingerprint},
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "image.img.xz").write_bytes(b"image")
            (cache / "image.img.xz.asc").write_bytes(b"signature")
            (cache / "image.img.xz.sha").write_text(
                "%s image.img.xz\n" % (base["sha256"],), encoding="ascii"
            )
            (cache / "signing.key").write_bytes(b"key")
            results = [
                SimpleNamespace(
                    returncode=0,
                    stdout="fpr:::::::::%s:\n" % (fingerprint,),
                    stderr="",
                ),
                SimpleNamespace(returncode=2, stdout="", stderr="agent unavailable"),
                SimpleNamespace(
                    returncode=0,
                    stdout="[GNUPG:] VALIDSIG %s 0 0\n" % (fingerprint,),
                    stderr="",
                ),
            ]
            with mock.patch("t300_mainline.staging.subprocess.run", side_effect=results):
                _verify_base_signature(base, cache)

    def test_signature_gate_rejects_other_valid_key(self):
        fingerprint = "DF00FAF1C577104B50BF1D0093D6889F9F0E78D5"
        other = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        base = {
            "name": "image.img.xz",
            "sha256": "a" * 64,
            "signing_key": {"name": "signing.key", "fingerprint": fingerprint},
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "image.img.xz").write_bytes(b"image")
            (cache / "image.img.xz.asc").write_bytes(b"signature")
            (cache / "image.img.xz.sha").write_text(
                "%s image.img.xz\n" % (base["sha256"],), encoding="ascii"
            )
            (cache / "signing.key").write_bytes(b"key")
            results = [
                SimpleNamespace(
                    returncode=0,
                    stdout="fpr:::::::::%s:\n" % (fingerprint,),
                    stderr="",
                ),
                SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(
                    returncode=0,
                    stdout="[GNUPG:] VALIDSIG %s 0 0\n" % (other,),
                    stderr="",
                ),
            ]
            with mock.patch("t300_mainline.staging.subprocess.run", side_effect=results):
                with self.assertRaisesRegex(StagingError, "not valid for the locked key"):
                    _verify_base_signature(base, cache)

    def test_calibration_allowlist_accepts_expected_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.cfg"
            path.write_text(VALID_CALIBRATION, encoding="ascii")
            self.assertEqual(validate_calibration(path), VALID_CALIBRATION.encode("ascii"))

    def test_calibration_rejects_safety_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.cfg"
            path.write_text(VALID_CALIBRATION.replace("control: pid", "max_temp: 999", 1), encoding="ascii")
            with self.assertRaisesRegex(StagingError, "forbidden options"):
                validate_calibration(path)

    def test_calibration_rejects_duplicate_and_missing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.cfg"
            duplicate.write_text(
                VALID_CALIBRATION.replace(
                    "rotation_distance: 3.5",
                    "rotation_distance: 3.5\nrotation_distance: 3.6",
                ),
                encoding="ascii",
            )
            with self.assertRaisesRegex(StagingError, "malformed"):
                validate_calibration(duplicate)

            missing = root / "missing.cfg"
            missing.write_text(
                VALID_CALIBRATION.replace("rotation_distance: 3.5\n", ""),
                encoding="ascii",
            )
            with self.assertRaisesRegex(StagingError, "missing required"):
                validate_calibration(missing)

    def test_calibration_rejects_nonfinite_and_gross_rotation_typo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nonfinite = root / "nonfinite.cfg"
            nonfinite.write_text(
                VALID_CALIBRATION.replace("pid_Kp: 1", "pid_Kp: NaN", 1),
                encoding="ascii",
            )
            with self.assertRaisesRegex(StagingError, "finite number"):
                validate_calibration(nonfinite)

            typo = root / "typo.cfg"
            typo.write_text(
                VALID_CALIBRATION.replace("rotation_distance: 3.5", "rotation_distance: 35"),
                encoding="ascii",
            )
            with self.assertRaisesRegex(StagingError, "rotation_distance"):
                validate_calibration(typo)

    def test_calibration_rejects_unsupported_shaper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.cfg"
            path.write_text(
                VALID_CALIBRATION.replace("shaper_type_x: mzv", "shaper_type_x: magic"),
                encoding="ascii",
            )
            with self.assertRaisesRegex(StagingError, "unsupported by pinned Klipper"):
                validate_calibration(path)

    def test_trusted_laptop_network_is_narrow_private_ipv4(self):
        self.assertEqual(
            str(validate_trusted_laptop_network("10.42.42.1/24")),
            "10.42.42.0/24",
        )
        for value in ("10.0.0.0/8", "2001:db8::/64", "203.0.113.0/24", "127.0.0.1/32"):
            with self.subTest(value=value):
                with self.assertRaises(StagingError):
                    validate_trusted_laptop_network(value)

    def test_private_gergo_requires_exact_outer_hash_and_nested_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            macro = b"[gcode_macro ONE]\ngcode:\n  M400\n[gcode_macro TWO]\ngcode:\n  M400\n[gcode_macro THREE]\ngcode:\n  M400\n"
            outer = private_gergo_fixture(root, macro)
            with mock.patch(
                "t300_mainline.private_config.GERGO_OUTER_SHA256",
                sha256_file(outer),
            ):
                self.assertEqual(load_purchased_gergo(outer), macro)

    def test_private_gergo_may_not_replace_an_existing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            macro = b"[gcode_macro ONE]\nrename_existing: G28\ngcode:\n  M400\n[gcode_macro TWO]\ngcode:\n  M400\n[gcode_macro THREE]\ngcode:\n  M400\n"
            outer = private_gergo_fixture(root, macro)
            with mock.patch(
                "t300_mainline.private_config.GERGO_OUTER_SHA256",
                sha256_file(outer),
            ):
                with self.assertRaisesRegex(PrivateConfigError, "replace"):
                    load_purchased_gergo(outer)

    def test_private_gergo_rejects_commands_outside_maintenance_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            macro = b"[gcode_macro ONE]\ngcode:\n  SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200\n[gcode_macro TWO]\ngcode:\n  M400\n[gcode_macro THREE]\ngcode:\n  M400\n"
            outer = private_gergo_fixture(root, macro)
            with mock.patch(
                "t300_mainline.private_config.GERGO_OUTER_SHA256",
                sha256_file(outer),
            ):
                with self.assertRaisesRegex(PrivateConfigError, "maintenance policy"):
                    load_purchased_gergo(outer)

    def test_private_gergo_allows_only_exact_z_release(self):
        accepted = (
            b"[gcode_macro ONE]\ngcode:\n"
            b"  SET_STEPPER_ENABLE STEPPER=stepper_z ENABLE=0\n"
            b"[gcode_macro TWO]\ngcode:\n  M400\n"
            b"[gcode_macro THREE]\ngcode:\n  M400\n"
        )
        rejected = (
            accepted.replace(b"ENABLE=0", b"ENABLE=1"),
            accepted.replace(b"stepper_z", b"stepper_x"),
            accepted.replace(b"ENABLE=0", b"ENABLE=0 EXTRA=1"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outer = private_gergo_fixture(root, accepted)
            with mock.patch(
                "t300_mainline.private_config.GERGO_OUTER_SHA256",
                sha256_file(outer),
            ):
                self.assertEqual(load_purchased_gergo(outer), accepted)
        for index, macro in enumerate(rejected):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outer = private_gergo_fixture(root, macro)
                with mock.patch(
                    "t300_mainline.private_config.GERGO_OUTER_SHA256",
                    sha256_file(outer),
                ), self.assertRaisesRegex(PrivateConfigError, "only release"):
                    load_purchased_gergo(outer)

    def test_private_gergo_rejects_unreviewed_outer_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "not-purchased.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("placeholder", b"not the package")
            with self.assertRaisesRegex(PrivateConfigError, "hash"):
                load_purchased_gergo(source)

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("source/../../escape")
                payload = b"bad"
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            with self.assertRaisesRegex(StagingError, "unsafe path"):
                extract_source(archive, root / "output")

    def test_safe_repository_symlink_is_skipped_not_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                regular = tarfile.TarInfo("source/target.txt")
                payload = b"ok"
                regular.size = len(payload)
                bundle.addfile(regular, io.BytesIO(payload))
                link = tarfile.TarInfo("source/docs/alias.txt")
                link.type = tarfile.SYMTYPE
                link.linkname = "../target.txt"
                bundle.addfile(link)
            output = root / "output"
            extract_source(archive, output)
            self.assertEqual((output / "target.txt").read_bytes(), b"ok")
            self.assertFalse((output / "docs/alias.txt").exists())

    def test_compiled_web_release_extracts_and_validates_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = web_release_fixture(root)
            output = root / "www"
            extract_web_release(archive, output, "v2.18.2")
            self.assertEqual((output / ".version").read_text().strip(), "v2.18.2")
            self.assertTrue((output / "assets/index.js").is_file())
            self.assertEqual((output / "assets/index.js").stat().st_mode & 0o777, 0o644)

    def test_web_release_rejects_traversal_and_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape", "bad")
            output = root / "www"
            with self.assertRaisesRegex(StagingError, "unsafe path"):
                extract_web_release(archive, output, "v2.18.2")
            self.assertFalse(output.exists())

    def test_web_release_rejects_symlink_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            link = zipfile.ZipInfo("index.html")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(link, "target")
            output = root / "www"
            with self.assertRaisesRegex(StagingError, "link or special"):
                extract_web_release(archive, output, "v2.18.2")
            self.assertFalse(output.exists())

    def test_web_release_rejects_development_source_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = web_release_fixture(
                root,
                index=(
                    '<script type="module" src="/src/main.ts"></script>'
                    '<link rel="stylesheet" href="/assets/index.css">'
                ),
            )
            output = root / "www"
            with self.assertRaisesRegex(StagingError, "compiled JavaScript"):
                extract_web_release(archive, output, "v2.18.2")
            self.assertFalse(output.exists())

    def test_recovery_overlay_is_forced_command_only_and_root_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_key = root / "recovery.pub"
            fingerprint = ed25519_public_key(public_key)
            output = root / "recovery-overlay"
            manifest_path = stage_recovery_overlay(ROOT, output, public_key)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["metadata"]["network_access"], "forced-command-only")
            self.assertEqual(
                manifest["metadata"]["recovery_public_key_fingerprint"], fingerprint
            )
            expected_modes = {
                "etc/ssh/sshd_config_t300_recovery": 0o600,
                "etc/systemd/system/ssh.service.d/20-t300-recovery.conf": 0o644,
                "etc/t300-recovery-authorized_keys": 0o400,
                "etc/t300-recovery.json": 0o600,
                "usr/local/sbin/t300-recovery-agent": 0o700,
                "usr/local/sbin/t300-recovery-ssh-gate": 0o700,
            }
            for relative, mode in expected_modes.items():
                with self.subTest(path=relative):
                    self.assertEqual((output / relative).stat().st_mode & 0o777, mode)
            authorized = (output / "etc/t300-recovery-authorized_keys").read_text(
                encoding="ascii"
            )
            self.assertIn(
                'restrict,no-user-rc,command="/usr/local/sbin/t300-recovery-ssh-gate"',
                authorized,
            )
            self.assertNotIn("recovery-laptop", authorized)
            self.assertIn("ForceCommand /usr/local/sbin/t300-recovery-ssh-gate", (
                output / "etc/ssh/sshd_config_t300_recovery"
            ).read_text(encoding="ascii"))
            override = (
                output
                / "etc/systemd/system/ssh.service.d/20-t300-recovery.conf"
            ).read_text(encoding="ascii")
            self.assertIn(
                "sshd -D -f /etc/ssh/sshd_config_t300_recovery", override
            )

    def test_recovery_overlay_failure_removes_partial_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_key = root / "recovery.pub"
            ed25519_public_key(public_key)
            output = root / "recovery-overlay"
            with mock.patch(
                "t300_mainline.staging._render", side_effect=StagingError("render failed")
            ):
                with self.assertRaisesRegex(StagingError, "render failed"):
                    stage_recovery_overlay(ROOT, output, public_key)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".recovery-overlay.partial-*")), [])


if __name__ == "__main__":
    unittest.main()

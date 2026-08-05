from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from t300_mainline.recovery_media import (
    RecoveryMediaError,
    audit_recovery_boot,
    audit_recovery_overlay,
    parse_armbian_env,
    render_recovery_env,
)


ROOT = Path(__file__).resolve().parents[1]


class RecoveryMediaTests(unittest.TestCase):
    def _overlay(self, root: Path) -> tuple[Path, str]:
        overlay = root / "overlay"
        records = []
        modes = {
            "etc/ssh/sshd_config_t300_recovery": 0o600,
            "etc/systemd/system/ssh.service.d/20-t300-recovery.conf": 0o644,
            "etc/t300-recovery-authorized_keys": 0o400,
            "etc/t300-recovery.json": 0o600,
            "usr/local/sbin/t300-recovery-agent": 0o700,
            "usr/local/sbin/t300-recovery-ssh-gate": 0o700,
        }
        for index, (name, mode) in enumerate(modes.items()):
            path = overlay / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("file-%d" % index).encode("ascii"))
            path.chmod(mode)
            records.append(
                {
                    "mode": oct(mode),
                    "path": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
            )
        manifest = overlay / "stage.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "metadata": {
                        "purpose": "marked T300 USB recovery overlay",
                        "recovery_public_key_fingerprint": "SHA256:test",
                    },
                    "files": records,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return overlay, hashlib.sha256(manifest.read_bytes()).hexdigest()

    def test_parser_rejects_duplicate_or_malformed_assignments(self):
        self.assertEqual(parse_armbian_env("rootdev=UUID=test\n"), {"rootdev": "UUID=test"})
        for value in ("rootdev=x\nrootdev=y\n", "not-an-assignment\n", "empty=\n"):
            with self.subTest(value=value), self.assertRaises(RecoveryMediaError):
                parse_armbian_env(value)

    def test_renderer_adds_only_locked_device_tree_to_local_file(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        policy = lock["recovery_boot"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "armbianEnv.txt"
            source.write_text(
                "console=both\nrootdev=UUID=%s\n" % policy["root_uuid"],
                encoding="ascii",
            )
            output = root / "reviewed-armbianEnv.txt"
            result = render_recovery_env(source, output, ROOT / "stack.lock.json")
            self.assertIn("fdtfile=" + policy["fdtfile"], output.read_text(encoding="ascii"))
            self.assertTrue(result["requires_owner_copy_to_usb"])
            self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_renderer_refuses_unexpected_root_or_device_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "armbianEnv.txt"
            output = root / "output"
            for text in (
                "rootdev=UUID=wrong\n",
                "rootdev=UUID=3a703405-2025-4c62-aae4-7fb9accdb996\nfdtfile=wrong.dtb\n",
            ):
                source.write_text(text, encoding="ascii")
                with self.subTest(text=text), self.assertRaises(RecoveryMediaError):
                    render_recovery_env(source, output, ROOT / "stack.lock.json")

    def test_audit_binds_all_boot_files_and_requires_explicit_fdt(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        policy = lock["recovery_boot"]
        with tempfile.TemporaryDirectory() as directory:
            boot = Path(directory)
            files = {
                "Image": (b"image", "image_sha256"),
                "uInitrd": (b"initrd", "uinitrd_sha256"),
                "boot.cmd": (b"command", "boot_cmd_sha256"),
                "boot.scr": (b"script", "boot_scr_sha256"),
                "dtb/rockchip/rk3328-mksklipad50.dtb": (b"dtb", "dtb_sha256"),
            }
            for name, (payload, key) in files.items():
                path = boot / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                policy[key] = hashlib.sha256(payload).hexdigest()
            custom_lock = boot / "lock.json"
            lock["recovery_boot"] = policy
            custom_lock.write_text(json.dumps(lock), encoding="utf-8")
            env = boot / "armbianEnv.txt"
            env.write_text(
                "console=both\nrootdev=UUID=%s\n" % policy["root_uuid"],
                encoding="ascii",
            )
            first = audit_recovery_boot(boot, custom_lock)
            self.assertFalse(first["ready_for_interactive_usb_boot"])
            self.assertIn("fdtfile", first["failures"])
            env.write_text(
                "console=both\nrootdev=UUID=%s\nfdtfile=%s\n"
                % (policy["root_uuid"], policy["fdtfile"]),
                encoding="ascii",
            )
            second = audit_recovery_boot(boot, custom_lock)
            self.assertTrue(second["ready_for_interactive_usb_boot"])

    def test_overlay_audit_binds_source_and_installed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overlay, digest = self._overlay(root)
            self.assertTrue(audit_recovery_overlay(overlay, digest)["ready"])
            installed = root / "installed"
            for source in overlay.rglob("*"):
                if source.is_file() and source.name != "stage.manifest.json":
                    destination = installed / source.relative_to(overlay)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
                    destination.chmod(source.stat().st_mode & 0o777)
            self.assertTrue(
                audit_recovery_overlay(overlay, digest, installed)["ready"]
            )
            (installed / "etc/t300-recovery.json").write_text(
                "tampered\n", encoding="ascii"
            )
            result = audit_recovery_overlay(overlay, digest, installed)
            self.assertFalse(result["ready"])
            self.assertIn("etc/t300-recovery.json", result["installed_failures"])

    def test_overlay_audit_rejects_manifest_or_source_tree_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overlay, digest = self._overlay(root)
            with self.assertRaisesRegex(RecoveryMediaError, "SHA-256 does not match"):
                audit_recovery_overlay(overlay, "0" * 64)
            extra = overlay / "unexpected"
            extra.write_text("extra\n", encoding="ascii")
            result = audit_recovery_overlay(overlay, digest)
            self.assertFalse(result["ready"])
            self.assertIn("file-set", result["source_failures"])


if __name__ == "__main__":
    unittest.main()

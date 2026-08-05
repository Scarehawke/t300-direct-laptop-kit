from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from t300_mainline.imaging import (
    ImagingError,
    RecoveryClient,
    _check_inspection,
    _check_mounted_read_only,
    _validated_loop_partitions,
    capture_image,
    load_manifest,
    sha256_file,
    verify_image,
    verify_image_filesystems,
    write_image,
)


MACHINE_ID = "a" * 64


def inspection(size: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "board": "MKS-Klipad50",
        "safe_for_capture": True,
        "safe_for_write": True,
        "verified_usb_boots": 3,
        "machine_id": MACHINE_ID,
        "target_device": "/dev/mmcblk2",
        "target_size": size,
        "marker_id": "12345678-1234-4123-8123-123456789abc",
        "block_geometry": {
            "logical_sector_bytes": 512,
            "physical_sector_bytes": 512,
            "minimum_io_bytes": 512,
            "optimal_io_bytes": 0,
            "sectors_512": size // 512,
            "device_bytes": size,
        },
        "partition_table": {
            "device": "/dev/mmcblk2",
            "label": "gpt",
            "unit": "sectors",
            "sector_size": 512,
            "partitions": [{"node": "/dev/mmcblk2p1", "start": 1, "size": 1}],
        },
        "bootloader": {"device_tree_handoff_identifies_board": True},
        "emmc": {
            "non_removable": True,
            "card_type": "MMC",
            "boot0_present": True,
            "boot1_present": True,
            "identifies_emmc": True,
        },
        "blocked_reasons": [],
    }


class FakeRecoveryClient:
    def __init__(self, raw: bytes, fail_stream: bool = False) -> None:
        self.raw = raw
        self.fail_stream = fail_stream
        self.commands: list[tuple[str, ...]] = []

    def inspect(self, _device: str):
        return inspection(len(self.raw))

    def hash_device(self, _device: str):
        return {"size": len(self.raw), "sha256": hashlib.sha256(self.raw).hexdigest()}

    def command(self, *arguments: str) -> list[str]:
        self.commands.append(arguments)
        if arguments[0] == "stream":
            code = 1 if self.fail_stream else 0
            script = (
                "import sys; sys.stdout.buffer.write(%r); "
                "sys.stdout.flush(); raise SystemExit(%d)" % (self.raw, code)
            )
            return [sys.executable, "-c", script]
        if arguments[0] == "write":
            script = (
                "import hashlib,json,sys; d=sys.stdin.buffer.read(); "
                "print(json.dumps({'bytes_written':len(d),"
                "'image_sha256':hashlib.sha256(d).hexdigest()}))"
            )
            return [sys.executable, "-c", script]
        raise AssertionError(arguments)


class RecoverySshTests(unittest.TestCase):
    def test_command_uses_fixed_key_host_identity_and_one_quoted_remote_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "id_ed25519"
            known_hosts = root / "known_hosts"
            identity.write_text("private\n", encoding="ascii")
            identity.chmod(0o600)
            known_hosts.write_text("host ssh-ed25519 AAAA\n", encoding="ascii")
            with mock.patch(
                "t300_mainline.imaging._require_program", return_value="/usr/bin/ssh"
            ):
                client = RecoveryClient(
                    "root@10.42.42.2", identity, known_hosts
                )
            command = client.command(
                "write", "--confirm", "WRITE " + MACHINE_ID
            )
            self.assertEqual(command[0:3], ["/usr/bin/ssh", "-F", "/dev/null"])
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("IdentityFile=%s" % identity, command)
            self.assertEqual(command[-2], "10.42.42.2")
            self.assertEqual(
                shlex.split(command[-1]),
                [
                    "/usr/local/sbin/t300-recovery-agent",
                    "write",
                    "--confirm",
                    "WRITE " + MACHINE_ID,
                ],
            )

    def test_rejects_loose_private_key_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "id"
            known_hosts = root / "known"
            identity.write_text("private", encoding="ascii")
            identity.chmod(0o644)
            known_hosts.write_text("known", encoding="ascii")
            with self.assertRaisesRegex(ImagingError, "private to its owner"):
                RecoveryClient("10.42.42.2", identity, known_hosts)


class ImagingWorkflowTests(unittest.TestCase):
    def _captured_fixture(self, root: Path, raw: bytes):
        client = FakeRecoveryClient(raw)
        image = root / "stock.img.zst"
        manifest_path = capture_image(client, "/dev/mmcblk2", image)
        return client, image, manifest_path

    def test_capture_verify_and_guarded_fake_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b"T300 stock image fixture\0" * 4096
            client, image, manifest_path = self._captured_fixture(root, raw)
            manifest = verify_image(image, manifest_path)
            self.assertEqual(manifest["raw_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
            result = write_image(
                client,
                "/dev/mmcblk2",
                image,
                manifest_path,
                True,
                "WRITE " + MACHINE_ID,
            )
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            write_arguments = next(item for item in client.commands if item[0] == "write")
            self.assertIn("--image-sha256", write_arguments)

    def test_interrupted_capture_keeps_partial_and_never_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeRecoveryClient(b"partial".ljust(512, b"\0"), fail_stream=True)
            image = root / "stock.img.zst"
            with self.assertRaisesRegex(ImagingError, "capture interrupted"):
                capture_image(client, "/dev/mmcblk2", image)
            self.assertFalse(image.exists())
            self.assertFalse((root / "stock.img.zst.manifest.json").exists())
            partial = root / ".stock.img.zst.partial"
            self.assertTrue(partial.exists())
            self.assertEqual(partial.stat().st_mode & 0o777, 0o600)

    def test_write_requires_apply_before_any_image_or_network_work(self):
        client = mock.Mock()
        with self.assertRaisesRegex(ImagingError, "dry run"):
            write_image(
                client,
                "/dev/mmcblk2",
                Path("missing.img.zst"),
                Path("missing.json"),
                False,
                None,
            )
        client.assert_not_called()

    def test_manifest_and_image_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _client, image, manifest_path = self._captured_fixture(
                root, b"image".ljust(512, b"\0")
            )
            image_link = root / "image-link.zst"
            image_link.symlink_to(image)
            with self.assertRaisesRegex(ImagingError, "non-symlink"):
                verify_image(image_link, manifest_path)
            manifest_link = root / "manifest-link.json"
            manifest_link.symlink_to(manifest_path)
            with self.assertRaisesRegex(ImagingError, "non-symlink"):
                load_manifest(manifest_link)

    def test_inspection_requires_strict_booleans_boot_count_and_target_size(self):
        value = inspection(1024)
        _check_inspection(value, "capture")
        for key, bad in (
            ("safe_for_capture", "yes"),
            ("verified_usb_boots", True),
            ("target_size", "1024"),
        ):
            with self.subTest(key=key):
                changed = dict(value)
                changed[key] = bad
                with self.assertRaises(ImagingError):
                    _check_inspection(changed, "capture")

        changed = dict(value)
        changed["block_geometry"] = dict(value["block_geometry"])
        changed["block_geometry"]["sectors_512"] = 1
        with self.assertRaisesRegex(ImagingError, "geometry"):
            _check_inspection(changed, "capture")

    def test_loop_layout_must_match_manifest_and_remain_read_only(self):
        size = 4096
        manifest = {"raw_size": size, "machine": inspection(size)}
        layout = {
            "blockdevices": [
                {
                    "path": "/dev/loop7",
                    "type": "loop",
                    "fstype": None,
                    "ro": True,
                    "size": size,
                    "mountpoints": [None],
                    "children": [
                        {
                            "path": "/dev/loop7p1",
                            "type": "part",
                            "fstype": "ext4",
                            "ro": True,
                            "size": 512,
                            "mountpoints": [None],
                        }
                    ],
                }
            ]
        }
        partitions = _validated_loop_partitions(layout, "/dev/loop7", manifest)
        self.assertEqual(partitions[0]["path"], "/dev/loop7p1")
        layout["blockdevices"][0]["children"][0]["ro"] = False
        with self.assertRaisesRegex(ImagingError, "writable"):
            _validated_loop_partitions(layout, "/dev/loop7", manifest)

    def test_findmnt_confirmation_requires_same_read_only_partition(self):
        valid = json.dumps(
            {
                "filesystems": [
                    {"source": "/dev/loop7p1", "options": "ro,nosuid,nodev"}
                ]
            }
        )
        _check_mounted_read_only(valid, "/dev/loop7p1")
        with self.assertRaisesRegex(ImagingError, "not sourced read-only"):
            _check_mounted_read_only(
                valid.replace("ro,nosuid", "rw,nosuid"), "/dev/loop7p1"
            )

    @mock.patch("t300_mainline.imaging.os.geteuid", return_value=1000)
    def test_filesystem_verification_refuses_non_root_before_opening_image(self, _geteuid):
        with self.assertRaisesRegex(ImagingError, "requires root"):
            verify_image_filesystems(
                Path("missing.img.zst"), Path("missing.json"), Path("/tmp")
            )


if __name__ == "__main__":
    unittest.main()

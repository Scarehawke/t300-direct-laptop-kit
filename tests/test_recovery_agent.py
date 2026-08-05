from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE_ID = "a" * 64


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load %s" % relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecoverySshGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = load_script("t300_recovery_ssh_gate_test", "bin/t300-recovery-ssh-gate.py")

    def test_gate_preserves_one_quoted_confirmation_and_cleans_environment(self):
        gate = self.gate
        command = (
            "/usr/local/sbin/t300-recovery-agent write --device /dev/mmcblk2 "
            "--image-size 1024 --image-sha256 %s --apply --confirm 'WRITE %s'"
            % ("b" * 64, MACHINE_ID)
        )
        agent_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o700, st_uid=0)
        with mock.patch.dict(gate.os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True), mock.patch.object(
            gate.Path, "lstat", return_value=agent_info
        ), mock.patch.object(gate.os, "execve", side_effect=RuntimeError("exec")) as execute:
            with self.assertRaisesRegex(RuntimeError, "exec"):
                gate.main()
        arguments = execute.call_args.args[1]
        self.assertEqual(arguments[-1], "WRITE " + MACHINE_ID)
        self.assertEqual(
            execute.call_args.args[2],
            {
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )

    def test_gate_rejects_shell_and_unknown_commands(self):
        gate = self.gate
        for command in (
            "/bin/sh -c id",
            "/usr/local/sbin/t300-recovery-agent reboot",
            "",
            "/usr/local/sbin/t300-recovery-agent inspect\n/bin/id",
        ):
            with self.subTest(command=command), mock.patch.dict(
                gate.os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True
            ), self.assertRaises(SystemExit):
                gate.main()

    def test_gate_rejects_replaceable_agent(self):
        gate = self.gate
        command = "/usr/local/sbin/t300-recovery-agent inspect --device /dev/mmcblk2"
        unsafe = SimpleNamespace(st_mode=stat.S_IFREG | 0o722, st_uid=1000)
        with mock.patch.dict(gate.os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True), mock.patch.object(
            gate.Path, "lstat", return_value=unsafe
        ), self.assertRaises(SystemExit):
            gate.main()


class RecoveryAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = load_script("t300_recovery_agent_test", "bin/t300-recovery-agent.py")

    def test_machine_identity_requires_exact_real_cid(self):
        agent = self.agent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cid_path = root / "mmcblk2/device/cid"
            cid_path.parent.mkdir(parents=True)
            with self.assertRaisesRegex(agent.RecoveryError, "no readable CID"):
                agent._machine_identity("MKS-Klipad50", "klipad50", "/dev/mmcblk3", 1024, root)
            cid_path.write_text("not-a-cid\n", encoding="ascii")
            with self.assertRaisesRegex(agent.RecoveryError, "malformed"):
                agent._machine_identity("MKS-Klipad50", "klipad50", "/dev/mmcblk2", 1024, root)
            cid_path.write_text("0123456789abcdef0123456789abcdef\n", encoding="ascii")
            identity = agent._machine_identity(
                "MKS-Klipad50", "klipad50", "/dev/mmcblk2", 1024, root
            )
            self.assertRegex(identity, r"^[0-9a-f]{64}$")

    def test_emmc_evidence_requires_fixed_mmc_and_both_boot_partitions(self):
        agent = self.agent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "mmcblk1"
            (target / "device").mkdir(parents=True)
            (target / "removable").write_text("0\n", encoding="ascii")
            (target / "device/type").write_text("MMC\n", encoding="ascii")
            (root / "mmcblk1boot0").mkdir()
            (root / "mmcblk1boot1").mkdir()
            evidence = agent._emmc_evidence("/dev/mmcblk1", root)
            self.assertTrue(evidence["identifies_emmc"])
            (target / "removable").write_text("1\n", encoding="ascii")
            self.assertFalse(
                agent._emmc_evidence("/dev/mmcblk1", root)["identifies_emmc"]
            )
            (target / "removable").write_text("0\n", encoding="ascii")
            (target / "device/type").write_text("SD\n", encoding="ascii")
            self.assertFalse(
                agent._emmc_evidence("/dev/mmcblk1", root)["identifies_emmc"]
            )
            (target / "device/type").write_text("MMC\n", encoding="ascii")
            (root / "mmcblk1boot1").rmdir()
            self.assertFalse(
                agent._emmc_evidence("/dev/mmcblk1", root)["identifies_emmc"]
            )

    def _write_fixture(self, incoming: bytes, expected_size: int, expected_hash: str):
        agent = self.agent
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "device"
            target.write_bytes(b"\0" * expected_size)
            inspection = {"target_size": expected_size, "machine_id": MACHINE_ID}
            fake_stdin = SimpleNamespace(buffer=io.BytesIO(incoming))
            with mock.patch.object(agent, "_require_safe", return_value=inspection), mock.patch.object(
                agent.sys, "stdin", fake_stdin
            ), mock.patch.object(agent, "_run", return_value=""):
                result = agent.write_device(
                    str(target),
                    expected_size,
                    expected_hash,
                    True,
                    "WRITE " + MACHINE_ID,
                )
            return result, target.read_bytes()

    def test_write_accepts_only_exact_size_and_hash(self):
        payload = b"verified recovery image"
        digest = hashlib.sha256(payload).hexdigest()
        result, written = self._write_fixture(payload, len(payload), digest)
        self.assertEqual(written, payload)
        self.assertEqual(result["bytes_written"], len(payload))
        self.assertEqual(result["image_sha256"], digest)

    def test_write_rejects_short_long_and_changed_streams(self):
        agent = self.agent
        cases = (
            (b"short", 10, hashlib.sha256(b"short").hexdigest(), "ended before"),
            (b"one-byte-extra", 13, hashlib.sha256(b"one-byte-extr").hexdigest(), "larger"),
            (b"same-size", 9, "0" * 64, "changed during transfer"),
        )
        for incoming, size, digest, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                agent.RecoveryError, message
            ):
                self._write_fixture(incoming, size, digest)

    def test_write_refuses_without_apply_or_exact_confirmation(self):
        agent = self.agent
        inspection = {"target_size": 4, "machine_id": MACHINE_ID}
        with mock.patch.object(agent, "_require_safe", return_value=inspection):
            with self.assertRaisesRegex(agent.RecoveryError, "requires --apply"):
                agent.write_device("/dev/mmcblk2", 4, "0" * 64, False, None)
            with self.assertRaisesRegex(agent.RecoveryError, "typed confirmation"):
                agent.write_device("/dev/mmcblk2", 4, "0" * 64, True, "WRITE wrong")

    def test_malformed_mount_data_and_zero_size_fail_closed(self):
        agent = self.agent
        with mock.patch.object(
            agent, "_run", return_value='{"blockdevices":[{"mountpoints":"/"}]}'
        ), self.assertRaisesRegex(agent.RecoveryError, "mountpoint"):
            agent._mounted_nodes("/dev/mmcblk2")
        with mock.patch.object(agent, "_run", return_value="0"), self.assertRaisesRegex(
            agent.RecoveryError, "invalid size"
        ):
            agent._block_size("/dev/mmcblk2")

    def test_block_geometry_is_bound_to_exact_device_size(self):
        agent = self.agent
        responses = {
            "--getss": "512",
            "--getpbsz": "4096",
            "--getiomin": "4096",
            "--getioopt": "0",
            "--getsz": "8",
        }

        def blockdev(_program, option, _device):
            return responses[option]

        with mock.patch.object(agent, "_run", side_effect=blockdev):
            geometry = agent._block_geometry("/dev/mmcblk2", 4096)
        self.assertEqual(geometry["physical_sector_bytes"], 4096)
        self.assertEqual(geometry["device_bytes"], 4096)
        responses["--getsz"] = "7"
        with mock.patch.object(agent, "_run", side_effect=blockdev), self.assertRaisesRegex(
            agent.RecoveryError, "sector count"
        ):
            agent._block_geometry("/dev/mmcblk2", 4096)

    def test_partition_table_is_normalized_and_bounds_checked(self):
        agent = self.agent
        table = {
            "partitiontable": {
                "device": "/dev/mmcblk2",
                "label": "gpt",
                "unit": "sectors",
                "sectorsize": 512,
                "partitions": [
                    {
                        "node": "/dev/mmcblk2p1",
                        "start": 1,
                        "size": 4,
                        "type": "linux",
                    }
                ],
            }
        }
        with mock.patch.object(agent, "_run", return_value=json.dumps(table)):
            value = agent._partition_table("/dev/mmcblk2", 4096)
        self.assertEqual(value["partitions"][0]["size"], 4)
        table["partitiontable"]["partitions"][0]["size"] = 20
        with mock.patch.object(agent, "_run", return_value=json.dumps(table)), self.assertRaisesRegex(
            agent.RecoveryError, "outside"
        ):
            agent._partition_table("/dev/mmcblk2", 4096)


if __name__ == "__main__":
    unittest.main()

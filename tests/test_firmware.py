from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import struct
import tempfile
import unittest

from t300_mainline.firmware import (
    FirmwareError,
    build_firmware,
    load_firmware_inputs,
    write_source_version,
)
from t300_mainline.lockfile import load_lock


ROOT = Path(__file__).resolve().parents[1]


class FirmwareTests(unittest.TestCase):
    def test_repository_firmware_inputs_match_lock_and_vendor_facts(self):
        lock = load_lock(ROOT / "stack.lock.json")
        value = load_firmware_inputs(ROOT / "mainline/firmware", lock)
        controller = value["provenance"]["builds"]["controller"]
        self.assertEqual(value["version"], "v0.13.0")
        self.assertEqual(controller["mcu"], "stm32f401xc")
        self.assertEqual(controller["application_address"], "0x08008000")
        self.assertEqual(controller["clock_reference_hz"], 8000000)
        self.assertTrue(
            value["provenance"]["dfu_evidence_only"]["deployment_status"].startswith(
                "blocked until"
            )
        )

    def test_tampered_firmware_config_is_rejected(self):
        lock = load_lock(ROOT / "stack.lock.json")
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "firmware"
            shutil.copytree(ROOT / "mainline/firmware", copied)
            with (copied / "stm32f401.config").open("a", encoding="ascii") as handle:
                handle.write("# changed\n")
            with self.assertRaisesRegex(FirmwareError, "does not match"):
                load_firmware_inputs(copied, lock)

    def test_source_version_is_one_time_and_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            marker = write_source_version(source, "v0.13.0")
            self.assertEqual(marker.read_text(encoding="ascii"), "v0.13.0\n")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o444)
            with self.assertRaisesRegex(FirmwareError, "unexpectedly contains"):
                write_source_version(source, "v0.13.0")

    def test_build_prepares_both_artifacts_without_flash_capability(self):
        lock = load_lock(ROOT / "stack.lock.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "klipper"
            source.mkdir()
            write_source_version(source, "v0.13.0")
            output = root / "firmware-output"

            def run(command, **_kwargs):
                if command[0] == "/usr/bin/readelf":
                    machine = "AArch64" if "linux_host" in command[-1] else "ARM"
                    return SimpleNamespace(stdout="  Machine: %s\n" % machine)
                if command[0] != "/usr/bin/make" or command[-1] == "olddefconfig":
                    return SimpleNamespace(stdout="")
                config = Path(next(item.split("=", 1)[1] for item in command if item.startswith("KCONFIG_CONFIG=")))
                build_output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("OUT=")))
                build_output.mkdir(parents=True, exist_ok=True)
                linux = "CONFIG_MACH_LINUX=y" in config.read_text(encoding="ascii")
                mcu = "linux" if linux else "stm32f401xc"
                dictionary = {
                    "app": "Klipper",
                    "license": "GNU GPLv3",
                    "version": "v0.13.0",
                    "build_versions": "gcc: test binutils: test",
                    "config": {"MCU": mcu},
                }
                (build_output / "klipper.dict").write_text(
                    json.dumps(dictionary), encoding="utf-8"
                )
                (build_output / "klipper.elf").write_bytes(b"ELF-test")
                if not linux:
                    image = bytearray(4096)
                    struct.pack_into("<II", image, 0, 0x20010000, 0x08008101)
                    (build_output / "klipper.bin").write_bytes(image)
                return SimpleNamespace(stdout="")

            manifest = build_firmware(
                source,
                ROOT / "mainline/firmware",
                output,
                lock,
                run,
            )
            self.assertIs(manifest["flash_capability"], False)
            self.assertEqual(set(manifest["builds"]), {"controller", "linux_host"})
            self.assertTrue((output / "controller/klipper.bin").is_file())
            self.assertTrue((output / "linux-host/klipper_mcu").is_file())
            recorded = json.loads(
                (output / "firmware.manifest.json").read_text(encoding="utf-8")
            )
            self.assertIs(recorded["flash_capability"], False)


if __name__ == "__main__":
    unittest.main()

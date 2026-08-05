"""Verified, non-flashing Klipper firmware preparation for the stock T300."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Any, Callable

from .lockfile import sha256_file


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
RunCommand = Callable[..., Any]


class FirmwareError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirmwareError("firmware provenance is unreadable") from exc
    if not isinstance(value, dict):
        raise FirmwareError("firmware provenance root must be an object")
    return value


def _klipper_identity(stack: dict[str, Any]) -> tuple[str, str]:
    matches = [item for item in stack.get("components", []) if item.get("name") == "klipper"]
    if len(matches) != 1:
        raise FirmwareError("stack must lock exactly one Klipper component")
    version, commit = matches[0].get("version"), matches[0].get("commit")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise FirmwareError("locked Klipper version is malformed")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise FirmwareError("locked Klipper commit is malformed")
    return version, commit


def _parse_kconfig(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise FirmwareError("firmware Kconfig must be readable ASCII") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise FirmwareError("firmware Kconfig contains a duplicate key")
        values[key] = value
    return values


def load_firmware_inputs(root: Path, stack: dict[str, Any]) -> dict[str, Any]:
    """Bind reviewed configs and provenance to the locked Klipper source."""
    requested_root = root.expanduser().absolute()
    if requested_root.is_symlink():
        raise FirmwareError("firmware input root must be one real directory")
    root = requested_root.resolve(strict=True)
    if not root.is_dir():
        raise FirmwareError("firmware input root must be one real directory")
    provenance_path = root / "provenance.json"
    if provenance_path.is_symlink() or not provenance_path.is_file():
        raise FirmwareError("firmware provenance is missing or unsafe")
    provenance = _read_json(provenance_path)
    firmware_lock = stack.get("firmware")
    if (
        not isinstance(firmware_lock, dict)
        or firmware_lock.get("provenance_path") != "mainline/firmware/provenance.json"
        or firmware_lock.get("provenance_sha256") != sha256_file(provenance_path)
        or firmware_lock.get("build_count") != 2
        or firmware_lock.get("flash_capability") is not False
    ):
        raise FirmwareError("firmware provenance differs from stack.lock.json")
    version, commit = _klipper_identity(stack)
    if provenance.get("schema_version") != 1 or provenance.get("klipper") != {
        "version": version,
        "commit": commit,
    }:
        raise FirmwareError("firmware provenance targets another Klipper source")

    builds = provenance.get("builds")
    if not isinstance(builds, dict) or set(builds) != {"controller", "linux_host"}:
        raise FirmwareError("firmware provenance must define both reviewed builds")
    expected = {
        "controller": {
            "filename": "stm32f401.config",
            "facts": {
                "CONFIG_MACH_STM32": "y",
                "CONFIG_MCU": '"stm32f401xc"',
                "CONFIG_CLOCK_FREQ": "84000000",
                "CONFIG_CLOCK_REF_FREQ": "8000000",
                "CONFIG_FLASH_SIZE": "0x40000",
                "CONFIG_FLASH_APPLICATION_ADDRESS": "0x8008000",
                "CONFIG_STM32_USB_PA11_PA12": "y",
                "CONFIG_USB": "y",
            },
        },
        "linux_host": {
            "filename": "linux-host.config",
            "facts": {
                "CONFIG_MACH_LINUX": "y",
                "CONFIG_BOARD_DIRECTORY": '"linux"',
                "CONFIG_CLOCK_FREQ": "50000000",
                "CONFIG_HAVE_GPIO_SPI": "y",
            },
        },
    }
    configs: dict[str, Path] = {}
    for name, requirement in expected.items():
        build = builds[name]
        if not isinstance(build, dict) or build.get("config") != requirement["filename"]:
            raise FirmwareError("firmware build names an unexpected Kconfig")
        digest = build.get("config_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise FirmwareError("firmware Kconfig hash is malformed")
        config = root / requirement["filename"]
        if config.is_symlink() or not config.is_file() or sha256_file(config) != digest:
            raise FirmwareError("firmware Kconfig does not match its provenance")
        values = _parse_kconfig(config)
        for key, value in requirement["facts"].items():
            if values.get(key) != value:
                raise FirmwareError("firmware Kconfig lost required fact %s" % key)
        configs[name] = config

    evidence = provenance.get("vendor_evidence")
    dfu = provenance.get("dfu_evidence_only")
    if not isinstance(evidence, dict) or not all(
        isinstance(evidence.get(key), str) and SHA256_RE.fullmatch(evidence[key])
        for key in (
            "full_image_sha256",
            "application_package_sha256",
            "vendor_stm32_config_sha256",
            "vendor_linux_config_sha256",
            "vendor_recovery_firmware_sha256",
            "vendor_update_script_sha256",
        )
    ):
        raise FirmwareError("vendor firmware evidence hashes are incomplete")
    if not isinstance(dfu, dict) or not str(dfu.get("deployment_status", "")).startswith(
        "blocked until"
    ):
        raise FirmwareError("DFU path must remain explicitly blocked")
    return {
        "root": root,
        "provenance": provenance,
        "provenance_sha256": sha256_file(provenance_path),
        "version": version,
        "commit": commit,
        "configs": configs,
    }


def write_source_version(source: Path, version: str) -> Path:
    """Add the immutable version file expected by archive-based Klipper builds."""
    if not VERSION_RE.fullmatch(version):
        raise FirmwareError("Klipper source version is malformed")
    target = source / ".version"
    if target.exists() or target.is_symlink():
        raise FirmwareError("Klipper source archive unexpectedly contains .version")
    target.write_text(version + "\n", encoding="ascii")
    target.chmod(0o444)
    return target


def _dictionary(path: Path, expected_mcu: str, version: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirmwareError("built Klipper dictionary is unreadable") from exc
    config = value.get("config")
    if (
        value.get("version") != version
        or value.get("app") != "Klipper"
        or value.get("license") != "GNU GPLv3"
        or not isinstance(config, dict)
        or config.get("MCU") != expected_mcu
    ):
        raise FirmwareError("built Klipper dictionary has the wrong identity")
    return value


def _copy_artifact(source: Path, destination: Path, mode: int) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file():
        raise FirmwareError("firmware build output is missing or unsafe: %s" % source.name)
    shutil.copy2(source, destination)
    destination.chmod(mode)
    return {
        "path": destination.name,
        "size": destination.stat().st_size,
        "mode": oct(mode),
        "sha256": sha256_file(destination),
    }


def build_firmware(
    source: Path,
    inputs: Path,
    destination: Path,
    stack: dict[str, Any],
    run: RunCommand,
) -> dict[str, Any]:
    """Build and inspect both MCU artifacts. This function cannot flash them."""
    info = load_firmware_inputs(inputs, stack)
    requested_source = source.expanduser().absolute()
    if requested_source.is_symlink():
        raise FirmwareError("Klipper source must be one real directory")
    source = requested_source.resolve(strict=True)
    if not source.is_dir():
        raise FirmwareError("Klipper source must be one real directory")
    try:
        source_version = (source / ".version").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise FirmwareError("Klipper archive version marker is missing") from exc
    if source_version != info["version"] + "\n":
        raise FirmwareError("Klipper archive version marker differs from the lock")
    if destination.exists() or destination.is_symlink():
        raise FirmwareError("firmware output directory already exists")

    temporary_destination = destination.with_name(".%s.partial" % destination.name)
    if temporary_destination.exists() or temporary_destination.is_symlink():
        raise FirmwareError("stale partial firmware output requires inspection")
    temporary_destination.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix="t300-firmware-") as directory:
            build_root = Path(directory)
            results: dict[str, Any] = {}
            specifications = (
                ("controller", "stm32f401xc", "klipper.bin"),
                ("linux_host", "linux", "klipper.elf"),
            )
            for name, expected_mcu, primary_output in specifications:
                work = build_root / name
                output = work / "out"
                work.mkdir()
                config = work / ".config"
                shutil.copy2(info["configs"][name], config)
                config.chmod(0o600)
                common = [
                    "/usr/bin/make",
                    "-j1",
                    "KCONFIG_CONFIG=%s" % config,
                    "OUT=%s/" % output,
                ]
                run([*common, "olddefconfig"], cwd=source, timeout=180, offline=True)
                expected_hash = info["provenance"]["builds"][name]["config_sha256"]
                if sha256_file(config) != expected_hash:
                    raise FirmwareError(
                        "pinned Klipper changed the reviewed %s Kconfig" % name
                    )
                run(common, cwd=source, timeout=1800, offline=True)
                dictionary = _dictionary(output / "klipper.dict", expected_mcu, info["version"])
                target = temporary_destination / name.replace("_", "-")
                target.mkdir()
                artifact_records = [
                    _copy_artifact(
                        output / primary_output,
                        target / ("klipper_mcu" if name == "linux_host" else primary_output),
                        0o555 if name == "linux_host" else 0o444,
                    ),
                    _copy_artifact(output / "klipper.dict", target / "klipper.dict", 0o444),
                ]
                elf = output / "klipper.elf"
                readelf = run(
                    ["/usr/bin/readelf", "-h", str(elf)],
                    timeout=30,
                    offline=True,
                ).stdout
                machine = "AArch64" if name == "linux_host" else "ARM"
                if re.search(r"Machine:\s+%s(?:\s|$)" % machine, readelf) is None:
                    raise FirmwareError("built %s ELF has the wrong architecture" % name)
                if name == "controller":
                    binary = output / "klipper.bin"
                    data = binary.read_bytes()
                    maximum = info["provenance"]["builds"][name]["maximum_application_bytes"]
                    if not 4096 <= len(data) <= maximum:
                        raise FirmwareError("controller firmware size is outside flash bounds")
                    stack_pointer, reset_handler = struct.unpack_from("<II", data)
                    if stack_pointer != 0x20010000 or not (
                        0x08008001 <= reset_handler < 0x08040000 and reset_handler & 1
                    ):
                        raise FirmwareError("controller vector table contradicts the hardware map")
                    artifact_records.append(
                        _copy_artifact(elf, target / "klipper.elf", 0o444)
                    )
                    vector = {
                        "initial_stack_pointer": "0x%08x" % stack_pointer,
                        "reset_handler": "0x%08x" % reset_handler,
                    }
                else:
                    vector = None
                results[name] = {
                    "config_sha256": sha256_file(config),
                    "dictionary_sha256": sha256_file(output / "klipper.dict"),
                    "build_tools": dictionary.get("build_versions"),
                    "version": dictionary["version"],
                    "vector_table": vector,
                    "artifacts": artifact_records,
                }
        manifest = {
            "schema_version": 1,
            "flash_capability": False,
            "klipper_version": info["version"],
            "klipper_commit": info["commit"],
            "provenance_sha256": info["provenance_sha256"],
            "builds": results,
        }
        manifest_path = temporary_destination / "firmware.manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_path.chmod(0o444)
        os.replace(temporary_destination, destination)
        return manifest
    except BaseException:
        shutil.rmtree(temporary_destination, ignore_errors=True)
        raise

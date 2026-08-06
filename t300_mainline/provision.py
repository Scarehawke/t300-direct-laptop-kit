"""Fail-closed provisioning of a live T300 USB candidate.

This module intentionally cannot target eMMC or an arbitrary mounted root. It
runs only from the exact signed Armbian base while that base is booted from a
removable USB disk. A failed candidate is rebuilt from the signed image; it is
never repaired in place and no printer-control unit is enabled here.
"""

from __future__ import annotations

import argparse
import datetime as dt
from email.parser import Parser
import grp
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import zipfile

from .debian_artifacts import load_debian_lock
from .firmware import FirmwareError, build_firmware
from .lockfile import load_lock, sha256_file
from .python_artifacts import load_artifact_lock


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ROOT_SOURCE_RE = re.compile(r"^/dev/(sd[a-z]+)[0-9]+$")
EXPECTED_ARMBIAN = {
    "BOARD": "mksklipad50",
    "BOARDFAMILY": "rockchip64",
    "BOOT_SOC": "rk3328",
    "ARCH": "arm64",
    "DISTRIBUTION_CODENAME": "trixie",
}
SERVICE_USERS = (
    "klipper",
    "moonraker",
    "crowsnest",
    "t300-touchscreen",
    "mainsail",
    "t300-policy",
    "t300-deploy",
    "t300-host-mcu",
)
SERVICE_GROUPS = (
    "t300-comms",
    "t300-gcode",
    *SERVICE_USERS,
)
T300_UNITS = (
    "klipper.service",
    "klipper-maintenance.service",
    "moonraker.service",
    "t300-admission.service",
    "crowsnest.service",
    "mainsail.service",
    "t300-touchscreen-gateway.service",
    "t300-touchscreen-bridge.service",
    "t300-host-mcu.service",
    r"mnt-t300\x2ddata.mount",
    r"var-lib-t300-moonraker\x2ddata-gcodes.mount",
)
ALLOWED_STAGE_PREFIXES = (
    "etc/systemd/system/",
    "etc/systemd/journald.conf.d/",
    "etc/ssh/sshd_config.d/",
    "etc/udev/rules.d/",
    "etc/t300/",
    "opt/t300/",
)
JOURNAL = Path("/var/lib/t300-mainline-provision.json")
MIB = 1024 * 1024
MIN_POST_PROVISION_FREE_BYTES = 1024 * MIB
BUILD_WORKSPACE_BYTES = 768 * MIB
MIN_FREE_FRACTION = 8


class ProvisionError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("could not read JSON: %s" % path) from exc
    if not isinstance(value, dict):
        raise ProvisionError("JSON root must be an object: %s" % path)
    return value


def _safe_stage_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ProvisionError("stage manifest contains an unsafe path")
    if not any(value.startswith(prefix) for prefix in ALLOWED_STAGE_PREFIXES):
        raise ProvisionError("stage file is outside the reviewed destination roots: %s" % value)
    return path


def verify_stage(stage: Path, expected_manifest_sha256: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_manifest_sha256):
        raise ProvisionError("stage manifest SHA-256 is malformed")
    requested_stage = stage.expanduser().absolute()
    if requested_stage.is_symlink():
        raise ProvisionError("stage must be one real directory")
    stage = requested_stage.resolve(strict=True)
    if not stage.is_dir():
        raise ProvisionError("stage must be one real directory")
    manifest_path = stage / "stage.manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProvisionError("stage manifest is missing or unsafe")
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise ProvisionError("stage manifest does not match the laptop-supplied SHA-256")
    manifest = _read_json(manifest_path)
    metadata = manifest.get("metadata")
    records = manifest.get("files")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(metadata, dict)
        or metadata.get("release_ready") is not False
        or metadata.get("stage_kind") != "source-and-configuration-overlay"
        or not isinstance(records, list)
        or not records
    ):
        raise ProvisionError("stage manifest header is not a candidate overlay")
    transport_present = metadata.get("deploy_transport_present")
    transport_fingerprint = metadata.get("deploy_public_key_fingerprint")
    if not isinstance(transport_present, bool) or (
        transport_present
        and (
            not isinstance(transport_fingerprint, str)
            or re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", transport_fingerprint)
            is None
        )
    ) or (not transport_present and transport_fingerprint is not None):
        raise ProvisionError("stage deployment-key metadata is malformed")

    expected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ProvisionError("stage manifest file record is malformed")
        name = record.get("path")
        if not isinstance(name, str) or name in expected:
            raise ProvisionError("stage manifest path is missing or duplicated")
        _safe_stage_path(name)
        size = record.get("size")
        mode = record.get("mode")
        digest = record.get("sha256")
        if (
            not isinstance(size, int)
            or size < 0
            or not isinstance(mode, str)
            or not re.fullmatch(r"0o[0-7]{3}", mode)
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise ProvisionError("stage manifest file metadata is invalid")
        expected[name] = record

    actual: dict[str, Path] = {}
    for path in stage.rglob("*"):
        relative = path.relative_to(stage).as_posix()
        if path.is_symlink():
            raise ProvisionError("stage contains a symlink: %s" % relative)
        if path.is_file():
            if relative == "stage.manifest.json":
                continue
            actual[relative] = path
        elif not path.is_dir():
            raise ProvisionError("stage contains an unsupported filesystem object")
    if set(actual) != set(expected):
        raise ProvisionError("stage files do not exactly match their manifest")
    for name, path in actual.items():
        record = expected[name]
        if (
            path.stat().st_size != record["size"]
            or oct(path.stat().st_mode & 0o777) != record["mode"]
            or sha256_file(path) != record["sha256"]
        ):
            raise ProvisionError("stage file changed after manifest creation: %s" % name)

    stack_path = stage / "opt/t300/stack.lock.json"
    stack = load_lock(stack_path)
    debian_ref = stack["debian_artifacts"]
    python_ref = stack["python_artifacts"]
    debian_path = stage / "opt/t300/debian-artifacts.lock.json"
    python_path = stage / "opt/t300/python-artifacts.lock.json"
    roots_path = stage / "opt/t300/debian-root-packages.json"
    if sha256_file(debian_path) != debian_ref["sha256"]:
        raise ProvisionError("staged Debian lock differs from stack.lock.json")
    if sha256_file(roots_path) != debian_ref["root_policy_sha256"]:
        raise ProvisionError("staged Debian root policy differs from stack.lock.json")
    if sha256_file(python_path) != python_ref["sha256"]:
        raise ProvisionError("staged Python lock differs from stack.lock.json")
    debian = load_debian_lock(debian_path)
    python = load_artifact_lock(python_path)
    if len(debian["artifacts"]) != debian_ref["artifact_count"]:
        raise ProvisionError("staged Debian package count differs from stack.lock.json")
    if any(
        python["target"].get(key) != value
        for key, value in python_ref["target"].items()
    ):
        raise ProvisionError("staged Python target differs from stack.lock.json")
    return {
        "root": stage,
        "manifest": manifest,
        "manifest_sha256": expected_manifest_sha256,
        "files": expected,
        "stack": stack,
        "debian": debian,
        "python": python,
    }


def capacity_budget(
    stage_info: dict[str, Any], total_bytes: int, available_bytes: int
) -> dict[str, Any]:
    if total_bytes <= 0 or available_bytes < 0 or available_bytes > total_bytes:
        raise ProvisionError("root filesystem capacity values are invalid")
    stage_copy_bytes = sum(
        record["size"] for record in stage_info["files"].values()
    ) + (stage_info["root"] / "stage.manifest.json").stat().st_size
    debian_installed_bytes = (
        stage_info["debian"]["solver"]["total_installed_size_kib"] * 1024
    )
    protected_free_bytes = max(
        MIN_POST_PROVISION_FREE_BYTES, total_bytes // MIN_FREE_FRACTION
    )
    required_available_bytes = (
        stage_copy_bytes
        + debian_installed_bytes
        + BUILD_WORKSPACE_BYTES
        + protected_free_bytes
    )
    return {
        "filesystem_total_bytes": total_bytes,
        "filesystem_available_bytes": available_bytes,
        "stage_copy_bytes": stage_copy_bytes,
        "debian_installed_bytes": debian_installed_bytes,
        "build_workspace_bytes": BUILD_WORKSPACE_BYTES,
        "protected_free_bytes": protected_free_bytes,
        "required_available_bytes": required_available_bytes,
        "headroom_bytes": available_bytes - required_available_bytes,
        "ready": available_bytes >= required_available_bytes,
    }


def _root_capacity(stage_info: dict[str, Any]) -> dict[str, Any]:
    try:
        filesystem = os.statvfs("/")
    except OSError as exc:
        raise ProvisionError("could not inspect root filesystem capacity") from exc
    return capacity_budget(
        stage_info,
        filesystem.f_blocks * filesystem.f_frsize,
        filesystem.f_bavail * filesystem.f_frsize,
    )


def _parse_assignment_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvisionError("could not read platform identity: %s" % path) from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def _output(command: list[str], timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError("could not run platform inspection: %s" % command[0]) from exc
    if result.returncode:
        raise ProvisionError(
            "platform inspection failed: %s: %s"
            % (command[0], result.stderr.strip() or "no detail")
        )
    return result.stdout.strip()


def inspect_candidate(stage_info: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    if platform.machine() != "aarch64":
        failures.append("running architecture is not aarch64")
    os_release = _parse_assignment_file(Path("/etc/os-release"))
    if os_release.get("ID") != "debian" or os_release.get("VERSION_ID") != "13":
        failures.append("running OS is not the pinned Debian 13 base")
    armbian = _parse_assignment_file(Path("/etc/armbian-release"))
    for key, expected in EXPECTED_ARMBIAN.items():
        if armbian.get(key) != expected:
            failures.append("Armbian identity %s is not %s" % (key, expected))
    expected_armbian_version = stage_info["stack"]["base_image"]["version"]
    if armbian.get("VERSION") != expected_armbian_version:
        failures.append(
            "Armbian version is not the pinned %s" % expected_armbian_version
        )
    expected_kernel = stage_info["stack"]["target"]["kernel"]
    running_kernel = platform.release()
    if not (
        running_kernel == expected_kernel
        or running_kernel.startswith(expected_kernel + "-")
    ):
        failures.append("running kernel is not the pinned %s series" % expected_kernel)
    details["armbian_version"] = armbian.get("VERSION")
    details["running_kernel"] = running_kernel
    try:
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\x00").decode("ascii")
    except (OSError, UnicodeDecodeError):
        model = ""
    normalized_model = re.sub(r"[^a-z0-9]+", "", model.lower())
    if "mks" not in normalized_model or "klipad50" not in normalized_model:
        failures.append("device tree does not identify an MKS-Klipad50")
    details["device_tree_model"] = model

    expected_status = stage_info["debian"]["base_dpkg_status_sha256"]
    status_path = Path("/var/lib/dpkg/status")
    if not status_path.is_file() or sha256_file(status_path) != expected_status:
        failures.append("installed-package database is not the exact signed base")

    try:
        root_source = _output(["/usr/bin/findmnt", "-n", "-o", "SOURCE", "/"])
        match = ROOT_SOURCE_RE.fullmatch(root_source)
        if match is None:
            failures.append("root filesystem is not a plain USB disk partition")
            root_disk = ""
        else:
            root_disk = "/dev/" + match.group(1)
            properties = _output(
                ["/usr/bin/lsblk", "-dn", "-o", "RM,TRAN,TYPE", root_disk]
            ).split()
            if properties != ["1", "usb", "disk"]:
                failures.append("root filesystem parent is not a removable USB disk")
        details["root_source"] = root_source
        details["root_disk"] = root_disk
        mounts = _output(["/usr/bin/findmnt", "-rn", "-o", "SOURCE"])
        if any(line.startswith("/dev/mmcblk") for line in mounts.splitlines()):
            failures.append("an eMMC partition is mounted")
    except ProvisionError as exc:
        failures.append(str(exc))

    for unit in T300_UNITS:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            failures.append("printer candidate service is already active: %s" % unit)
    for path in (Path("/opt/t300"), Path("/etc/t300"), JOURNAL):
        if path.exists() or path.is_symlink():
            failures.append("candidate destination already exists: %s" % path)
    for name in SERVICE_USERS:
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            failures.append("candidate service account already exists: %s" % name)
    for name in SERVICE_GROUPS:
        try:
            grp.getgrnam(name)
        except KeyError:
            pass
        else:
            failures.append("candidate service group already exists: %s" % name)
    details["base_dpkg_status_sha256"] = expected_status
    try:
        capacity = _root_capacity(stage_info)
    except (OSError, ProvisionError) as exc:
        failures.append(str(exc))
    else:
        details["capacity"] = capacity
        if not capacity["ready"]:
            failures.append(
                "root filesystem lacks the conservative provisioning headroom"
            )
    return {"ready": not failures, "failures": failures, "details": details}


def _write_json_atomic(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _run(
    command: list[str],
    *,
    timeout: int = 600,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    offline: bool = False,
) -> subprocess.CompletedProcess[str]:
    if offline:
        command = ["/usr/bin/unshare", "--net", "--", *command]
    clean_env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/var/empty",
        "PYTHONHASHSEED": "0",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "SOURCE_DATE_EPOCH": "1704067200",
    }
    if env:
        clean_env.update(env)
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            text=True,
            cwd=str(cwd) if cwd is not None else "/",
            env=clean_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError("command failed to execute: %s" % command[-1]) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ProvisionError(
            "command failed (%s): %s" % (command[-1], detail[-2000:] or "no detail")
        )
    return result


def _copy_exact_stage_file(
    source: Path, destination: Path, size: int, mode: int, digest: str
) -> None:
    if destination.exists() or destination.is_symlink():
        raise ProvisionError(
            "refusing to replace an existing candidate file: %s" % destination
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_descriptor = -1
    destination_descriptor = -1
    temporary: Path | None = None
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(source_descriptor)
        current = os.lstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or not os.path.samestat(before, current)
            or before.st_size != size
            or stat.S_IMODE(before.st_mode) != mode
        ):
            raise ProvisionError("staged source metadata changed before copy: %s" % source)
        destination_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % destination.name, dir=str(destination.parent)
        )
        temporary = Path(temporary_name)
        copied = 0
        hash_state = hashlib.sha256()
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            copied += len(block)
            hash_state.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise ProvisionError("candidate file copy made no progress")
                view = view[written:]
        after = os.fstat(source_descriptor)
        current = os.lstat(source)
        if (
            not os.path.samestat(before, after)
            or not os.path.samestat(before, current)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or copied != size
            or hash_state.hexdigest() != digest
        ):
            raise ProvisionError("staged source changed while copied: %s" % source)
        os.fchmod(destination_descriptor, mode)
        os.fsync(destination_descriptor)
        os.close(destination_descriptor)
        destination_descriptor = -1
        os.replace(temporary, destination)
        temporary = None
        installed = os.lstat(destination)
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_size != size
            or stat.S_IMODE(installed.st_mode) != mode
            or sha256_file(destination) != digest
        ):
            raise ProvisionError("installed candidate file failed readback: %s" % destination)
    except OSError as exc:
        raise ProvisionError("could not copy exact staged file: %s" % source) from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_stage(stage_info: dict[str, Any]) -> None:
    stage: Path = stage_info["root"]
    for name in sorted(stage_info["files"]):
        record = stage_info["files"][name]
        source = stage.joinpath(*PurePosixPath(name).parts)
        destination = Path("/").joinpath(*PurePosixPath(name).parts)
        _copy_exact_stage_file(
            source,
            destination,
            record["size"],
            int(record["mode"], 8),
            record["sha256"],
        )
    manifest_source = stage / "stage.manifest.json"
    manifest_info = os.lstat(manifest_source)
    _copy_exact_stage_file(
        manifest_source,
        Path("/opt/t300/stage.manifest.json"),
        manifest_info.st_size,
        stat.S_IMODE(manifest_info.st_mode),
        stage_info["manifest_sha256"],
    )


def _verify_debian_cache(lock: dict[str, Any]) -> list[Path]:
    root = Path("/opt/t300/debian-packages")
    expected = {item["filename"]: item for item in lock["artifacts"]}
    actual = {path.name: path for path in root.iterdir() if path.is_file() and not path.is_symlink()}
    if any(path.is_symlink() for path in root.iterdir()) or set(actual) != set(expected):
        raise ProvisionError("local Debian package directory differs from its lock")
    paths: list[Path] = []
    for name in sorted(expected):
        path = actual[name]
        record = expected[name]
        if path.stat().st_size != record["size"] or sha256_file(path) != record["sha256"]:
            raise ProvisionError("local Debian package bytes changed: %s" % name)
        paths.append(path)
    return paths


def _installed_debian() -> dict[tuple[str, str], str]:
    result = _run(
        [
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${Architecture}\\n",
        ],
        timeout=60,
    )
    installed: dict[tuple[str, str], str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        package, version, architecture = fields
        package = package.split(":", 1)[0]
        installed[(package, architecture)] = version
    return installed


def _install_debian(lock: dict[str, Any]) -> dict[str, str]:
    packages = _verify_debian_cache(lock)
    policy = Path("/usr/sbin/policy-rc.d")
    if policy.exists() or policy.is_symlink():
        raise ProvisionError("policy-rc.d unexpectedly exists on the signed base")
    policy.write_text("#!/bin/sh\nexit 101\n", encoding="ascii")
    os.chmod(policy, 0o755)
    try:
        command = [
            "/usr/bin/apt-get",
            "--yes",
            "--no-download",
            "--no-install-recommends",
            "--no-remove",
            "-o",
            "Dpkg::Options::=--force-confold",
            "install",
            *[str(path) for path in packages],
        ]
        _run(
            command,
            timeout=3600,
            offline=True,
            env={"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"},
        )
    finally:
        try:
            policy.unlink()
        except FileNotFoundError:
            pass
    _run(["/usr/bin/dpkg", "--audit"], timeout=120)
    installed = _installed_debian()
    verified: dict[str, str] = {}
    for record in lock["artifacts"]:
        key = (record["package"], record["architecture"])
        if installed.get(key) != record["version"]:
            raise ProvisionError("installed Debian version differs from lock: %s" % record["package"])
        verified["%s:%s" % key] = record["version"]
    return verified


def _create_accounts() -> None:
    for name in SERVICE_GROUPS:
        _run(["/usr/sbin/groupadd", "--system", name], timeout=30)
    for name in SERVICE_USERS:
        home = "/nonexistent"
        command = [
            "/usr/sbin/useradd",
            "--system",
            "--gid",
            name,
            "--home-dir",
            home,
            "--no-create-home",
            "--shell",
            "/bin/sh" if name == "t300-deploy" else "/usr/sbin/nologin",
        ]
        if name == "t300-deploy":
            password = secrets.token_urlsafe(48)
            try:
                hashed = subprocess.run(
                    ["/usr/bin/openssl", "passwd", "-6", "-stdin"],
                    input=password + "\n",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=30,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProvisionError("could not create the deployment account hash") from exc
            password = ""
            password_hash = hashed.stdout.strip()
            if hashed.returncode or not password_hash.startswith("$6$"):
                raise ProvisionError("could not create the deployment account hash")
            command.extend(("--password", password_hash))
        command.append(name)
        _run(command, timeout=30)


def _ensure_dir(path: str, mode: int, owner: str, group: str) -> Path:
    target = Path(path)
    descriptor = -1
    try:
        target.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ProvisionError("runtime path is not a real directory: %s" % target)
        os.fchown(
            descriptor,
            pwd.getpwnam(owner).pw_uid if owner != "root" else 0,
            grp.getgrnam(group).gr_gid if group != "root" else 0,
        )
        os.fchmod(descriptor, mode)
    except OSError as exc:
        raise ProvisionError("runtime directory is unsafe or unavailable: %s" % target) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return target


def _prepare_runtime_directories() -> None:
    _ensure_dir("/etc/t300/commissioning", 0o700, "root", "root")
    _ensure_dir("/mnt/t300-data", 0o755, "root", "root")
    _ensure_dir("/var/lib/t300", 0o755, "root", "root")
    _ensure_dir("/var/lib/t300/moonraker-data", 0o750, "moonraker", "t300-comms")
    _ensure_dir(
        "/var/lib/t300/moonraker-data/config", 0o750, "moonraker", "t300-comms"
    )
    # This covered directory remains non-writable if the data USB disappears.
    _ensure_dir("/var/lib/t300/moonraker-data/gcodes", 0o550, "root", "t300-gcode")
    # Setgid keeps immutable 0440 approvals and snapshots readable by the
    # production Klipper process through the narrow t300-gcode group.
    _ensure_dir("/var/lib/t300/gcode-approvals", 0o2750, "t300-policy", "t300-gcode")
    _ensure_dir("/var/lib/t300/approved-gcodes", 0o2750, "t300-policy", "t300-gcode")
    _ensure_dir("/var/lib/t300/gcode-rejections", 0o750, "t300-policy", "t300-policy")
    _ensure_dir("/var/lib/t300/incoming", 0o730, "root", "t300-deploy")
    for name, owner in (
        ("klipper", "klipper"),
        ("klipper-maintenance", "klipper"),
        ("moonraker", "moonraker"),
        ("crowsnest", "crowsnest"),
        ("mainsail", "mainsail"),
        ("touchscreen", "t300-touchscreen"),
    ):
        _ensure_dir("/var/log/t300/%s" % name, 0o750, owner, owner if owner != "root" else "root")


def _install_mainsail_defaults() -> None:
    source = Path("/etc/t300/mainsail/default.json")
    defaults = _read_json(source)
    required = {"general", "navigation", "uiSettings", "view", "macros", "dashboard"}
    if set(defaults) != required:
        raise ProvisionError("Mainsail defaults do not contain the reviewed UI sections")
    theme_dir = _ensure_dir(
        "/var/lib/t300/moonraker-data/config/.theme", 0o755, "root", "root"
    )
    destination = theme_dir / "default.json"
    if destination.exists() or destination.is_symlink():
        raise ProvisionError("refusing to replace existing Mainsail defaults")
    _write_json_atomic(destination, defaults, mode=0o444)
    os.chown(destination, 0, 0)
    os.chmod(theme_dir, 0o555)


def _stop_and_disable_base_ssh() -> None:
    if any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        raise ProvisionError("candidate provisioning must be initiated from the local console")
    _run(["/usr/bin/systemctl", "disable", "--now", "ssh.service"], timeout=60)


def _prepare_restricted_ssh() -> str:
    for path in Path("/etc/ssh").glob("ssh_host_*"):
        if path.is_symlink() or not path.is_file():
            raise ProvisionError("base SSH host-key path is unsafe")
        path.unlink()
    private_key = Path("/etc/ssh/ssh_host_ed25519_key")
    _run(
        [
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private_key),
        ],
        timeout=60,
    )
    _run(["/usr/sbin/sshd", "-t"], timeout=30)
    _validate_effective_sshd()
    fingerprint = _run(
        ["/usr/bin/ssh-keygen", "-l", "-E", "sha256", "-f", str(private_key) + ".pub"],
        timeout=30,
    ).stdout.split()
    if len(fingerprint) < 2 or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint[1]):
        raise ProvisionError("could not determine the rotated SSH host fingerprint")
    return fingerprint[1]


def _validate_effective_sshd(
    config_path: Path = Path("/etc/ssh/sshd_config.d/60-t300.conf"),
) -> dict[str, str]:
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvisionError("restricted SSH configuration is unreadable") from exc
    match = re.search(
        r"(?m)^AllowUsers\s+t300-deploy@([^\s]+)\s*$", text
    )
    if match is None:
        raise ProvisionError("restricted SSH configuration lacks one source network")
    try:
        network = ipaddress.ip_network(match.group(1), strict=True)
    except ValueError as exc:
        raise ProvisionError("restricted SSH source network is malformed") from exc
    if network.version != 4:
        raise ProvisionError("restricted SSH transport must use the staged IPv4 network")
    address = network.network_address
    if network.num_addresses > 2:
        address += 1
    result = _run(
        [
            "/usr/sbin/sshd",
            "-T",
            "-C",
            "user=t300-deploy,host=t300-candidate,addr=%s" % address,
        ],
        timeout=30,
    )
    settings: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator:
            settings[key.lower()] = value.strip()
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
    }
    failures = [
        "%s=%s" % (name, settings.get(name, "<missing>"))
        for name, expected in required.items()
        if settings.get(name) != expected
    ]
    allow_users = settings.get("allowusers", "").split()
    expected_user = "t300-deploy@%s" % network
    if allow_users != [expected_user]:
        failures.append("allowusers=%s" % (settings.get("allowusers", "<missing>"),))
    if failures:
        raise ProvisionError(
            "effective restricted SSH policy differs: %s" % ", ".join(failures)
        )
    return settings


def _normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ProvisionError("built wheel has no unique METADATA: %s" % path.name)
            message = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ProvisionError("could not inspect built wheel: %s" % path.name) from exc
    name, version = message.get("Name"), message.get("Version")
    if not name or not version:
        raise ProvisionError("built wheel metadata lacks name or version")
    return _normalize_package(name), version


def _venv_python(path: Path) -> Path:
    return path / "bin/python"


def _create_venv(path: Path, with_pip: bool) -> Path:
    command = ["/usr/bin/python3", "-m", "venv"]
    if not with_pip:
        command.append("--without-pip")
    command.append(str(path))
    _run(command, timeout=180, offline=True)
    return _venv_python(path)


def _distribution_map(python: Path) -> dict[str, str]:
    script = (
        "import importlib.metadata as m,json,re;"
        "n=lambda s:re.sub(r'[-_.]+','-',s).lower();"
        "print(json.dumps({n(d.metadata['Name']):d.version for d in m.distributions()},sort_keys=True))"
    )
    result = _run([str(python), "-I", "-c", script], timeout=60, offline=True)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProvisionError("could not inspect Python environment") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ProvisionError("Python environment metadata is malformed")
    return value


def _install_python_environments(lock: dict[str, Any]) -> dict[str, Any]:
    expected_version = lock["target"]["python"]
    actual_version = _run(
        ["/usr/bin/python3", "-c", "import platform; print(platform.python_version())"],
        timeout=30,
        offline=True,
    ).stdout.strip()
    if actual_version != expected_version:
        raise ProvisionError("system Python is not the locked %s" % expected_version)

    wheelhouse = Path("/opt/t300/python-wheelhouse")
    environments = {item["name"]: item for item in lock["environments"]}
    build = environments.get("build")
    if build is None or any(item["packagetype"] != "bdist_wheel" for item in build["artifacts"]):
        raise ProvisionError("locked Python build environment is incomplete")
    build_python = _create_venv(Path("/opt/t300/venvs/build"), with_pip=True)
    build_files = [wheelhouse / "build" / item["filename"] for item in build["artifacts"]]
    _run(
        [
            str(build_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            *[str(path) for path in build_files],
        ],
        timeout=600,
        offline=True,
    )
    expected_build = {
        _normalize_package(item["package"]): item["version"]
        for item in build["artifacts"]
    }
    expected_build["pip"] = lock["bootstrap"]["pip"]
    actual_build = _distribution_map(build_python)
    if actual_build != expected_build:
        raise ProvisionError("installed Python build environment differs from its lock")

    built: dict[tuple[str, str, str], Path] = {}
    native_root = Path("/opt/t300/native-wheels")
    native_root.mkdir(parents=True, exist_ok=False)
    build_environment = {
        "CC": "/usr/bin/cc",
        "CXX": "/usr/bin/c++",
        "PKG_CONFIG": "/usr/bin/pkg-config",
        "NINJA": "/usr/bin/ninja",
    }
    for environment in lock["environments"]:
        if environment["name"] == "build":
            continue
        output = native_root / environment["name"]
        output.mkdir()
        for artifact in environment["artifacts"]:
            if artifact["packagetype"] != "sdist":
                continue
            source = wheelhouse / environment["name"] / artifact["filename"]
            before = set(output.iterdir())
            _run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--no-cache-dir",
                    "--wheel-dir",
                    str(output),
                    str(source),
                ],
                timeout=1800,
                offline=True,
                env=build_environment,
            )
            created = [path for path in set(output.iterdir()) - before if path.suffix == ".whl"]
            if len(created) != 1:
                raise ProvisionError("source artifact did not produce exactly one wheel")
            package, version = _wheel_identity(created[0])
            expected = (_normalize_package(artifact["package"]), artifact["version"])
            if (package, version) != expected:
                raise ProvisionError("built wheel identity differs from its locked source")
            built[(environment["name"], package, version)] = created[0]

    installed: dict[str, dict[str, str]] = {}
    for environment in lock["environments"]:
        name = environment["name"]
        if name == "build":
            continue
        target = Path("/opt/t300/venvs") / name
        target_python = _create_venv(target, with_pip=False)
        install_files: list[Path] = []
        expected: dict[str, str] = {}
        for artifact in environment["artifacts"]:
            package = _normalize_package(artifact["package"])
            if package in expected and expected[package] != artifact["version"]:
                raise ProvisionError("one Python environment locks two versions of %s" % package)
            expected[package] = artifact["version"]
            if artifact["packagetype"] == "bdist_wheel":
                install_files.append(wheelhouse / name / artifact["filename"])
            else:
                install_files.append(built[(name, package, artifact["version"])])
        _run(
            [
                str(build_python),
                "-m",
                "pip",
                "--python",
                str(target_python),
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                *[str(path) for path in install_files],
            ],
            timeout=1200,
            offline=True,
        )
        actual = _distribution_map(target_python)
        if actual != expected:
            raise ProvisionError("installed Python environment differs from lock: %s" % name)
        installed[name] = actual

    for name, source in (
        ("control", "/opt/t300/control"),
        ("crowsnest", "/opt/t300/src/crowsnest"),
    ):
        python = _create_venv(Path("/opt/t300/venvs") / name, with_pip=False)
        site_result = _run(
            [str(python), "-I", "-c", "import site; print(site.getsitepackages()[0])"],
            timeout=30,
            offline=True,
        )
        site_path = Path(site_result.stdout.strip())
        site_path.mkdir(parents=True, exist_ok=True)
        pth = site_path / "t300-source.pth"
        pth.write_text(source + "\n", encoding="ascii")
        os.chmod(pth, 0o444)
        installed[name] = _distribution_map(python)
        if installed[name]:
            raise ProvisionError("stdlib-only environment unexpectedly contains packages: %s" % name)
    return {
        "environments": installed,
        "native_wheels": [
            {
                "environment": environment,
                "package": package,
                "version": version,
                "filename": path.name,
                "sha256": sha256_file(path),
            }
            for (environment, package, version), path in sorted(built.items())
        ],
        "build_environment": actual_build,
    }


def _build_ustreamer() -> dict[str, str]:
    source = Path("/opt/t300/src/ustreamer")
    env = {
        "CC": "/usr/bin/cc",
        "PKG_CONFIG": "/usr/bin/pkg-config",
        "CFLAGS": "-O2 -pipe -fstack-protector-strong -D_FORTIFY_SOURCE=2 -fPIE",
        "LDFLAGS": "-Wl,-z,relro -Wl,-z,now -pie",
    }
    _run(
        [
            "/usr/bin/make",
            "-j1",
            "WITH_PYTHON=0",
            "WITH_JANUS=0",
            "WITH_V4P=0",
            "WITH_GPIO=0",
            "WITH_SYSTEMD=0",
        ],
        timeout=1800,
        cwd=source,
        env=env,
        offline=True,
    )
    binary = source / "src/ustreamer.bin"
    if binary.is_symlink() or not binary.is_file():
        raise ProvisionError("uStreamer build did not produce its regular binary")
    file_info = _run(["/usr/bin/readelf", "-h", str(binary)], timeout=30, offline=True).stdout
    if re.search(r"Machine:\s+AArch64(?:\s|$)", file_info) is None:
        raise ProvisionError("uStreamer binary is not AArch64")
    linkage = _run(["/usr/bin/ldd", str(binary)], timeout=30, offline=True).stdout
    if "not found" in linkage:
        raise ProvisionError("uStreamer has an unresolved shared-library dependency")
    version = _run([str(binary), "--version"], timeout=30, offline=True)
    destination = Path("/opt/t300/src/crowsnest/bin/ustreamer/ustreamer")
    destination.parent.mkdir(parents=True, exist_ok=False)
    shutil.copy2(binary, destination)
    os.chmod(destination, 0o755)
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "version_output": (version.stdout or version.stderr).strip(),
        "linkage_sha256": hashlib.sha256(linkage.encode("utf-8")).hexdigest(),
    }


def _lock_configuration_permissions() -> None:
    root = Path("/etc/t300")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ProvisionError("configuration contains a symlink: %s" % path)
        if path == root / "commissioning" or (root / "commissioning") in path.parents:
            continue
        if path == root / "klipper/private" or (root / "klipper/private") in path.parents:
            if path.is_dir():
                os.chown(path, 0, grp.getgrnam("klipper").gr_gid)
                os.chmod(path, 0o750)
            else:
                os.chown(path, 0, grp.getgrnam("klipper").gr_gid)
                os.chmod(path, 0o440)
            continue
        os.chown(path, 0, 0)
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chown(root, 0, 0)
    os.chmod(root, 0o555)


def _lock_private_runtime_permissions() -> None:
    root = Path("/opt/t300/private/touchscreen")
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ProvisionError("private touchscreen runtime is not one real directory")
    group = grp.getgrnam("t300-touchscreen").gr_gid
    expected = {
        root / "zhongchuang_klipper": 0o550,
        root / "gene5.py": 0o440,
        root / "lib/libboost_system.so.1.67.0": 0o440,
        root / "lib/libwpa_client.so": 0o440,
    }
    actual = {path for path in root.rglob("*") if path.is_file()}
    if actual != set(expected) or any(path.is_symlink() for path in root.rglob("*")):
        raise ProvisionError("private touchscreen runtime shape changed after staging")
    for directory in (root, root / "lib"):
        os.chown(directory, 0, group)
        os.chmod(directory, 0o550)
    for path, mode in expected.items():
        os.chown(path, 0, group)
        os.chmod(path, mode)


def _validate_services() -> None:
    # Klipper's own asynchronous logger catches EFBIG and continues, while
    # CPython must keep SIGXFSZ ignored for the systemd file-size ceiling to
    # remain a log failure instead of terminating motion control.
    _run(
        [
            "/opt/t300/venvs/klipper/bin/python",
            "-I",
            "-c",
            "import signal;raise SystemExit(0 if signal.getsignal(signal.SIGXFSZ) == signal.SIG_IGN else 1)",
        ],
        timeout=30,
        offline=True,
    )
    unit_paths = [Path("/etc/systemd/system") / name for name in T300_UNITS]
    _run(["/usr/bin/systemd-analyze", "verify", *[str(path) for path in unit_paths]], timeout=180)
    _run(["/usr/bin/systemd-analyze", "verify", "/lib/systemd/system/ssh.service"], timeout=180)
    _run(["/usr/sbin/sshd", "-t"], timeout=30)
    _validate_effective_sshd()
    _run(["/usr/sbin/nginx", "-t", "-c", "/etc/t300/nginx/nginx.conf"], timeout=60)
    _run(
        ["/usr/bin/udevadm", "verify", "/etc/udev/rules.d/60-t300-host-mcu.rules"],
        timeout=30,
    )
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    _run(["/usr/bin/systemctl", "mask", "nginx.service"], timeout=60)
    for unit in T300_UNITS:
        active = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if active.returncode == 0:
            raise ProvisionError("candidate service became active during provisioning: %s" % unit)
        enabled = subprocess.run(
            ["/usr/bin/systemctl", "is-enabled", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if enabled.returncode == 0:
            raise ProvisionError("candidate service became enabled during provisioning: %s" % unit)
    for operation in ("is-active", "is-enabled"):
        result = subprocess.run(
            ["/usr/bin/systemctl", operation, "--quiet", "ssh.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            raise ProvisionError(
                "restricted SSH must remain stopped and disabled after provisioning"
            )


def provision(
    stage: Path,
    manifest_sha256: str,
    apply: bool,
    confirmation: str | None,
    *,
    verify_stage_only: bool = False,
) -> dict[str, Any]:
    stage_info = verify_stage(stage, manifest_sha256)
    if verify_stage_only:
        if apply or confirmation is not None:
            raise ProvisionError(
                "--verify-stage-only cannot be combined with --apply or --confirm"
            )
        return {
            "stage_verified": True,
            "stage_manifest_sha256": manifest_sha256,
            "file_count": len(stage_info["files"]),
            "debian_artifact_count": len(stage_info["debian"]["artifacts"]),
            "python_environment_count": len(stage_info["python"]["environments"]),
            "metadata": stage_info["manifest"]["metadata"],
        }
    inspection = inspect_candidate(stage_info)
    expected_confirmation = "PROVISION T300 USB %s" % manifest_sha256[:12]
    result = {
        "stage_manifest_sha256": manifest_sha256,
        "candidate": inspection,
        "expected_confirmation": expected_confirmation,
        "apply_requested": apply,
    }
    if not apply:
        return result
    if os.geteuid() != 0:
        raise ProvisionError("--apply requires root on the USB-booted candidate")
    if not inspection["ready"]:
        raise ProvisionError("candidate gates failed: %s" % "; ".join(inspection["failures"]))
    if confirmation != expected_confirmation:
        raise ProvisionError("typed confirmation must be exactly: %s" % expected_confirmation)

    journal: dict[str, Any] = {
        "schema_version": 1,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage_manifest_sha256": manifest_sha256,
        "phase": "copy-stage",
        "status": "in-progress",
        "target": "live removable USB root only",
    }
    _write_json_atomic(JOURNAL, journal)
    try:
        _stop_and_disable_base_ssh()
        _copy_stage(stage_info)
        journal["phase"] = "install-debian"
        _write_json_atomic(JOURNAL, journal)
        debian_versions = _install_debian(stage_info["debian"])
        journal["phase"] = "firmware-build"
        _write_json_atomic(JOURNAL, journal)
        try:
            firmware = build_firmware(
                Path("/opt/t300/src/klipper"),
                Path("/opt/t300/firmware-inputs"),
                Path("/opt/t300/firmware"),
                stage_info["stack"],
                _run,
            )
        except FirmwareError as exc:
            raise ProvisionError(str(exc)) from exc
        journal["phase"] = "accounts-and-directories"
        _write_json_atomic(JOURNAL, journal)
        _create_accounts()
        _prepare_runtime_directories()
        _install_mainsail_defaults()
        journal["phase"] = "python-environments"
        _write_json_atomic(JOURNAL, journal)
        python_result = _install_python_environments(stage_info["python"])
        journal["phase"] = "ustreamer"
        _write_json_atomic(JOURNAL, journal)
        ustreamer = _build_ustreamer()
        journal["phase"] = "restricted-transport"
        _write_json_atomic(JOURNAL, journal)
        transport_host_key = _prepare_restricted_ssh()
        journal["phase"] = "permissions-and-validation"
        _write_json_atomic(JOURNAL, journal)
        _lock_configuration_permissions()
        _lock_private_runtime_permissions()
        _validate_services()

        candidate_manifest = {
            "schema_version": 1,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "release_ready": False,
            "production_enabled": False,
            "host_validated": False,
            "maintenance_enabled": False,
            "stage_manifest_sha256": manifest_sha256,
            "stack_lock_sha256": sha256_file(Path("/opt/t300/stack.lock.json")),
            "debian_lock_sha256": sha256_file(Path("/opt/t300/debian-artifacts.lock.json")),
            "python_lock_sha256": sha256_file(Path("/opt/t300/python-artifacts.lock.json")),
            "debian_packages": debian_versions,
            "python": python_result,
            "firmware": firmware,
            "ustreamer": ustreamer,
            "transport_host_key_fingerprint": transport_host_key,
            "enabled_units": [],
            "next_gate": "owner-run USB peripheral validation with printer control disabled",
        }
        manifest_path = Path("/opt/t300/candidate.manifest.json")
        _write_json_atomic(manifest_path, candidate_manifest, mode=0o400)
        journal.update(
            {
                "phase": "complete",
                "status": "complete",
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "candidate_manifest_sha256": sha256_file(manifest_path),
            }
        )
        _write_json_atomic(JOURNAL, journal)
        result["candidate_manifest"] = str(manifest_path)
        result["candidate_manifest_sha256"] = sha256_file(manifest_path)
        return result
    except BaseException as exc:
        journal.update(
            {
                "status": "failed-reimage-required",
                "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
            }
        )
        try:
            _write_json_atomic(JOURNAL, journal)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--stage-manifest-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument(
        "--verify-stage-only",
        action="store_true",
        help="verify the complete staged bundle on any host without inspecting or changing a target",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = provision(
            args.stage,
            args.stage_manifest_sha256,
            args.apply,
            args.confirm,
            verify_stage_only=args.verify_stage_only,
        )
    except (OSError, ValueError, ProvisionError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

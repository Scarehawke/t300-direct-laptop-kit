"""Guarded laptop-side capture and restore for a T300 recovery boot."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Any, BinaryIO


MANIFEST_SCHEMA = 1
HOST_RE = re.compile(r"^(?:root@)?(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])$")
DEVICE_RE = re.compile(r"^/dev/mmcblk[0-9]+$")
MACHINE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
LOOP_DEVICE_RE = re.compile(r"^/dev/loop[0-9]+$")
SUPPORTED_IMAGE_FILESYSTEMS = {"ext2", "ext3", "ext4", "vfat"}
MAX_LOCAL_COMMAND_OUTPUT = 1024 * 1024
FILESYSTEM_VERIFY_RESERVE_BYTES = 512 * 1024 * 1024


class ImagingError(RuntimeError):
    pass


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _existing_regular_file(path: Path, description: str) -> Path:
    path = path.expanduser().absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ImagingError("%s is unavailable: %s" % (description, exc)) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ImagingError("%s must be one regular, non-symlink file" % (description,))
    return path.resolve(strict=True)


def _fsync_file_and_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor = -1
    temporary = path.with_name(".%s.partial" % (path.name,))
    if temporary.exists() or temporary.is_symlink():
        raise ImagingError("temporary manifest already exists: %s" % (temporary,))
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_file_and_directory(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validated_host(value: str) -> str:
    if not HOST_RE.fullmatch(value) or value.startswith("-"):
        raise ImagingError("recovery host must be one plain hostname or IP address")
    return value


def _validated_device(value: str) -> str:
    if not DEVICE_RE.fullmatch(value):
        raise ImagingError("recovery target must be a whole /dev/mmcblkN device")
    return value


def _require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ImagingError("required program is not installed: %s" % (name,))
    return path


def _close_pipe(pipe: BinaryIO | None) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except OSError:
        pass


def _kill_and_wait(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _read_process_error(process: subprocess.Popen[bytes]) -> str:
    if process.stderr is None:
        return ""
    try:
        return process.stderr.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _validated_ssh_file(path: Path, description: str, private: bool) -> Path:
    path = path.expanduser().absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ImagingError("%s is unavailable: %s" % (description, exc)) from exc
    if path.is_symlink() or not path.is_file():
        raise ImagingError("%s must be one regular, non-symlink file" % (description,))
    if not private and info.st_size == 0:
        raise ImagingError("%s is empty" % (description,))
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ImagingError("%s may not be group- or world-writable" % (description,))
    if private and info.st_mode & 0o077:
        raise ImagingError("%s must be private to its owner" % (description,))
    return path.resolve(strict=True)


class RecoveryClient:
    def __init__(
        self,
        host: str,
        identity_file: Path,
        known_hosts_file: Path,
        agent_path: str = "/usr/local/sbin/t300-recovery-agent",
        ssh_program: str = "ssh",
    ) -> None:
        self.host = _validated_host(host)
        if self.host.startswith("root@"):
            self.host = self.host[5:]
        self.identity_file = _validated_ssh_file(
            identity_file, "recovery SSH identity", private=True
        )
        self.known_hosts_file = _validated_ssh_file(
            known_hosts_file, "recovery SSH known-hosts file", private=False
        )
        if agent_path != "/usr/local/sbin/t300-recovery-agent":
            raise ImagingError("the recovery-agent path is fixed by policy")
        self.agent_path = agent_path
        self.ssh = _require_program(ssh_program)

    def command(self, *arguments: str) -> list[str]:
        remote_command = shlex.join([self.agent_path, *arguments])
        return [
            self.ssh,
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=%s" % (self.known_hosts_file,),
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentityFile=%s" % (self.identity_file,),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "RequestTTY=no",
            "-l",
            "root",
            self.host,
            remote_command,
        ]

    def inspect(
        self,
        device: str,
        record_boot: bool = False,
        apply: bool = False,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        device = _validated_device(device)
        arguments = ["inspect", "--device", device]
        if record_boot:
            arguments.append("--record-boot")
        if apply:
            arguments.append("--apply")
        if confirmation is not None:
            arguments.extend(["--confirm", confirmation])
        result = subprocess.run(
            self.command(*arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise ImagingError("recovery inspection failed: %s" % (detail,))
        if len(result.stdout) > 1024 * 1024:
            raise ImagingError("recovery inspection response exceeded 1 MiB")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImagingError("recovery agent returned invalid inspection JSON") from exc
        if not isinstance(value, dict):
            raise ImagingError("recovery inspection must return one JSON object")
        return value

    def hash_device(self, device: str) -> dict[str, Any]:
        device = _validated_device(device)
        result = subprocess.run(
            self.command("hash", "--device", device),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise ImagingError(
                "second device hash failed: %s"
                % (result.stderr.decode("utf-8", "replace").strip(),)
            )
        if len(result.stdout) > 1024 * 1024:
            raise ImagingError("recovery hash response exceeded 1 MiB")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImagingError("recovery hash response was invalid") from exc
        if not isinstance(value, dict):
            raise ImagingError("recovery hash response was malformed")
        return value


def _validated_hash_response(value: dict[str, Any], expected_size: int) -> str:
    digest = value.get("sha256")
    size = value.get("size")
    if not isinstance(digest, str) or MACHINE_ID_RE.fullmatch(digest) is None:
        raise ImagingError("recovery device hash is malformed")
    if not isinstance(size, int) or isinstance(size, bool) or size != expected_size:
        raise ImagingError("recovery device hash size is malformed")
    return digest


def _check_inspection(inspection: dict[str, Any], purpose: str) -> None:
    if inspection.get("schema_version") != 1:
        raise ImagingError("recovery inspection schema is unsupported")
    key = "safe_for_%s" % (purpose,)
    if inspection.get("board") != "MKS-Klipad50":
        raise ImagingError("recovery board identity is not MKS-Klipad50")
    if inspection.get(key) is not True:
        reasons = inspection.get("blocked_reasons", [])
        raise ImagingError(
            "recovery target is not safe for %s: %s"
            % (purpose, "; ".join(map(str, reasons)) or "unspecified gate")
        )
    boots = inspection.get("verified_usb_boots")
    if not isinstance(boots, int) or isinstance(boots, bool) or boots < 3:
        raise ImagingError("three distinct verified recovery-USB boots are required")
    machine_id = inspection.get("machine_id")
    if not isinstance(machine_id, str) or not MACHINE_ID_RE.fullmatch(machine_id):
        raise ImagingError("recovery machine identity is malformed")
    target = inspection.get("target_device")
    if not isinstance(target, str) or DEVICE_RE.fullmatch(target) is None:
        raise ImagingError("recovery target identity is malformed")
    target_size = inspection.get("target_size")
    if not isinstance(target_size, int) or isinstance(target_size, bool) or target_size <= 0:
        raise ImagingError("recovery target size is malformed")
    marker_id = inspection.get("marker_id")
    if not isinstance(marker_id, str) or UUID_RE.fullmatch(marker_id) is None:
        raise ImagingError("recovery overlay identity is malformed")
    geometry = inspection.get("block_geometry")
    if not isinstance(geometry, dict):
        raise ImagingError("recovery block geometry is missing")
    integer_fields = (
        "logical_sector_bytes",
        "physical_sector_bytes",
        "minimum_io_bytes",
        "optimal_io_bytes",
        "sectors_512",
        "device_bytes",
    )
    if any(
        not isinstance(geometry.get(name), int)
        or isinstance(geometry.get(name), bool)
        for name in integer_fields
    ):
        raise ImagingError("recovery block geometry is malformed")
    logical = geometry["logical_sector_bytes"]
    physical = geometry["physical_sector_bytes"]
    if (
        logical < 512
        or logical & (logical - 1)
        or physical < logical
        or physical % logical
        or geometry["minimum_io_bytes"] < logical
        or geometry["optimal_io_bytes"] < 0
        or geometry["sectors_512"] <= 0
        or geometry["sectors_512"] * 512 != target_size
        or geometry["device_bytes"] != target_size
    ):
        raise ImagingError("recovery block geometry is inconsistent")
    table = inspection.get("partition_table")
    if (
        not isinstance(table, dict)
        or table.get("device") != target
        or table.get("label") not in {"dos", "gpt"}
        or table.get("unit") != "sectors"
        or table.get("sector_size") != logical
        or not isinstance(table.get("partitions"), list)
        or not table["partitions"]
    ):
        raise ImagingError("recovery partition-table evidence is malformed")
    bootloader = inspection.get("bootloader")
    if (
        not isinstance(bootloader, dict)
        or bootloader.get("device_tree_handoff_identifies_board") is not True
    ):
        raise ImagingError("recovery bootloader handoff identity is malformed")
    emmc = inspection.get("emmc")
    if (
        not isinstance(emmc, dict)
        or emmc.get("non_removable") is not True
        or emmc.get("card_type") != "MMC"
        or emmc.get("boot0_present") is not True
        or emmc.get("boot1_present") is not True
        or emmc.get("identifies_emmc") is not True
    ):
        raise ImagingError("recovery target lacks exact non-removable eMMC evidence")


def capture_image(
    client: RecoveryClient,
    device: str,
    output: Path,
    manifest_path: Path | None = None,
) -> Path:
    device = _validated_device(device)
    unresolved_output = output.expanduser().absolute()
    unresolved_output.parent.mkdir(parents=True, exist_ok=True)
    output = unresolved_output.parent.resolve(strict=True) / unresolved_output.name
    if output.exists() or output.is_symlink():
        raise ImagingError("capture destination already exists: %s" % (output,))
    manifest_path = (
        manifest_path.expanduser().absolute()
        if manifest_path is not None
        else output.with_suffix(output.suffix + ".manifest.json")
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_path.parent.resolve(strict=True) / manifest_path.name
    if manifest_path == output:
        raise ImagingError("capture image and manifest paths must differ")
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ImagingError("capture manifest already exists: %s" % (manifest_path,))

    inspection = client.inspect(device)
    _check_inspection(inspection, "capture")
    if inspection["target_device"] != device:
        raise ImagingError("recovery inspection returned another target device")
    expected_size = int(inspection["target_size"])
    zstd = _require_program("zstd")
    partial = output.with_name(".%s.partial" % (output.name,))
    if partial.exists() or partial.is_symlink():
        raise ImagingError(
            "partial capture already exists; inspect it before removing or restarting: %s"
            % (partial,)
        )

    try:
        partial_descriptor = os.open(
            partial,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise ImagingError("could not create private partial capture: %s" % (exc,)) from exc
    partial_output = os.fdopen(partial_descriptor, "wb")
    try:
        remote = subprocess.Popen(
            client.command("stream", "--device", device),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except BaseException:
        partial_output.close()
        raise
    try:
        compressor = subprocess.Popen(
            [zstd, "-T0", "-10", "--quiet", "--stdout"],
            stdin=subprocess.PIPE,
            stdout=partial_output,
            stderr=subprocess.PIPE,
        )
    except BaseException:
        partial_output.close()
        _kill_and_wait(remote)
        _close_pipe(remote.stdout)
        _close_pipe(remote.stderr)
        raise
    partial_output.close()
    if remote.stdout is None or compressor.stdin is None:
        _kill_and_wait(remote)
        _kill_and_wait(compressor)
        _close_pipe(remote.stdout)
        _close_pipe(remote.stderr)
        _close_pipe(compressor.stdin)
        _close_pipe(compressor.stderr)
        raise ImagingError("could not establish the capture stream")
    raw_hash = hashlib.sha256()
    raw_size = 0
    remote_error = ""
    compressor_error = ""
    try:
        while True:
            block = remote.stdout.read(4 * 1024 * 1024)
            if not block:
                break
            raw_hash.update(block)
            raw_size += len(block)
            compressor.stdin.write(block)
        compressor.stdin.close()
        compressor_code = compressor.wait()
        remote_code = remote.wait()
        remote_error = _read_process_error(remote)
        compressor_error = _read_process_error(compressor)
    except BaseException as exc:
        _kill_and_wait(remote)
        _kill_and_wait(compressor)
        remote_error = _read_process_error(remote)
        compressor_error = _read_process_error(compressor)
        if isinstance(exc, (BrokenPipeError, OSError)):
            raise ImagingError(
                "capture pipeline failed: %s %s; partial kept at %s"
                % (remote_error, compressor_error, partial)
            ) from exc
        raise
    finally:
        _close_pipe(remote.stdout)
        _close_pipe(remote.stderr)
        _close_pipe(compressor.stdin)
        _close_pipe(compressor.stderr)
    if remote_code or compressor_code:
        raise ImagingError(
            "capture interrupted (recovery=%d, zstd=%d): %s %s; partial kept at %s"
            % (remote_code, compressor_code, remote_error, compressor_error, partial)
        )
    if raw_size != expected_size:
        raise ImagingError(
            "capture returned %d bytes but device declared %d; partial kept at %s"
            % (raw_size, expected_size, partial)
        )
    raw_digest = raw_hash.hexdigest()

    second_pass = client.hash_device(device)
    if _validated_hash_response(second_pass, raw_size) != raw_digest:
        raise ImagingError(
            "second raw-device hash did not match; partial kept at %s" % (partial,)
        )
    test = subprocess.run(
        [zstd, "--test", "--quiet", str(partial)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if test.returncode:
        raise ImagingError("zstd integrity test failed; partial capture was kept")

    compressed_digest = sha256_file(partial)
    _fsync_file_and_directory(partial)
    os.replace(partial, output)
    _fsync_file_and_directory(output)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "T300 eMMC over marked recovery USB and direct Ethernet",
        "machine": inspection,
        "raw_size": raw_size,
        "raw_sha256": raw_digest,
        "compressed_size": output.stat().st_size,
        "compressed_sha256": compressed_digest,
        "compression": "zstd",
        "image_file": output.name,
        "second_device_pass_sha256": second_pass["sha256"],
    }
    _write_json_atomic(manifest_path, manifest)
    return manifest_path


def load_manifest(path: Path) -> dict[str, Any]:
    path = _existing_regular_file(path, "image manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImagingError("could not read image manifest: %s" % (exc,)) from exc
    if not isinstance(value, dict) or value.get("schema_version") != MANIFEST_SCHEMA:
        raise ImagingError("unsupported image manifest")
    for key in ("raw_sha256", "compressed_sha256"):
        digest = value.get(key)
        if not isinstance(digest, str) or not MACHINE_ID_RE.fullmatch(digest):
            raise ImagingError("manifest field %s is malformed" % (key,))
    for key in ("raw_size", "compressed_size"):
        size = value.get(key)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ImagingError("manifest field %s is malformed" % (key,))
    image_file = value.get("image_file")
    if (
        not isinstance(image_file, str)
        or not image_file
        or Path(image_file).name != image_file
    ):
        raise ImagingError("manifest image filename is malformed")
    machine = value.get("machine")
    if not isinstance(machine, dict):
        raise ImagingError("manifest machine identity is malformed")
    machine_id = machine.get("machine_id")
    if not isinstance(machine_id, str) or MACHINE_ID_RE.fullmatch(machine_id) is None:
        raise ImagingError("manifest machine identity is malformed")
    _check_inspection(machine, "capture")
    if value.get("compression") != "zstd":
        raise ImagingError("manifest compression is unsupported")
    return value


def verify_image(image: Path, manifest_path: Path) -> dict[str, Any]:
    image = _existing_regular_file(image, "compressed recovery image")
    manifest = load_manifest(manifest_path)
    if image.name != manifest.get("image_file"):
        raise ImagingError("image filename differs from its manifest")
    if image.stat().st_size != int(manifest.get("compressed_size", -1)):
        raise ImagingError("compressed image size differs from its manifest")
    if sha256_file(image) != manifest["compressed_sha256"]:
        raise ImagingError("compressed image SHA-256 differs from its manifest")

    zstd = _require_program("zstd")
    process = subprocess.Popen(
        [zstd, "--decompress", "--stdout", "--quiet", str(image)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        _kill_and_wait(process)
        _close_pipe(process.stderr)
        raise ImagingError("could not open zstd verification stream")
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            block = process.stdout.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        code = process.wait()
        detail = _read_process_error(process)
        if code:
            raise ImagingError("zstd verification failed: %s" % (detail,))
    finally:
        if process.poll() is None:
            _kill_and_wait(process)
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)
    if size != int(manifest.get("raw_size", -1)):
        raise ImagingError("decompressed image size differs from its manifest")
    if digest.hexdigest() != manifest["raw_sha256"]:
        raise ImagingError("decompressed image SHA-256 differs from its manifest")
    return manifest


def _run_local(
    arguments: list[str], timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImagingError("local verification command failed: %s" % arguments[0]) from exc
    if (
        len(result.stdout.encode("utf-8", "replace")) > MAX_LOCAL_COMMAND_OUTPUT
        or len(result.stderr.encode("utf-8", "replace")) > MAX_LOCAL_COMMAND_OUTPUT
    ):
        raise ImagingError("local verification command returned too much output")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no detail"
        raise ImagingError(
            "local verification command failed: %s: %s"
            % (Path(arguments[0]).name, detail)
        )
    return result


def _verification_workspace(path: Path) -> Path:
    requested = path.expanduser().absolute()
    try:
        info = requested.lstat()
    except OSError as exc:
        raise ImagingError("filesystem-check workspace is unavailable") from exc
    if requested.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ImagingError("filesystem-check workspace must be one real directory")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
        raise ImagingError(
            "writable filesystem-check workspace must have the sticky bit"
        )
    return requested.resolve(strict=True)


def _materialize_verified_raw(
    image: Path, manifest: dict[str, Any], destination: Path
) -> None:
    zstd = _require_program("zstd")
    expected_size = int(manifest["raw_size"])
    expected_digest = manifest["raw_sha256"]
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    process = subprocess.Popen(
        [zstd, "--decompress", "--stdout", "--quiet", str(image)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        os.close(descriptor)
        _kill_and_wait(process)
        _close_pipe(process.stderr)
        raise ImagingError("could not open the filesystem-check decompression stream")
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            while True:
                block = process.stdout.read(4 * 1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > expected_size:
                    raise ImagingError("decompressed image exceeds its declared size")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        code = process.wait()
        detail = _read_process_error(process)
        if code:
            raise ImagingError("filesystem-check decompression failed: %s" % detail)
    except BaseException:
        if process.poll() is None:
            _kill_and_wait(process)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise ImagingError("materialized image differs from its verified manifest")


def _read_only(value: Any) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def _has_mountpoint(node: dict[str, Any]) -> bool:
    points = node.get("mountpoints")
    if points is None:
        return False
    if not isinstance(points, list) or any(
        item is not None and not isinstance(item, str) for item in points
    ):
        raise ImagingError("lsblk returned malformed mountpoint data")
    return any(bool(item) for item in points)


def _validated_loop_partitions(
    raw: dict[str, Any], loop: str, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    devices = raw.get("blockdevices") if isinstance(raw, dict) else None
    if not isinstance(devices, list) or len(devices) != 1:
        raise ImagingError("lsblk did not return exactly one loop device")
    root = devices[0]
    if (
        not isinstance(root, dict)
        or root.get("path") != loop
        or root.get("type") != "loop"
        or not _read_only(root.get("ro"))
        or root.get("size") != manifest["raw_size"]
        or _has_mountpoint(root)
    ):
        raise ImagingError("loop device is not the expected unmounted read-only image")
    children = root.get("children")
    if not isinstance(children, list):
        raise ImagingError("loop device has no partition children")

    table = manifest["machine"]["partition_table"]
    sector_size = table["sector_size"]
    expected: dict[int, dict[str, Any]] = {}
    for partition in table["partitions"]:
        match = re.search(r"p([0-9]+)$", partition["node"])
        if match is None:
            raise ImagingError("manifest partition node is malformed")
        expected[int(match.group(1))] = partition
    actual: dict[int, dict[str, Any]] = {}
    for node in children:
        if not isinstance(node, dict):
            raise ImagingError("lsblk returned a malformed partition")
        match = re.fullmatch(re.escape(loop) + r"p([0-9]+)", str(node.get("path", "")))
        number = int(match.group(1)) if match is not None else -1
        filesystem = node.get("fstype")
        if (
            number <= 0
            or number in actual
            or node.get("type") != "part"
            or not _read_only(node.get("ro"))
            or _has_mountpoint(node)
            or node.get("children") not in (None, [])
            or filesystem not in SUPPORTED_IMAGE_FILESYSTEMS
        ):
            raise ImagingError("loop partition is unsupported, writable, or mounted")
        actual[number] = node
    if set(actual) != set(expected):
        raise ImagingError("loop partitions differ from the captured partition table")
    for number, node in actual.items():
        if node.get("size") != expected[number]["size"] * sector_size:
            raise ImagingError("loop partition size differs from its capture manifest")
    return [actual[number] for number in sorted(actual)]


def _check_mounted_read_only(findmnt_output: str, partition: str) -> None:
    try:
        value = json.loads(findmnt_output)
    except json.JSONDecodeError as exc:
        raise ImagingError("findmnt returned malformed JSON") from exc
    filesystems = value.get("filesystems") if isinstance(value, dict) else None
    if not isinstance(filesystems, list) or len(filesystems) != 1:
        raise ImagingError("findmnt did not report one verification mount")
    record = filesystems[0]
    source = record.get("source") if isinstance(record, dict) else None
    options = record.get("options") if isinstance(record, dict) else None
    if (
        not isinstance(source, str)
        or os.path.realpath(source) != os.path.realpath(partition)
        or not isinstance(options, str)
        or "ro" not in options.split(",")
    ):
        raise ImagingError("verification mount is not sourced read-only from the loop partition")


def _check_one_filesystem(partition: dict[str, Any], mount_root: Path) -> dict[str, Any]:
    path = partition["path"]
    filesystem = partition["fstype"]
    if filesystem in {"ext2", "ext3", "ext4"}:
        checker = _require_program("e2fsck")
        check_arguments = [checker, "-f", "-n", path]
        mount_options = "ro,nosuid,nodev,noexec"
        if filesystem in {"ext3", "ext4"}:
            mount_options += ",noload"
    elif filesystem == "vfat":
        checker = _require_program("fsck.vfat")
        check_arguments = [checker, "-n", path]
        mount_options = "ro,nosuid,nodev,noexec"
    else:
        raise ImagingError("unsupported filesystem in captured image")
    _run_local(check_arguments, timeout=1800)

    mount_program = _require_program("mount")
    umount_program = _require_program("umount")
    findmnt = _require_program("findmnt")
    mountpoint = mount_root / Path(path).name
    mountpoint.mkdir(mode=0o700)
    mounted = False
    try:
        _run_local(
            [
                mount_program,
                "--types",
                filesystem,
                "--options",
                mount_options,
                path,
                str(mountpoint),
            ]
        )
        mounted = True
        mounted_record = _run_local(
            [findmnt, "--json", "--output", "SOURCE,OPTIONS", "--target", str(mountpoint)]
        )
        _check_mounted_read_only(mounted_record.stdout, path)
        try:
            next(os.scandir(mountpoint), None)
        except OSError as exc:
            raise ImagingError("read-only verification mount could not be read") from exc
    except BaseException:
        if mounted:
            try:
                _run_local([umount_program, str(mountpoint)])
                mounted = False
            except ImagingError as cleanup:
                raise ImagingError(
                    "filesystem verification failed and its read-only mount remains attached"
                ) from cleanup
        raise
    if mounted:
        _run_local([umount_program, str(mountpoint)])
    return {
        "partition": path,
        "filesystem": filesystem,
        "fsck_mode": "read-only",
        "mounted_read_only": True,
    }


def verify_image_filesystems(
    image: Path, manifest_path: Path, workspace: Path
) -> dict[str, Any]:
    """Materialize, fsck, and mount each captured filesystem without writes."""
    if os.geteuid() != 0:
        raise ImagingError("filesystem verification requires root on the laptop")
    image = _existing_regular_file(image, "compressed recovery image")
    manifest_path = _existing_regular_file(manifest_path, "image manifest")
    manifest = verify_image(image, manifest_path)
    workspace = _verification_workspace(workspace)
    required = int(manifest["raw_size"]) + FILESYSTEM_VERIFY_RESERVE_BYTES
    if shutil.disk_usage(workspace).free < required:
        raise ImagingError("filesystem-check workspace lacks raw-image headroom")

    temporary = Path(tempfile.mkdtemp(prefix=".t300-image-verify-", dir=workspace))
    os.chmod(temporary, 0o700)
    raw_path = temporary / "capture.raw"
    loop: str | None = None
    primary: BaseException | None = None
    result: dict[str, Any] | None = None
    try:
        _materialize_verified_raw(image, manifest, raw_path)
        losetup = _require_program("losetup")
        loop = _run_local(
            [losetup, "--find", "--show", "--read-only", "--partscan", str(raw_path)]
        ).stdout.strip()
        if LOOP_DEVICE_RE.fullmatch(loop) is None:
            raise ImagingError("losetup returned an unexpected device")
        udevadm = _require_program("udevadm")
        _run_local([udevadm, "settle", "--timeout=10"])
        lsblk = _require_program("lsblk")
        layout = _run_local(
            [
                lsblk,
                "--json",
                "--bytes",
                "--paths",
                "--output",
                "PATH,TYPE,FSTYPE,RO,SIZE,MOUNTPOINTS",
                loop,
            ]
        )
        try:
            layout_json = json.loads(layout.stdout)
        except json.JSONDecodeError as exc:
            raise ImagingError("lsblk returned malformed JSON") from exc
        partitions = _validated_loop_partitions(layout_json, loop, manifest)
        mount_root = temporary / "mounts"
        mount_root.mkdir(mode=0o700)
        checked = [
            _check_one_filesystem(partition, mount_root)
            for partition in partitions
        ]
        result = {
            "schema_version": 1,
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "image_sha256": manifest["compressed_sha256"],
            "raw_sha256": manifest["raw_sha256"],
            "raw_size": manifest["raw_size"],
            "machine_id": manifest["machine"]["machine_id"],
            "loop_read_only": True,
            "filesystems": checked,
            "passed": True,
        }
    except BaseException as exc:
        primary = exc
    if loop is not None:
        try:
            _run_local([_require_program("losetup"), "--detach", loop])
            loop = None
        except ImagingError as exc:
            primary = ImagingError(
                "read-only loop cleanup failed; inspect %s before retrying" % temporary
            )
    if loop is None:
        shutil.rmtree(temporary)
    if primary is not None:
        raise primary
    if result is None:
        raise ImagingError("filesystem verification produced no result")
    return result


def write_image(
    client: RecoveryClient,
    device: str,
    image: Path,
    manifest_path: Path,
    apply: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    if not apply:
        raise ImagingError("write is a dry run unless --apply is supplied")
    manifest = verify_image(image, manifest_path)
    inspection = client.inspect(device)
    _check_inspection(inspection, "write")
    if inspection["target_device"] != _validated_device(device):
        raise ImagingError("recovery inspection returned another target device")
    captured_machine = manifest.get("machine", {}).get("machine_id")
    if captured_machine != inspection.get("machine_id"):
        raise ImagingError("image was captured from a different machine identity")
    if int(manifest["raw_size"]) != int(inspection["target_size"]):
        raise ImagingError("image size does not exactly match the target eMMC")
    if manifest["machine"].get("block_geometry") != inspection.get("block_geometry"):
        raise ImagingError("target eMMC sector geometry differs from the capture")
    expected_confirmation = "WRITE %s" % (inspection["machine_id"],)
    if confirmation != expected_confirmation:
        raise ImagingError("typed confirmation must be exactly: %s" % (expected_confirmation,))

    zstd = _require_program("zstd")
    decompressor = subprocess.Popen(
        [zstd, "--decompress", "--stdout", "--quiet", str(image)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        remote = subprocess.Popen(
            client.command(
                "write",
                "--device",
                _validated_device(device),
                "--image-size",
                str(manifest["raw_size"]),
                "--image-sha256",
                manifest["raw_sha256"],
                "--apply",
                "--confirm",
                confirmation,
            ),
            stdin=decompressor.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except BaseException:
        decompressor.kill()
        decompressor.wait()
        raise
    if decompressor.stdout is not None:
        decompressor.stdout.close()
    try:
        remote_out, remote_error = remote.communicate()
        decompressor_code = decompressor.wait()
        decompressor_error = _read_process_error(decompressor)
    finally:
        if remote.poll() is None:
            _kill_and_wait(remote)
        if decompressor.poll() is None:
            _kill_and_wait(decompressor)
        _close_pipe(remote.stdout)
        _close_pipe(remote.stderr)
        _close_pipe(decompressor.stdout)
        _close_pipe(decompressor.stderr)
    if remote.returncode or decompressor_code:
        raise ImagingError(
            "write failed (recovery=%d, zstd=%d): %s %s"
            % (
                remote.returncode,
                decompressor_code,
                remote_error.decode("utf-8", "replace").strip(),
                decompressor_error,
            )
        )
    try:
        result = json.loads(remote_out.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImagingError("recovery write response was invalid") from exc
    if not isinstance(result, dict):
        raise ImagingError("recovery write response was malformed")
    if result.get("bytes_written") != int(manifest["raw_size"]):
        raise ImagingError("recovery agent reported a short write")
    if result.get("image_sha256") != manifest["raw_sha256"]:
        raise ImagingError("recovery agent received different image bytes")
    second_pass = client.hash_device(device)
    if (
        _validated_hash_response(second_pass, int(manifest["raw_size"]))
        != manifest["raw_sha256"]
    ):
        raise ImagingError("post-write eMMC hash does not match the image")
    return second_pass

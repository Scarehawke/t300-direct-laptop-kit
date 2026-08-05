#!/usr/bin/env python3
"""Root-side guard used only from the marked T300 Armbian recovery USB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Iterable


EXPECTED_BOARD = "MKS-Klipad50"
MARKER_PATH = Path("/etc/t300-recovery.json")
BOOT_HISTORY_PATH = Path("/var/lib/t300-recovery/boot-history.json")
DEVICE_RE = re.compile(r"^/dev/mmcblk[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CID_RE = re.compile(r"^[0-9a-f]{32}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
MAX_COMMAND_OUTPUT = 8 * 1024 * 1024


class RecoveryError(RuntimeError):
    pass


def _run(*arguments: str) -> str:
    try:
        result = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("%s could not complete: %s" % (arguments[0], exc)) from exc
    if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
        raise RecoveryError("%s returned too much output" % (arguments[0],))
    if result.returncode:
        raise RecoveryError(
            "%s failed: %s" % (arguments[0], result.stderr.strip())
        )
    return result.stdout.strip()


def _device(value: str) -> str:
    if not DEVICE_RE.fullmatch(value):
        raise RecoveryError("target must be one whole /dev/mmcblkN device")
    try:
        mode = os.stat(value).st_mode
    except OSError as exc:
        raise RecoveryError("target is unavailable: %s" % (exc,)) from exc
    if not stat.S_ISBLK(mode):
        raise RecoveryError("target is not a block device")
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().rstrip(b"\x00\n").decode("utf-8", "replace")
    except OSError as exc:
        raise RecoveryError("could not read %s: %s" % (path, exc)) from exc


def _load_marker() -> dict[str, Any]:
    try:
        info = MARKER_PATH.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise RecoveryError("recovery marker is not an immutable root-owned file")
        value = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery marker is unavailable or malformed: %s" % (exc,)) from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RecoveryError("unsupported recovery marker")
    if value.get("board") != EXPECTED_BOARD:
        raise RecoveryError("recovery marker names a different board")
    recovery_id = value.get("recovery_id")
    if not isinstance(recovery_id, str) or UUID_RE.fullmatch(recovery_id) is None:
        raise RecoveryError("recovery marker ID is malformed")
    return value


def _root_source() -> str:
    source = _run("findmnt", "--noheadings", "--output", "SOURCE", "/")
    if not source.startswith("/dev/"):
        raise RecoveryError("recovery root is not a block-device filesystem")
    return os.path.realpath(source)


def _whole_device(path: str) -> str:
    name = os.path.basename(os.path.realpath(path))
    parent = _run("lsblk", "--noheadings", "--output", "PKNAME", path).strip()
    return "/dev/%s" % (parent or name,)


def _transport(path: str) -> str:
    whole = _whole_device(path)
    return _run("lsblk", "--nodeps", "--noheadings", "--output", "TRAN", whole).strip()


def _mounted_nodes(device: str) -> list[dict[str, str]]:
    raw = _run("lsblk", "--json", "--paths", "--output", "NAME,MOUNTPOINTS", device)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecoveryError("lsblk returned malformed JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("blockdevices"), list):
        raise RecoveryError("lsblk returned malformed device data")
    mounted: list[dict[str, str]] = []

    def visit(node: dict[str, Any]) -> None:
        if not isinstance(node, dict):
            raise RecoveryError("lsblk returned malformed node data")
        raw_points = node.get("mountpoints")
        if raw_points is None:
            points: list[str] = []
        elif isinstance(raw_points, list) and all(
            point is None or isinstance(point, str) for point in raw_points
        ):
            points = [point for point in raw_points if point]
        else:
            raise RecoveryError("lsblk returned malformed mountpoint data")
        if points:
            mounted.append({"name": str(node.get("name")), "mountpoints": ",".join(points)})
        children = node.get("children") or []
        if not isinstance(children, list):
            raise RecoveryError("lsblk returned malformed child data")
        for child in children:
            visit(child)

    for block in value.get("blockdevices", []):
        visit(block)
    return mounted


def _block_size(device: str) -> int:
    try:
        size = int(_run("blockdev", "--getsize64", device))
    except ValueError as exc:
        raise RecoveryError("blockdev returned an invalid target size") from exc
    if size <= 0:
        raise RecoveryError("target device has an invalid size")
    return size


def _block_geometry(device: str, size: int) -> dict[str, int]:
    commands = {
        "logical_sector_bytes": ("blockdev", "--getss", device),
        "physical_sector_bytes": ("blockdev", "--getpbsz", device),
        "minimum_io_bytes": ("blockdev", "--getiomin", device),
        "optimal_io_bytes": ("blockdev", "--getioopt", device),
        "sectors_512": ("blockdev", "--getsz", device),
    }
    values: dict[str, int] = {}
    for name, command in commands.items():
        try:
            values[name] = int(_run(*command))
        except ValueError as exc:
            raise RecoveryError("blockdev returned malformed geometry") from exc
    logical = values["logical_sector_bytes"]
    physical = values["physical_sector_bytes"]
    if logical < 512 or logical & (logical - 1):
        raise RecoveryError("target logical sector geometry is invalid")
    if physical < logical or physical % logical:
        raise RecoveryError("target physical sector geometry is invalid")
    if values["minimum_io_bytes"] < logical:
        raise RecoveryError("target minimum-I/O geometry is invalid")
    if values["optimal_io_bytes"] < 0:
        raise RecoveryError("target optimal-I/O geometry is invalid")
    if values["sectors_512"] <= 0 or values["sectors_512"] * 512 != size:
        raise RecoveryError("target sector count does not match its byte size")
    values["device_bytes"] = size
    return values


def _partition_table(device: str, device_size: int) -> dict[str, Any]:
    raw = _run("sfdisk", "--json", device)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecoveryError("sfdisk returned malformed JSON") from exc
    table = value.get("partitiontable") if isinstance(value, dict) else None
    if not isinstance(table, dict):
        raise RecoveryError("sfdisk returned no partition table")
    if os.path.realpath(str(table.get("device", ""))) != os.path.realpath(device):
        raise RecoveryError("partition table identifies another device")
    if table.get("unit") != "sectors" or table.get("label") not in {"dos", "gpt"}:
        raise RecoveryError("partition table label or units are unsupported")
    sector_size = table.get("sectorsize")
    partitions = table.get("partitions")
    if (
        not isinstance(sector_size, int)
        or isinstance(sector_size, bool)
        or sector_size < 512
        or not isinstance(partitions, list)
        or not partitions
    ):
        raise RecoveryError("partition table geometry is malformed")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    node_pattern = re.compile(re.escape(device) + r"p[0-9]+$")
    for item in partitions:
        if not isinstance(item, dict):
            raise RecoveryError("partition table contains malformed entries")
        node = item.get("node")
        start = item.get("start")
        size = item.get("size")
        if (
            not isinstance(node, str)
            or node_pattern.fullmatch(node) is None
            or node in seen
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or start < 0
            or size <= 0
            or (start + size) * sector_size > device_size
        ):
            raise RecoveryError("partition table entry is outside the target")
        seen.add(node)
        normalized.append(
            {
                key: item[key]
                for key in ("node", "start", "size", "type", "uuid", "name", "attrs")
                if key in item
            }
        )
    return {
        "device": device,
        "label": table["label"],
        "id": table.get("id"),
        "unit": "sectors",
        "sector_size": sector_size,
        "partitions": normalized,
    }


def _bootloader_evidence(board: str, compatible: str) -> dict[str, Any]:
    chosen = Path("/sys/firmware/devicetree/base/chosen")
    version_path = chosen / "u-boot,version"
    build_path = chosen / "u-boot,build"

    def optional(path: Path) -> str | None:
        if not path.exists():
            return None
        value = _read_text(path).replace("\x00", "").strip()
        return value or None

    bootargs_path = chosen / "bootargs"
    bootargs = optional(bootargs_path)
    return {
        "device_tree_handoff_identifies_board": (
            board == EXPECTED_BOARD and "klipad50" in compatible.lower()
        ),
        "u_boot_version": optional(version_path),
        "u_boot_build": optional(build_path),
        "bootargs_sha256": (
            hashlib.sha256(bootargs.encode("utf-8")).hexdigest()
            if bootargs is not None
            else None
        ),
    }


def _machine_identity(
    board: str,
    compatible: str,
    device: str,
    size: int,
    sys_block_root: Path = Path("/sys/class/block"),
) -> str:
    sys_name = os.path.basename(device)
    cid_path = sys_block_root / sys_name / "device" / "cid"
    if not cid_path.exists():
        raise RecoveryError("target eMMC has no readable CID identity")
    cid = _read_text(cid_path).strip().lower()
    if CID_RE.fullmatch(cid) is None:
        raise RecoveryError("target eMMC CID identity is malformed")
    material = "\n".join((board, compatible, cid, str(size))).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _history_directory(create: bool) -> Path | None:
    directory = BOOT_HISTORY_PATH.parent
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
            info = directory.lstat()
        except OSError as exc:
            raise RecoveryError(
                "boot-history directory could not be created: %s" % (exc,)
            ) from exc
    except OSError as exc:
        raise RecoveryError("boot-history directory is unavailable: %s" % (exc,)) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RecoveryError("boot-history directory ownership or permissions are unsafe")
    return directory


def _load_boot_history(recovery_id: str, machine_id: str) -> list[str]:
    if _history_directory(create=False) is None:
        return []
    try:
        BOOT_HISTORY_PATH.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RecoveryError("boot-history file is unavailable: %s" % (exc,)) from exc
    try:
        info = BOOT_HISTORY_PATH.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RecoveryError("boot-history file ownership or permissions are unsafe")
        value = json.loads(BOOT_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("boot-history file is malformed: %s" % (exc,)) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("recovery_id") != recovery_id
        or value.get("machine_id") != machine_id
        or not isinstance(value.get("boot_ids"), list)
        or any(
            not isinstance(item, str) or UUID_RE.fullmatch(item) is None
            for item in value.get("boot_ids", [])
        )
    ):
        raise RecoveryError("boot-history content is malformed")
    history = value["boot_ids"]
    if len(history) != len(set(history)):
        raise RecoveryError("boot-history contains duplicate boot IDs")
    return history


def _record_boot(
    history: list[str],
    recovery_id: str,
    machine_id: str,
    apply: bool,
    confirmation: str | None,
) -> list[str]:
    if not apply or confirmation != "RECORD-USB-BOOT":
        raise RecoveryError(
            "recording a boot requires --apply --confirm RECORD-USB-BOOT"
        )
    boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id"))
    if UUID_RE.fullmatch(boot_id) is None:
        raise RecoveryError("kernel boot ID is malformed")
    if boot_id not in history:
        history.append(boot_id)
    directory = _history_directory(create=True)
    if directory is None:
        raise RecoveryError("boot-history directory could not be created")
    temporary = BOOT_HISTORY_PATH.with_name(".boot-history.json.partial")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        value = {
            "schema_version": 1,
            "recovery_id": recovery_id,
            "machine_id": machine_id,
            "boot_ids": history,
        }
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, BOOT_HISTORY_PATH)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise RecoveryError("could not write boot history: %s" % (exc,)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return history


def inspect_target(
    device: str,
    record_boot: bool = False,
    apply: bool = False,
    confirmation: str | None = None,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RecoveryError("recovery agent must run as root")
    marker = _load_marker()
    device = _device(device)
    board_raw = _read_text(Path("/sys/firmware/devicetree/base/model"))
    compatible = _read_text(Path("/sys/firmware/devicetree/base/compatible"))
    board = EXPECTED_BOARD if "Klipad50" in board_raw else board_raw
    root_source = _root_source()
    root_whole = _whole_device(root_source)
    root_transport = _transport(root_source)
    target_size = _block_size(device)
    block_geometry = _block_geometry(device, target_size)
    partition_table = _partition_table(device, target_size)
    target_mounts = _mounted_nodes(device)
    machine_id = _machine_identity(board, compatible, device, target_size)
    bootloader = _bootloader_evidence(board, compatible)
    history = _load_boot_history(marker["recovery_id"], machine_id)

    blocked: list[str] = []
    if marker.get("board") != EXPECTED_BOARD or board != EXPECTED_BOARD:
        blocked.append("board/device-tree identity mismatch")
    if "klipad50" not in compatible.lower():
        blocked.append("device-tree compatible does not identify Klipad50")
    if not bootloader["device_tree_handoff_identifies_board"]:
        blocked.append("bootloader device-tree handoff does not identify Klipad50")
    if root_transport.lower() != "usb":
        blocked.append("running root filesystem is not on USB")
    if os.path.realpath(root_whole) == os.path.realpath(device):
        blocked.append("target device contains the running root filesystem")
    if target_mounts:
        blocked.append("target device or a target partition is mounted")
    if not os.path.basename(device).startswith("mmcblk"):
        blocked.append("target is not eMMC/MMC storage")

    # A boot is only eligible for recording after all immutable identity and
    # root-on-USB gates pass. Existing target mounts remain a blocker.
    if record_boot:
        if blocked:
            raise RecoveryError("cannot record unsafe recovery boot: %s" % ("; ".join(blocked),))
        history = _record_boot(
            history,
            marker["recovery_id"],
            machine_id,
            apply,
            confirmation,
        )

    safe = not blocked and len(history) >= 3
    return {
        "schema_version": 1,
        "board": board,
        "board_model_raw": board_raw,
        "compatible": compatible.replace("\x00", ","),
        "marker_id": marker.get("recovery_id"),
        "kernel": _run("uname", "-r"),
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "verified_usb_boots": len(history),
        "root_source": root_source,
        "root_whole_device": root_whole,
        "root_transport": root_transport,
        "target_device": device,
        "target_size": target_size,
        "block_geometry": block_geometry,
        "partition_table": partition_table,
        "bootloader": bootloader,
        "target_mounts": target_mounts,
        "machine_id": machine_id,
        "blocked_reasons": blocked,
        "safe_for_capture": safe,
        "safe_for_write": safe,
    }


def _require_safe(device: str, purpose: str) -> dict[str, Any]:
    inspection = inspect_target(device)
    if not inspection.get("safe_for_%s" % (purpose,)):
        raise RecoveryError(
            "target is not safe for %s: %s"
            % (purpose, "; ".join(inspection["blocked_reasons"]) or "three boots not verified")
        )
    return inspection


def stream_device(device: str) -> None:
    inspection = _require_safe(device, "capture")
    remaining = int(inspection["target_size"])
    fd = os.open(device, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        output = sys.stdout.buffer
        while remaining:
            block = os.read(fd, min(4 * 1024 * 1024, remaining))
            if not block:
                raise RecoveryError("unexpected end of target device")
            output.write(block)
            remaining -= len(block)
        output.flush()
    finally:
        os.close(fd)


def hash_device(device: str) -> dict[str, Any]:
    inspection = _require_safe(device, "capture")
    expected = int(inspection["target_size"])
    digest = hashlib.sha256()
    size = 0
    with open(device, "rb", buffering=0) as handle:
        while size < expected:
            block = handle.read(min(4 * 1024 * 1024, expected - size))
            if not block:
                raise RecoveryError("unexpected end of target during hash")
            digest.update(block)
            size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


def write_device(
    device: str,
    image_size: int,
    image_sha256: str,
    apply: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    inspection = _require_safe(device, "write")
    if not apply:
        raise RecoveryError("write requires --apply")
    expected = "WRITE %s" % (inspection["machine_id"],)
    if confirmation != expected:
        raise RecoveryError("typed confirmation must be exactly: %s" % (expected,))
    if image_size != int(inspection["target_size"]):
        raise RecoveryError("incoming image size does not exactly match target eMMC")
    if SHA256_RE.fullmatch(image_sha256) is None:
        raise RecoveryError("incoming image SHA-256 is malformed")
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_SYNC", 0)
    fd = os.open(device, flags)
    written = 0
    digest = hashlib.sha256()
    try:
        while written < image_size:
            block = sys.stdin.buffer.read(min(4 * 1024 * 1024, image_size - written))
            if not block:
                raise RecoveryError("incoming image ended before the target was complete")
            digest.update(block)
            view = memoryview(block)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise RecoveryError("target write made no progress")
                view = view[count:]
                written += count
        if sys.stdin.buffer.read(1):
            raise RecoveryError("incoming image is larger than the target")
        os.fsync(fd)
    finally:
        os.close(fd)
    _run("blockdev", "--flushbufs", device)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != image_sha256:
        raise RecoveryError("incoming image SHA-256 changed during transfer")
    return {
        "bytes_written": written,
        "target_device": device,
        "image_sha256": actual_sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--device", required=True)
    inspect_parser.add_argument("--record-boot", action="store_true")
    inspect_parser.add_argument("--apply", action="store_true")
    inspect_parser.add_argument("--confirm")
    stream_parser = subparsers.add_parser("stream")
    stream_parser.add_argument("--device", required=True)
    hash_parser = subparsers.add_parser("hash")
    hash_parser.add_argument("--device", required=True)
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--device", required=True)
    write_parser.add_argument("--image-size", type=int, required=True)
    write_parser.add_argument("--image-sha256", required=True)
    write_parser.add_argument("--apply", action="store_true")
    write_parser.add_argument("--confirm")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_target(
                args.device,
                record_boot=args.record_boot,
                apply=args.apply,
                confirmation=args.confirm,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "stream":
            stream_device(args.device)
        elif args.command == "hash":
            print(json.dumps(hash_device(args.device), sort_keys=True))
        elif args.command == "write":
            print(json.dumps(write_device(
                args.device,
                args.image_size,
                args.image_sha256,
                args.apply,
                args.confirm,
            ), sort_keys=True))
        return 0
    except RecoveryError as exc:
        print("Error: %s" % (exc,), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

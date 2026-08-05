"""Deterministic, fail-closed T300 configuration deployment bundles."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import Any, Iterable

from .commissioning import (
    CandidateController,
    CommissioningError,
    HOST_MARKER,
    HOST_UNITS,
    MAINTENANCE_MARKER,
    NORMAL_PRINTER_UNITS,
    PRODUCTION_MARKER,
    RELEASE_MARKER,
    SSH_UNIT,
    STORAGE_MARKER,
    TRANSPORT_MARKER,
    inventory_configuration_tree,
)
from .lockfile import sha256_file
from .provision import ProvisionError, verify_stage
from .transfer import INCOMING_BUNDLE, INCOMING_DIRECTORY


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 4096
MAX_EVIDENCE_BYTES = 1024 * 1024
REQUIRED_VALIDATION_CHECKS = (
    "stage_verified",
    "unit_tests_passed",
    "vendor_v012_harness_passed",
    "klipper_v013_harness_passed",
    "gcode_policy_tests_passed",
    "klipper_lifecycle_reviewed",
    "systemd_units_reviewed",
    "host_network_boundary_reviewed",
    "secret_scan_passed",
)
VALIDATION_GENERATOR = "t300-validation-v1"
REQUIRED_IDLE_CHECKS = (
    "owner_at_printer",
    "printer_idle",
    "hotend_target_zero",
    "bed_target_zero",
    "hotend_below_50c",
    "bed_below_50c",
    "normal_services_stopped",
)
JOURNAL_PATH = "/var/lib/t300/config-deploy-journal.json"
BACKUP_ROOT = "/var/lib/t300/config-backups"


class ConfigDeployError(RuntimeError):
    pass


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigDeployError("could not read JSON: %s" % path) from exc
    if not isinstance(value, dict):
        raise ConfigDeployError("JSON root must be an object: %s" % path)
    return value


def _write_json_atomic(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ConfigDeployError("bundle contains an unsafe path")
    return path


def _normalized_mode(relative: Path, is_directory: bool) -> int:
    private = relative.parts[:2] == ("klipper", "private")
    if is_directory:
        return 0o750 if private else 0o555
    return 0o440 if private else 0o444


def _copy_normalized_config(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ConfigDeployError("staged configuration root is missing or unsafe")
    destination.mkdir(mode=0o755)
    directories: list[tuple[Path, Path]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0] == "commissioning":
            raise ConfigDeployError("a deployment bundle may not supply commissioning markers")
        target = destination / relative
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise ConfigDeployError("staged configuration contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir()
            directories.append((target, relative))
        elif stat.S_ISREG(info.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(_normalized_mode(relative, False))
        else:
            raise ConfigDeployError("staged configuration contains a special file")
    for target, relative in sorted(directories, key=lambda item: len(item[1].parts), reverse=True):
        target.chmod(_normalized_mode(relative, True))
    destination.chmod(0o555)


def _validate_inventory(value: dict[str, Any]) -> dict[str, Any]:
    records = value.get("files")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "t300-config-inventory"
        or not isinstance(value.get("candidate_sha256"), str)
        or SHA256_RE.fullmatch(value["candidate_sha256"]) is None
        or not isinstance(value.get("config_sha256"), str)
        or SHA256_RE.fullmatch(value["config_sha256"]) is None
        or not isinstance(records, list)
    ):
        raise ConfigDeployError("base inventory is malformed")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("type") not in {"file", "directory"}:
            raise ConfigDeployError("base inventory record is malformed")
        name = record.get("path")
        if not isinstance(name, str) or name in seen:
            raise ConfigDeployError("base inventory path is missing or duplicated")
        _safe_relative(name)
        seen.add(name)
    return value


def _validate_report(value: dict[str, Any]) -> dict[str, Any]:
    checks = value.get("checks")
    evidence = value.get("evidence")
    if (
        value.get("schema_version") != 1
        or value.get("generated_by") != VALIDATION_GENERATOR
        or not isinstance(checks, dict)
        or not isinstance(evidence, dict)
    ):
        raise ConfigDeployError("validation report is malformed")
    missing = [name for name in REQUIRED_VALIDATION_CHECKS if checks.get(name) is not True]
    if missing:
        raise ConfigDeployError("validation report checks failed: %s" % ", ".join(missing))
    if any(
        not isinstance(evidence.get(name), dict)
        or evidence[name].get("passed") is not True
        for name in REQUIRED_VALIDATION_CHECKS
    ):
        raise ConfigDeployError("validation report lacks passing evidence")
    return value


def _record_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {record["path"]: record for record in records if record["type"] == "file"}


def _diff_records(
    old_records: list[dict[str, Any]], new_records: list[dict[str, Any]]
) -> dict[str, list[str]]:
    old = _record_map(old_records)
    new = _record_map(new_records)
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(
            name
            for name in set(old) & set(new)
            if old[name].get("sha256") != new[name].get("sha256")
            or old[name].get("mode") != new[name].get("mode")
        ),
    }


def _tar_add_bytes(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as handle:
        handle.write(content)
        handle.seek(0)
        archive.addfile(info, handle)


def _tar_add_tree(archive: tarfile.TarFile, root: Path) -> None:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        name = "config/" + relative
        info = tarfile.TarInfo(name)
        source_info = os.lstat(path)
        info.mode = source_info.st_mode & 0o777
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = 0
        if path.is_dir():
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        elif path.is_file() and not path.is_symlink():
            info.size = source_info.st_size
            with path.open("rb") as handle:
                archive.addfile(info, handle)
        else:
            raise ConfigDeployError("normalized configuration contains an unsafe object")


def prepare_bundle(
    stage: Path,
    stage_manifest_sha256: str,
    base_inventory_path: Path,
    validation_report_path: Path,
    output: Path,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ConfigDeployError("output bundle already exists")
    try:
        stage_info = verify_stage(stage, stage_manifest_sha256)
    except ProvisionError as exc:
        raise ConfigDeployError(str(exc)) from exc
    base = _validate_inventory(_read_json(base_inventory_path))
    report = _validate_report(_read_json(validation_report_path))
    if report.get("stage_manifest_sha256") != stage_manifest_sha256:
        raise ConfigDeployError("validation report belongs to another staged build")
    with tempfile.TemporaryDirectory(prefix="t300-config-bundle-") as directory:
        normalized = Path(directory) / "config"
        _copy_normalized_config(stage_info["root"] / "etc/t300", normalized)
        new_inventory = inventory_configuration_tree(normalized, strict_owner=False)
        metadata = stage_info["manifest"]["metadata"]
        calibration_ready = metadata.get("calibration_ready") is True
        safety = (normalized / "klipper/safety.cfg").read_text(encoding="utf-8")
        expected_lock = not calibration_ready
        lock_enabled = bool(
            re.search(r"^commissioning_lock:\s*True\s*$", safety, re.MULTILINE)
        )
        lock_disabled = bool(
            re.search(r"^commissioning_lock:\s*False\s*$", safety, re.MULTILINE)
        )
        if lock_enabled == lock_disabled or lock_enabled != expected_lock:
            raise ConfigDeployError("calibration state and commissioning lock disagree")
        report_bytes = _canonical_json(report)
        manifest = {
            "schema_version": 1,
            "kind": "t300-config-deployment",
            "base_candidate_sha256": base["candidate_sha256"],
            "base_config_sha256": base["config_sha256"],
            "new_config_sha256": new_inventory["config_sha256"],
            "stage_manifest_sha256": stage_manifest_sha256,
            "calibration_ready": calibration_ready,
            "commissioning_lock": lock_enabled,
            "validation_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "files": new_inventory["files"],
            "diff": _diff_records(base["files"], new_inventory["files"]),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % output.name, dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
                _tar_add_bytes(archive, "deployment.json", _canonical_json(manifest), 0o444)
                _tar_add_bytes(archive, "validation-report.json", report_bytes, 0o444)
                _tar_add_tree(archive, normalized)
            if temporary.stat().st_size > MAX_BUNDLE_BYTES:
                raise ConfigDeployError("configuration bundle exceeds its size ceiling")
            os.chmod(temporary, 0o400)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "bundle": str(output),
        "bundle_sha256": sha256_file(output),
        "manifest": manifest,
    }


def _extract_verified_bundle(
    bundle: Path, expected_sha256: str, destination: Path
) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ConfigDeployError("bundle SHA-256 is malformed")
    snapshot = destination / ".verified-bundle-snapshot"
    source_descriptor = -1
    snapshot_descriptor = -1
    snapshot_ready = False
    try:
        source_descriptor = os.open(
            bundle,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        source_info = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size > MAX_BUNDLE_BYTES:
            raise ConfigDeployError("bundle must be one bounded regular file")
        snapshot_descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            copied += len(block)
            if copied > MAX_BUNDLE_BYTES:
                raise ConfigDeployError("bundle exceeds its size ceiling while reading")
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(snapshot_descriptor, view)
                if written <= 0:
                    raise ConfigDeployError("could not snapshot the deployment bundle")
                view = view[written:]
        os.fsync(snapshot_descriptor)
        os.close(snapshot_descriptor)
        snapshot_descriptor = -1
        if digest.hexdigest() != expected_sha256:
            raise ConfigDeployError("bundle does not match the reviewed SHA-256")
        snapshot.chmod(0o400)
        snapshot_ready = True
    except OSError as exc:
        raise ConfigDeployError("could not safely snapshot the deployment bundle") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        if not snapshot_ready:
            snapshot.unlink(missing_ok=True)
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(snapshot, "r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_BUNDLE_MEMBERS:
                raise ConfigDeployError("bundle member count is empty or excessive")
            for member in members:
                path = _safe_relative(member.name)
                name = path.as_posix()
                if name in seen:
                    raise ConfigDeployError("bundle path is duplicated")
                seen.add(name)
                if not (
                    name in {"deployment.json", "validation-report.json"}
                    or name.startswith("config/")
                ):
                    raise ConfigDeployError("bundle contains an unexpected path")
                if not (member.isfile() or member.isdir()):
                    raise ConfigDeployError("bundle contains a link or special file")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ConfigDeployError("bundle member exceeds its size ceiling")
                total += member.size
            if total > MAX_BUNDLE_BYTES:
                raise ConfigDeployError("bundle payload exceeds its size ceiling")
            if not {"deployment.json", "validation-report.json"}.issubset(seen):
                raise ConfigDeployError("bundle metadata is incomplete")

            directory_modes: list[tuple[Path, int]] = []
            for member in members:
                path = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=False, mode=0o755)
                    directory_modes.append((path, member.mode & 0o777))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ConfigDeployError("bundle member could not be read")
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    member.mode & 0o777,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    shutil.copyfileobj(source, handle, 1024 * 1024)
                    handle.flush()
                    os.fsync(handle.fileno())
            for path, mode in sorted(
                directory_modes, key=lambda item: len(item[0].parts), reverse=True
            ):
                os.chmod(path, mode)
    finally:
        snapshot.unlink(missing_ok=True)
    manifest = _read_json(destination / "deployment.json")
    report = _validate_report(_read_json(destination / "validation-report.json"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "t300-config-deployment"
        or not isinstance(manifest.get("files"), list)
    ):
        raise ConfigDeployError("deployment manifest is malformed")
    report_bytes = _canonical_json(report)
    report_digest = hashlib.sha256(report_bytes).hexdigest()
    if manifest.get("validation_report_sha256") != report_digest:
        raise ConfigDeployError("validation report does not match the deployment manifest")
    config = destination / "config"
    inventory = inventory_configuration_tree(config, strict_owner=False)
    if (
        inventory["config_sha256"] != manifest.get("new_config_sha256")
        or inventory["files"] != manifest["files"]
    ):
        raise ConfigDeployError("bundle configuration differs from its manifest")
    if report.get("stage_manifest_sha256") != manifest.get("stage_manifest_sha256"):
        raise ConfigDeployError("validation report and deployment stage differ")
    return {"manifest": manifest, "report": report, "config": config}


def inspect_bundle(bundle: Path, expected_sha256: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="t300-config-inspect-") as directory:
        value = _extract_verified_bundle(bundle, expected_sha256, Path(directory))
        return {
            "bundle_sha256": expected_sha256,
            "manifest": value["manifest"],
            "validation_checks": value["report"]["checks"],
        }


def _validate_idle_evidence(
    path: Path, candidate_sha256: str, config_sha256: str
) -> str:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > MAX_EVIDENCE_BYTES
        ):
            raise ConfigDeployError("idle evidence must be one bounded regular file")
        blocks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                raise ConfigDeployError("idle evidence changed while it was read")
            blocks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ConfigDeployError("idle evidence changed while it was read")
        after = os.fstat(descriptor)
        if not os.path.samestat(info, after) or info.st_mtime_ns != after.st_mtime_ns:
            raise ConfigDeployError("idle evidence changed while it was read")
    except OSError as exc:
        raise ConfigDeployError("idle evidence must be one bounded regular file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    content = b"".join(blocks)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigDeployError("idle evidence is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ConfigDeployError("idle evidence JSON root must be an object")
    checks = value.get("checks")
    if (
        value.get("schema_version") != 1
        or value.get("candidate_sha256") != candidate_sha256
        or value.get("config_sha256") != config_sha256
        or not isinstance(checks, dict)
    ):
        raise ConfigDeployError("idle evidence belongs to another candidate or configuration")
    missing = [name for name in REQUIRED_IDLE_CHECKS if checks.get(name) is not True]
    if missing:
        raise ConfigDeployError("idle evidence checks failed: %s" % ", ".join(missing))
    try:
        captured = dt.datetime.fromisoformat(value["captured_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigDeployError("idle evidence timestamp is invalid") from exc
    if captured.tzinfo is None:
        raise ConfigDeployError("idle evidence timestamp lacks a timezone")
    age = dt.datetime.now(dt.timezone.utc) - captured.astimezone(dt.timezone.utc)
    if age < dt.timedelta(minutes=-1) or age > dt.timedelta(minutes=10):
        raise ConfigDeployError("idle evidence is not recent")
    return hashlib.sha256(content).hexdigest()


def _chown_root_tree(root: Path) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        if path.is_symlink():
            raise ConfigDeployError("candidate config tree contains a symlink")
        os.chown(path, 0, 0)


def _validate_commissioning_directory(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ConfigDeployError("commissioning marker directory is unsafe")
    directory_info = os.lstat(directory)
    if directory_info.st_uid != 0 or directory_info.st_gid != 0:
        raise ConfigDeployError("commissioning marker directory is not root-owned")
    if stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise ConfigDeployError("commissioning marker directory must have mode 0700")
    allowed = {
        HOST_MARKER,
        STORAGE_MARKER,
        PRODUCTION_MARKER,
        MAINTENANCE_MARKER,
        TRANSPORT_MARKER,
        RELEASE_MARKER,
    }
    for path in directory.iterdir():
        if path.name not in allowed:
            raise ConfigDeployError("unexpected commissioning marker: %s" % path.name)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigDeployError("commissioning marker is not a regular file")
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise ConfigDeployError("commissioning marker is not private to root")


def _update_next_markers(next_root: Path, new_config_sha256: str, bundle_sha256: str) -> None:
    directory = next_root / "commissioning"
    for name in (
        HOST_MARKER,
        PRODUCTION_MARKER,
        TRANSPORT_MARKER,
        RELEASE_MARKER,
    ):
        path = directory / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise ConfigDeployError("commissioning marker is unsafe: %s" % name)
            path.unlink()
    for name in (STORAGE_MARKER,):
        path = directory / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ConfigDeployError("commissioning marker is unsafe: %s" % name)
        value = _read_json(path)
        value["config_sha256"] = new_config_sha256
        value["configuration_deployment_sha256"] = bundle_sha256
        value["configuration_updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_json_atomic(path, value, mode=0o400)


def _exchange_directories(first: Path, second: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ConfigDeployError("atomic rename exchange is unavailable on this base")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(first),
        -100,
        os.fsencode(second),
        2,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise ConfigDeployError("atomic configuration exchange failed: %s" % os.strerror(error))


def _consume_verified_quarantine(path: Path, expected_sha256: str) -> None:
    """Remove one successful upload without following or unlinking through links."""
    directory_descriptor = -1
    file_descriptor = -1
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        directory_info = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(directory_info.st_mode):
            raise ConfigDeployError("configuration quarantine is not a directory")
        file_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_BUNDLE_BYTES
        ):
            raise ConfigDeployError("configuration quarantine file is unsafe")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not block:
                raise ConfigDeployError("configuration quarantine changed while read")
            digest.update(block)
            remaining -= len(block)
        if os.read(file_descriptor, 1):
            raise ConfigDeployError("configuration quarantine changed while read")
        after = os.fstat(file_descriptor)
        current = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not os.path.samestat(before, after)
            or not os.path.samestat(before, current)
            or before.st_mtime_ns != after.st_mtime_ns
            or digest.hexdigest() != expected_sha256
        ):
            raise ConfigDeployError("configuration quarantine no longer matches the applied bundle")
        os.unlink(path.name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise ConfigDeployError("could not safely consume configuration quarantine") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def apply_bundle(
    bundle: Path,
    expected_sha256: str,
    idle_evidence: Path,
    apply: bool,
    confirmation: str | None,
    root: Path = Path("/"),
    controller: CandidateController | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="t300-config-apply-") as directory:
        verified = _extract_verified_bundle(bundle, expected_sha256, Path(directory))
        manifest = verified["manifest"]
        controller = controller or CandidateController(root)
        identity = controller.candidate_identity(strict_owner=apply)
        current_digest = controller.configuration_digest(strict_owner=apply)
        if manifest.get("base_candidate_sha256") != identity["candidate_sha256"]:
            raise ConfigDeployError("bundle targets another candidate image")
        if manifest.get("base_config_sha256") != current_digest:
            raise ConfigDeployError("bundle base does not match the installed configuration")
        controller._require_units_inactive((*HOST_UNITS, *NORMAL_PRINTER_UNITS, SSH_UNIT))
        if controller.marker(MAINTENANCE_MARKER).exists():
            raise ConfigDeployError("maintenance authorization must be absent")
        if controller.marker(TRANSPORT_MARKER).exists():
            raise ConfigDeployError("restricted transfer gate must be absent")
        evidence_digest = _validate_idle_evidence(
            idle_evidence, identity["candidate_sha256"], current_digest
        )
        new_digest = manifest["new_config_sha256"]
        expected_confirmation = "APPLY T300 CONFIG %s FROM %s" % (
            new_digest[:12],
            current_digest[:12],
        )
        result = {
            "action": "apply-config-bundle",
            "apply": apply,
            "bundle_sha256": expected_sha256,
            "base_config_sha256": current_digest,
            "new_config_sha256": new_digest,
            "diff": manifest["diff"],
            "expected_confirmation": expected_confirmation,
            "services_started": [],
        }
        if not apply:
            return result
        controller._require_live_root()
        if confirmation != expected_confirmation:
            raise ConfigDeployError(
                "typed confirmation must be exactly: %s" % expected_confirmation
            )

        config_root = controller.path("/etc/t300")
        sibling = config_root.parent / (".t300-next-%d" % os.getpid())
        if sibling.exists() or sibling.is_symlink():
            raise ConfigDeployError("temporary configuration destination already exists")
        previous = config_root.parent / (".t300-previous-%s" % current_digest[:12])
        if previous.exists() or previous.is_symlink():
            raise ConfigDeployError("atomic previous-tree destination already exists")
        backup_root = controller.path(BACKUP_ROOT)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_root / ("%s-%s" % (stamp, current_digest[:12]))
        if backup.exists() or backup.is_symlink():
            raise ConfigDeployError("configuration backup destination already exists")
        required = sum(
            record.get("size", 0)
            for record in manifest["files"]
            if record.get("type") == "file"
        ) * 3 + 16 * 1024 * 1024
        if shutil.disk_usage(config_root).free < required:
            raise ConfigDeployError("system disk lacks space for config transaction and backup")

        journal = {
            "schema_version": 1,
            "status": "in-progress",
            "phase": "backup",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "candidate_sha256": identity["candidate_sha256"],
            "bundle_sha256": expected_sha256,
            "base_config_sha256": current_digest,
            "new_config_sha256": new_digest,
            "calibration_ready": manifest.get("calibration_ready") is True,
            "commissioning_lock": manifest.get("commissioning_lock") is True,
            "idle_evidence_sha256": evidence_digest,
        }
        journal_path = controller.path(JOURNAL_PATH)
        _write_json_atomic(journal_path, journal)
        rollback_partner: Path | None = None
        retained_previous: Path | None = None
        failed_candidate: Path | None = None
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.mkdir(mode=0o700)
            _validate_commissioning_directory(config_root / "commissioning")
            shutil.copytree(config_root, backup / "tree", symlinks=False)
            _write_json_atomic(
                backup / "backup.json",
                {
                    "schema_version": 1,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "candidate_sha256": identity["candidate_sha256"],
                    "config_sha256": current_digest,
                    "bundle_sha256": expected_sha256,
                    "idle_evidence_sha256": evidence_digest,
                },
            )
            if controller.configuration_digest() != current_digest:
                raise ConfigDeployError("installed configuration changed during backup")

            journal["phase"] = "build-next-tree"
            _write_json_atomic(journal_path, journal)
            shutil.copytree(verified["config"], sibling, symlinks=False)
            commissioning = config_root / "commissioning"
            shutil.copytree(commissioning, sibling / "commissioning", symlinks=False)
            _update_next_markers(sibling, new_digest, expected_sha256)
            _chown_root_tree(sibling)
            sibling.chmod(0o555)
            next_inventory = inventory_configuration_tree(sibling, strict_owner=True)
            if next_inventory["config_sha256"] != new_digest:
                raise ConfigDeployError("prepared next configuration has the wrong digest")

            journal["phase"] = "atomic-exchange"
            _write_json_atomic(journal_path, journal)
            _exchange_directories(config_root, sibling)
            rollback_partner = sibling
            if controller.configuration_digest() != new_digest:
                raise ConfigDeployError("post-exchange configuration verification failed")

            cleanup_warning = None
            try:
                os.rename(sibling, previous)
                rollback_partner = previous
                retained_previous = previous
            except OSError as exc:
                cleanup_warning = "old config remains at %s: %s" % (sibling, exc)
                previous = sibling
                retained_previous = sibling

            journal.update(
                {
                    "phase": "complete",
                    "status": "complete-revalidation-required",
                    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "backup": str(backup),
                    "atomic_previous_tree": str(previous),
                }
            )
            if cleanup_warning is not None:
                journal["cleanup_warning"] = cleanup_warning
            _write_json_atomic(journal_path, journal)
            rollback_partner = None
            result.update(
                {
                    "state": "revalidation-required",
                    "backup": str(backup),
                    "atomic_previous_tree": str(previous),
                    "journal": str(journal_path),
                }
            )
            quarantine = controller.path(
                str(INCOMING_DIRECTORY / INCOMING_BUNDLE)
            )
            if Path(os.path.abspath(bundle)) == quarantine:
                try:
                    _consume_verified_quarantine(quarantine, expected_sha256)
                    result["quarantine_consumed"] = True
                    journal["quarantine_consumed"] = True
                except ConfigDeployError as exc:
                    warning = "applied config is valid but upload quarantine remains: %s" % exc
                    result["quarantine_consumed"] = False
                    result["quarantine_warning"] = warning
                    journal["quarantine_warning"] = warning
                try:
                    _write_json_atomic(journal_path, journal)
                except OSError as exc:
                    result["journal_cleanup_warning"] = str(exc)
            if cleanup_warning is not None:
                result["cleanup_warning"] = cleanup_warning
            return result
        except BaseException as exc:
            if rollback_partner is not None:
                try:
                    _exchange_directories(config_root, rollback_partner)
                    failed_candidate = rollback_partner
                    journal["rollback"] = "complete"
                    journal["failed_candidate_tree"] = str(failed_candidate)
                    rollback_partner = None
                except BaseException as rollback_exc:
                    journal["rollback"] = "FAILED-MANUAL-RECOVERY-REQUIRED"
                    journal["rollback_error_type"] = type(rollback_exc).__name__
            journal.update(
                {
                    "status": "failed",
                    "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                }
            )
            try:
                _write_json_atomic(journal_path, journal)
            except OSError:
                pass
            raise
        finally:
            preserve = {path for path in (retained_previous, failed_candidate) if path is not None}
            if sibling.exists() and sibling not in preserve and rollback_partner != sibling:
                shutil.rmtree(sibling)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--stage", type=Path, required=True)
    prepare.add_argument("--stage-manifest-sha256", required=True)
    prepare.add_argument("--base-inventory", type=Path, required=True)
    prepare.add_argument("--validation-report", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--bundle", type=Path, required=True)
    inspect.add_argument("--bundle-sha256", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--bundle", type=Path, required=True)
    apply_parser.add_argument("--bundle-sha256", required=True)
    apply_parser.add_argument("--idle-evidence", type=Path, required=True)
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.add_argument("--confirm")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_bundle(
                args.stage,
                args.stage_manifest_sha256,
                args.base_inventory,
                args.validation_report,
                args.output,
            )
        elif args.command == "inspect":
            result = inspect_bundle(args.bundle, args.bundle_sha256)
        else:
            result = apply_bundle(
                args.bundle,
                args.bundle_sha256,
                args.idle_evidence,
                args.apply,
                args.confirm,
            )
    except (OSError, ValueError, tarfile.TarError, CommissioningError, ConfigDeployError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

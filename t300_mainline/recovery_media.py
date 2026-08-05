"""Offline inspection and preparation of the pinned T300 recovery boot files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Any

from .lockfile import load_lock


ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
FORBIDDEN_OUTPUT_ROOTS = (Path("/media"), Path("/mnt"), Path("/run/media"))
REQUIRED_BOOT_FILES = {
    "Image": "image_sha256",
    "uInitrd": "uinitrd_sha256",
    "boot.cmd": "boot_cmd_sha256",
    "boot.scr": "boot_scr_sha256",
}
RECOVERY_OVERLAY_MODES = {
    "etc/ssh/sshd_config_t300_recovery": 0o600,
    "etc/systemd/system/ssh.service.d/20-t300-recovery.conf": 0o644,
    "etc/t300-recovery-authorized_keys": 0o400,
    "etc/t300-recovery.json": 0o600,
    "usr/local/sbin/t300-recovery-agent": 0o700,
    "usr/local/sbin/t300-recovery-ssh-gate": 0o700,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoveryMediaError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _regular_file(path: Path, description: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RecoveryMediaError("%s is unavailable: %s" % (description, exc)) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RecoveryMediaError("%s must be one regular, non-symlink file" % description)
    return path


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise RecoveryMediaError("recovery overlay manifest contains an unsafe path")
    return path


def _rooted_regular(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RecoveryMediaError("recovery overlay path is unavailable: %s" % relative) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RecoveryMediaError("recovery overlay path crosses an unsafe directory")
    return _regular_file(root.joinpath(*relative.parts), relative.as_posix())


def _load_overlay_manifest(
    overlay_root: Path, expected_manifest_sha256: str
) -> tuple[Path, dict[str, Any], dict[str, dict[str, Any]]]:
    if SHA256_RE.fullmatch(expected_manifest_sha256) is None:
        raise RecoveryMediaError("recovery overlay manifest SHA-256 is malformed")
    requested = overlay_root.expanduser().absolute()
    if requested.is_symlink():
        raise RecoveryMediaError("recovery overlay root must be one real directory")
    try:
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise RecoveryMediaError("recovery overlay root is unavailable") from exc
    if not root.is_dir():
        raise RecoveryMediaError("recovery overlay root must be one directory")
    manifest_path = _regular_file(root / "stage.manifest.json", "recovery overlay manifest")
    if _sha256_file(manifest_path) != expected_manifest_sha256:
        raise RecoveryMediaError("recovery overlay manifest SHA-256 does not match")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryMediaError("recovery overlay manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("metadata"), dict)
        or manifest["metadata"].get("purpose") != "marked T300 USB recovery overlay"
        or not isinstance(manifest.get("files"), list)
    ):
        raise RecoveryMediaError("recovery overlay manifest header is invalid")
    records: dict[str, dict[str, Any]] = {}
    for record in manifest["files"]:
        if not isinstance(record, dict) or set(record) != {"mode", "path", "sha256", "size"}:
            raise RecoveryMediaError("recovery overlay manifest file record is malformed")
        relative = _safe_relative(record["path"] if isinstance(record.get("path"), str) else "")
        name = relative.as_posix()
        expected_mode = RECOVERY_OVERLAY_MODES.get(name)
        if (
            expected_mode is None
            or name in records
            or record.get("mode") != oct(expected_mode)
            or not isinstance(record.get("size"), int)
            or record["size"] < 1
            or not isinstance(record.get("sha256"), str)
            or SHA256_RE.fullmatch(record["sha256"]) is None
        ):
            raise RecoveryMediaError("recovery overlay manifest file policy is invalid")
        records[name] = record
    if set(records) != set(RECOVERY_OVERLAY_MODES):
        raise RecoveryMediaError("recovery overlay manifest file set is incomplete")
    return root, manifest, records


def _audit_overlay_files(
    root: Path, records: dict[str, dict[str, Any]], require_exact_tree: bool
) -> list[str]:
    failures: list[str] = []
    for name, record in records.items():
        relative = PurePosixPath(name)
        try:
            path = _rooted_regular(root, relative)
            info = path.stat()
            passed = (
                info.st_size == record["size"]
                and stat.S_IMODE(info.st_mode) == RECOVERY_OVERLAY_MODES[name]
                and _sha256_file(path) == record["sha256"]
            )
        except RecoveryMediaError:
            passed = False
        if not passed:
            failures.append(name)
    if require_exact_tree:
        actual: set[str] = set()
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                failures.append("unsafe:%s" % relative)
            elif stat.S_ISREG(info.st_mode) and relative != "stage.manifest.json":
                actual.add(relative)
        if actual != set(records):
            failures.append("file-set")
    return sorted(set(failures))


def audit_recovery_overlay(
    overlay_root: Path,
    expected_manifest_sha256: str,
    installed_root: Path | None = None,
) -> dict[str, Any]:
    root, manifest, records = _load_overlay_manifest(
        overlay_root, expected_manifest_sha256
    )
    source_failures = _audit_overlay_files(root, records, True)
    installed_failures: list[str] | None = None
    installed_path: str | None = None
    if installed_root is not None:
        requested = installed_root.expanduser().absolute()
        if requested.is_symlink():
            raise RecoveryMediaError("installed recovery root must be one real directory")
        try:
            destination = requested.resolve(strict=True)
        except OSError as exc:
            raise RecoveryMediaError("installed recovery root is unavailable") from exc
        if not destination.is_dir():
            raise RecoveryMediaError("installed recovery root must be one directory")
        installed_path = str(destination)
        installed_failures = _audit_overlay_files(destination, records, False)
    ready = not source_failures and (
        installed_failures is None or not installed_failures
    )
    return {
        "schema_version": 1,
        "overlay_root": str(root),
        "manifest_sha256": expected_manifest_sha256,
        "recovery_public_key_fingerprint": manifest["metadata"].get(
            "recovery_public_key_fingerprint"
        ),
        "source_failures": source_failures,
        "installed_root": installed_path,
        "installed_failures": installed_failures,
        "ready": ready,
    }


def parse_armbian_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT_RE.fullmatch(line)
        if match is None or not match.group(2):
            raise RecoveryMediaError(
                "armbianEnv.txt line %d is not one non-empty assignment" % number
            )
        key, value = match.groups()
        if key in values:
            raise RecoveryMediaError("armbianEnv.txt repeats %s" % key)
        if any(character in value for character in "\x00\r\n"):
            raise RecoveryMediaError("armbianEnv.txt contains unsafe control data")
        values[key] = value
    return values


def _policy(lock_path: Path) -> dict[str, Any]:
    return load_lock(lock_path)["recovery_boot"]


def audit_recovery_boot(boot_root: Path, lock_path: Path) -> dict[str, Any]:
    requested = boot_root.expanduser().absolute()
    if requested.is_symlink():
        raise RecoveryMediaError("boot root must be one real directory")
    try:
        boot_root = requested.resolve(strict=True)
    except OSError as exc:
        raise RecoveryMediaError("boot root is unavailable: %s" % exc) from exc
    if not boot_root.is_dir():
        raise RecoveryMediaError("boot root must be one directory")
    policy = _policy(lock_path)
    env_path = _regular_file(boot_root / "armbianEnv.txt", "armbianEnv.txt")
    try:
        env_text = env_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryMediaError("armbianEnv.txt is unreadable: %s" % exc) from exc
    values = parse_armbian_env(env_text)
    checks: dict[str, bool] = {
        "root_uuid": values.get("rootdev") == "UUID=" + policy["root_uuid"],
        "fdtfile": values.get("fdtfile") == policy["fdtfile"],
        "serial_console": values.get("console") in {"serial", "both"},
    }
    hashes: dict[str, str | None] = {}
    for relative, policy_key in REQUIRED_BOOT_FILES.items():
        try:
            path = _regular_file(boot_root / relative, relative)
            digest = _sha256_file(path)
        except RecoveryMediaError:
            digest = None
        hashes[relative] = digest
        checks["hash:%s" % relative] = digest == policy[policy_key]
    dtb_relative = Path("dtb") / policy["fdtfile"]
    try:
        dtb = _regular_file(boot_root / dtb_relative, "Klipad50 device tree")
        dtb_digest = _sha256_file(dtb)
    except RecoveryMediaError:
        dtb_digest = None
    hashes[dtb_relative.as_posix()] = dtb_digest
    checks["hash:%s" % dtb_relative.as_posix()] = (
        dtb_digest == policy["dtb_sha256"]
    )
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "boot_root": str(boot_root),
        "expected_fdtfile": policy["fdtfile"],
        "expected_root_uuid": policy["root_uuid"],
        "serial_baud": policy["serial_baud"],
        "checks": checks,
        "hashes": hashes,
        "failures": failures,
        "ready_for_interactive_usb_boot": not failures,
    }


def render_recovery_env(source: Path, output: Path, lock_path: Path) -> dict[str, Any]:
    source = _regular_file(source.expanduser().absolute(), "source armbianEnv.txt")
    try:
        text = source.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryMediaError("source armbianEnv.txt is unreadable: %s" % exc) from exc
    values = parse_armbian_env(text)
    policy = _policy(lock_path)
    if values.get("rootdev") != "UUID=" + policy["root_uuid"]:
        raise RecoveryMediaError("source armbianEnv.txt names an unexpected root filesystem")
    existing_fdt = values.get("fdtfile")
    if existing_fdt not in (None, policy["fdtfile"]):
        raise RecoveryMediaError("source armbianEnv.txt already names another device tree")

    output = output.expanduser().absolute()
    resolved_parent = output.parent.resolve(strict=True)
    resolved_output = resolved_parent / output.name
    if any(root == resolved_output or root in resolved_output.parents for root in FORBIDDEN_OUTPUT_ROOTS):
        raise RecoveryMediaError("render output must be a laptop-local review file, not mounted media")
    if output.exists() or output.is_symlink():
        raise RecoveryMediaError("render output already exists")
    rendered = text
    if not rendered.endswith("\n"):
        rendered += "\n"
    if existing_fdt is None:
        rendered += "fdtfile=%s\n" % policy["fdtfile"]
    descriptor = -1
    try:
        descriptor = os.open(
            resolved_output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            descriptor = -1
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(resolved_parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "schema_version": 1,
        "source": str(source),
        "output": str(resolved_output),
        "sha256": _sha256_file(resolved_output),
        "fdtfile": policy["fdtfile"],
        "root_uuid": policy["root_uuid"],
        "requires_owner_copy_to_usb": True,
    }


def format_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True)

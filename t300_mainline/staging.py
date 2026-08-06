"""Reproducible, non-deploying root-filesystem staging for T300 mainline."""

from __future__ import annotations

import configparser
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any
import uuid
import zipfile

from .lockfile import load_lock, sha256_file
from .debian_artifacts import DebianArtifactError, load_debian_lock
from .firmware import FirmwareError, load_firmware_inputs, write_source_version
from .python_artifacts import PythonArtifactError, load_artifact_lock
from .private_config import (
    GERGO_MACRO_FILENAME,
    PrivateConfigError,
    load_purchased_gergo,
)
from .private_touchscreen import (
    PrivateTouchscreenError,
    load_touchscreen_runtime,
)
from .transfer import TransferError, validate_public_key


MCU_SERIAL_RE = re.compile(r"^/dev/serial/by-id/[A-Za-z0-9_.:+-]+$")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
FAT_UUID_RE = re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}$")
MAX_ARCHIVE_MEMBERS = 100000
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_WEB_RELEASE_MEMBERS = 20000
MAX_WEB_RELEASE_BYTES = 256 * 1024 * 1024


class StagingError(RuntimeError):
    pass


def component_archive_name(component: dict[str, Any]) -> str:
    return "%s-%s.tar.gz" % (component["name"], component["commit"][:8])


def _verify_base_signature(base: dict[str, Any], cache_dir: Path) -> None:
    image = cache_dir / base["name"]
    signature = cache_dir / (base["name"] + ".asc")
    checksum = cache_dir / (base["name"] + ".sha")
    signing_key = cache_dir / base["signing_key"]["name"]
    expected_fingerprint = base["signing_key"]["fingerprint"]

    try:
        checksum_fields = checksum.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeDecodeError) as exc:
        raise StagingError("could not read the locked Armbian checksum file") from exc
    if checksum_fields != [base["sha256"], base["name"]]:
        raise StagingError("Armbian checksum file does not name the locked image and hash")

    with tempfile.TemporaryDirectory(prefix="t300-gpg-") as directory:
        homedir = Path(directory)
        homedir.chmod(0o700)
        common = ["gpg", "--batch", "--no-options", "--homedir", str(homedir)]
        try:
            key_result = subprocess.run(
                common
                + [
                    "--with-colons",
                    "--import-options",
                    "show-only",
                    "--import",
                    str(signing_key),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StagingError("could not inspect the locked Armbian signing key") from exc
        fingerprints = {
            fields[9]
            for line in key_result.stdout.splitlines()
            if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9
        }
        if key_result.returncode != 0 or expected_fingerprint not in fingerprints:
            raise StagingError("Armbian signing key fingerprint does not match the lock")

        try:
            subprocess.run(
                common + ["--import", str(signing_key)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            verify_result = subprocess.run(
                common
                + [
                    "--status-fd",
                    "1",
                    "--verify",
                    str(signature),
                    str(image),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StagingError("could not verify the locked Armbian signature") from exc
        valid_fingerprints = {
            fields[2]
            for line in verify_result.stdout.splitlines()
            if (fields := line.split())[:2] == ["[GNUPG:]", "VALIDSIG"]
            and len(fields) > 2
        }
        if verify_result.returncode != 0 or expected_fingerprint not in valid_fingerprints:
            raise StagingError("Armbian detached signature is not valid for the locked key")


def verify_cache(lock_path: Path, cache_dir: Path, include_base: bool = True) -> list[Path]:
    lock = load_lock(lock_path)
    cache_dir = cache_dir.expanduser().resolve(strict=True)
    records: list[tuple[str, str, int | None]] = []
    if include_base:
        base = lock["base_image"]
        records.append((base["name"], base["sha256"], None))
        records.append((base["name"] + ".asc", base["signature_sha256"], None))
        records.append((base["name"] + ".sha", base["checksum_sha256"], None))
        records.append(
            (base["signing_key"]["name"], base["signing_key"]["sha256"], None)
        )
    for component in lock["components"]:
        records.append(
            (component_archive_name(component), component["archive_sha256"], None)
        )
        asset = component.get("release_asset")
        if asset is not None:
            records.append((asset["name"], asset["sha256"], asset["size"]))
    checked: list[Path] = []
    for filename, expected, expected_size in records:
        path = cache_dir / filename
        if not path.is_file() or path.is_symlink():
            raise StagingError("locked artifact is missing or unsafe: %s" % (path,))
        if expected_size is not None and path.stat().st_size != expected_size:
            raise StagingError(
                "artifact size mismatch for %s: expected %d, got %d"
                % (filename, expected_size, path.stat().st_size)
            )
        actual = sha256_file(path)
        if actual != expected:
            raise StagingError(
                "artifact hash mismatch for %s: expected %s, got %s"
                % (filename, expected, actual)
            )
        checked.append(path)
    if include_base:
        _verify_base_signature(lock["base_image"], cache_dir)
    return checked


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise StagingError("archive contains an unsafe path: %s" % (name,))
    return path


def extract_source(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise StagingError("archive member count is empty or excessive: %s" % (archive,))
        total = 0
        roots: set[str] = set()
        parsed: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in members:
            path = _safe_member_path(member.name)
            roots.add(path.parts[0])
            if member.issym() or member.islnk():
                base = posixpath.dirname(member.name) if member.issym() else ""
                resolved = posixpath.normpath(posixpath.join(base, member.linkname))
                linked = _safe_member_path(resolved)
                if linked.parts[0] != path.parts[0]:
                    raise StagingError("archive link escapes its source root: %s" % (member.name,))
            elif member.isdev() or member.isfifo():
                raise StagingError("archive contains a special file: %s" % (member.name,))
            elif not (member.isdir() or member.isfile()):
                raise StagingError("archive contains an unsupported member: %s" % (member.name,))
            total += max(0, member.size)
            if total > MAX_ARCHIVE_BYTES:
                raise StagingError("archive expands beyond the 4 GiB staging limit")
            parsed.append((member, path))
        if len(roots) != 1:
            raise StagingError("source archive must contain one top-level directory")
        for member, path in parsed:
            # Repository convenience links are not needed for builds. Skipping
            # them avoids creating any symlink in the staged root filesystem.
            if member.issym() or member.islnk():
                continue
            relative_parts = path.parts[1:]
            if not relative_parts:
                continue
            target = destination.joinpath(*relative_parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise StagingError("could not read archive member %s" % (member.name,))
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(target, member.mode & 0o755 or 0o644)


def extract_web_release(
    archive: Path, destination: Path, expected_version: str
) -> None:
    """Extract one locked static web release without trusting ZIP metadata."""
    if destination.exists() or destination.is_symlink():
        raise StagingError("web release destination already exists: %s" % destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            members = bundle.infolist()
            if not members or len(members) > MAX_WEB_RELEASE_MEMBERS:
                raise StagingError("web release member count is empty or excessive")
            seen: set[PurePosixPath] = set()
            declared_total = 0
            parsed: list[tuple[zipfile.ZipInfo, PurePosixPath, bool]] = []
            for member in members:
                if (
                    not member.filename
                    or "\x00" in member.filename
                    or "\\" in member.filename
                ):
                    raise StagingError("web release contains an unsafe filename")
                path = _safe_member_path(member.filename)
                if path in seen:
                    raise StagingError(
                        "web release contains a duplicate path: %s" % member.filename
                    )
                seen.add(path)
                if member.flag_bits & 0x1:
                    raise StagingError("encrypted web release members are unsupported")
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                is_directory = member.is_dir()
                allowed_type = stat.S_IFDIR if is_directory else stat.S_IFREG
                if file_type not in (0, allowed_type):
                    raise StagingError(
                        "web release contains a link or special file: %s"
                        % member.filename
                    )
                if member.file_size < 0:
                    raise StagingError("web release contains a negative member size")
                declared_total += member.file_size
                if declared_total > MAX_WEB_RELEASE_BYTES:
                    raise StagingError("web release expands beyond the 256 MiB limit")
                parsed.append((member, path, is_directory))

            extracted_total = 0
            for member, path, is_directory in parsed:
                target = destination.joinpath(*path.parts)
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, 0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(member, "r") as source, target.open("xb") as output:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        written += len(block)
                        extracted_total += len(block)
                        if (
                            written > member.file_size
                            or extracted_total > MAX_WEB_RELEASE_BYTES
                        ):
                            raise StagingError(
                                "web release expanded beyond its declared bounds"
                            )
                        output.write(block)
                if written != member.file_size:
                    raise StagingError(
                        "web release member size changed while extracting"
                    )
                os.chmod(target, 0o644)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("could not safely extract the web release: %s" % exc) from exc
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    try:
        version = (destination / ".version").read_text(encoding="ascii").strip()
        index = (destination / "index.html").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("compiled Mainsail release metadata is unreadable") from exc
    if version != expected_version:
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("compiled Mainsail release version does not match the lock")
    references = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index)
    if not references or not any(value.endswith(".js") for value in references):
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("Mainsail index does not reference a compiled JavaScript asset")
    if not any(value.endswith(".css") for value in references):
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("Mainsail index does not reference a compiled stylesheet")
    if "/src/" in index or re.search(r'\.tsx?(?:[?"\'])', index):
        shutil.rmtree(destination, ignore_errors=True)
        raise StagingError("Mainsail index still references development source")
    for reference in references:
        relative = _safe_member_path(reference.lstrip("/"))
        target = destination.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file():
            shutil.rmtree(destination, ignore_errors=True)
            raise StagingError("Mainsail index references a missing static asset")


def apply_locked_patches(
    lock: dict[str, Any], repo_root: Path, sources: dict[str, Path], stage_root: Path
) -> None:
    patch_root = stage_root / "opt/t300/patches"
    patch_root.mkdir(parents=True, exist_ok=True)
    for item in lock["compatibility_patches"]:
        source_path = repo_root / item["path"]
        if source_path.is_symlink() or not source_path.is_file():
            raise StagingError("locked patch is not a regular file: %s" % (source_path,))
        source = source_path.resolve(strict=True)
        try:
            source.relative_to(repo_root)
        except ValueError as exc:
            raise StagingError("locked patch escapes the repository") from exc
        if sha256_file(source) != item["sha256"]:
            raise StagingError("locked patch hash changed: %s" % (item["name"],))

        component_root = sources[item["component"]]
        target = component_root / item["input_path"]
        if target.is_symlink() or not target.is_file():
            raise StagingError("locked patch target is missing or unsafe")
        if sha256_file(target) != item["input_sha256"]:
            raise StagingError("locked patch input does not match the expected base")
        try:
            result = subprocess.run(
                [
                    "patch",
                    "--batch",
                    "--forward",
                    "--fuzz=0",
                    "-p1",
                    "--input",
                    str(source),
                ],
                cwd=component_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StagingError("could not apply locked compatibility patch") from exc
        patch_output = result.stdout + result.stderr
        if result.returncode != 0 or "offset" in patch_output or "fuzz" in patch_output:
            raise StagingError(
                "locked compatibility patch did not apply exactly: %s" % (item["name"],)
            )
        if sha256_file(target) != item["output_sha256"]:
            raise StagingError("locked patch output hash does not match")
        shutil.copy2(source, patch_root / (item["name"] + ".patch"))


CALIBRATION_ALLOWLIST = {
    "extruder": {
        "rotation_distance",
        "control",
        "pid_kp",
        "pid_ki",
        "pid_kd",
    },
    "heater_bed": {"control", "pid_kp", "pid_ki", "pid_kd"},
    "probe": {"z_offset"},
    "input_shaper": {
        "shaper_type_x",
        "shaper_freq_x",
        "shaper_type_y",
        "shaper_freq_y",
        "damping_ratio_x",
        "damping_ratio_y",
    },
}

CALIBRATION_REQUIRED = {
    "extruder": {
        "rotation_distance",
        "control",
        "pid_kp",
        "pid_ki",
        "pid_kd",
    },
    "heater_bed": {"control", "pid_kp", "pid_ki", "pid_kd"},
    "probe": {"z_offset"},
    "input_shaper": {
        "shaper_type_x",
        "shaper_freq_x",
        "shaper_type_y",
        "shaper_freq_y",
    },
}

T300_SHAPER_TYPES = {"zv", "mzv", "zvd", "ei", "2hump_ei", "3hump_ei"}
TRUSTED_IPV4_RANGES = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _calibration_float(
    parser: configparser.RawConfigParser,
    section: str,
    option: str,
    minimum: float,
    maximum: float,
    *,
    minimum_exclusive: bool = False,
) -> float:
    try:
        raw = parser.get(section, option)
        value = float(raw)
    except (configparser.Error, TypeError, ValueError) as exc:
        raise StagingError(
            "calibration value %s.%s must be one finite number" % (section, option)
        ) from exc
    if not math.isfinite(value):
        raise StagingError(
            "calibration value %s.%s must be one finite number" % (section, option)
        )
    too_low = value <= minimum if minimum_exclusive else value < minimum
    if too_low or value > maximum:
        lower = "> %s" % minimum if minimum_exclusive else ">= %s" % minimum
        raise StagingError(
            "calibration value %s.%s must be %s and <= %s"
            % (section, option, lower, maximum)
        )
    return value


def validate_trusted_laptop_network(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise StagingError("trusted laptop CIDR is malformed") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise StagingError("trusted laptop network must be IPv4")
    if network.prefixlen < 24:
        raise StagingError("trusted laptop network must be a private /24 or narrower")
    if not any(network.subnet_of(parent) for parent in TRUSTED_IPV4_RANGES):
        raise StagingError("trusted laptop network must be inside an RFC1918 range")
    return network


def validate_calibration(path: Path) -> bytes:
    try:
        content = path.read_bytes()
        text = content.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise StagingError("calibration file must be readable ASCII: %s" % (exc,)) from exc
    if b"SAVE_CONFIG" in content or b"REPLACE_" in content:
        raise StagingError("calibration file contains generated or placeholder material")
    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise StagingError("calibration file is malformed: %s" % (exc,)) from exc
    if set(parser.sections()) != set(CALIBRATION_ALLOWLIST):
        raise StagingError("calibration file must contain exactly the four approved sections")
    if parser.defaults():
        raise StagingError("calibration file must not contain DEFAULT options")
    for section, allowed in CALIBRATION_ALLOWLIST.items():
        options = set(parser.options(section))
        unknown = options - allowed
        if unknown:
            raise StagingError(
                "calibration section %s has forbidden options: %s"
                % (section, ", ".join(sorted(unknown)))
            )
        missing = CALIBRATION_REQUIRED[section] - options
        if missing:
            raise StagingError(
                "calibration section %s is missing required options: %s"
                % (section, ", ".join(sorted(missing)))
            )
    if parser.get("extruder", "control", fallback="").lower() != "pid":
        raise StagingError("release calibration must use PID hotend control")
    if parser.get("heater_bed", "control", fallback="").lower() != "pid":
        raise StagingError("release calibration must use PID bed control")

    # These broad bounds surround the verified stock T300 values and normal
    # calibration outputs. They catch unit mistakes and dangerous typos; they
    # do not select or tune a calibration result.
    _calibration_float(parser, "extruder", "rotation_distance", 2.5, 5.0)
    for section in ("extruder", "heater_bed"):
        _calibration_float(parser, section, "pid_kp", 0.0, 10000.0, minimum_exclusive=True)
        _calibration_float(parser, section, "pid_ki", 0.0, 10000.0, minimum_exclusive=True)
        _calibration_float(parser, section, "pid_kd", 0.0, 10000.0)
    _calibration_float(parser, "probe", "z_offset", 0.1, 5.0)
    for axis in ("x", "y"):
        shaper_type = parser.get("input_shaper", "shaper_type_" + axis).strip()
        if shaper_type not in T300_SHAPER_TYPES:
            raise StagingError(
                "calibration input shaper type for %s is unsupported by pinned Klipper"
                % axis.upper()
            )
        _calibration_float(
            parser, "input_shaper", "shaper_freq_" + axis, 0.0, 200.0,
            minimum_exclusive=True,
        )
        damping = "damping_ratio_" + axis
        if parser.has_option("input_shaper", damping):
            _calibration_float(parser, "input_shaper", damping, 0.0, 1.0)
    return content


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def stage_python_artifacts(
    repo_root: Path,
    stack_lock: dict[str, Any],
    python_cache_dir: Path,
    stage_root: Path,
) -> None:
    reference = stack_lock["python_artifacts"]
    unresolved = repo_root / reference["path"]
    if unresolved.is_symlink() or not unresolved.is_file():
        raise StagingError("Python artifact lock is missing or unsafe")
    lock_path = unresolved.resolve(strict=True)
    try:
        lock_path.relative_to(repo_root)
    except ValueError as exc:
        raise StagingError("Python artifact lock escapes the repository") from exc
    if sha256_file(lock_path) != reference["sha256"]:
        raise StagingError("Python artifact lock hash changed")
    try:
        artifact_lock = load_artifact_lock(lock_path)
    except PythonArtifactError as exc:
        raise StagingError("Python artifact lock is invalid: %s" % (exc,)) from exc
    lock_target = artifact_lock["target"]
    for key, value in reference["target"].items():
        if lock_target.get(key) != value:
            raise StagingError("Python artifact lock target does not match stack.lock.json")

    cache_root = python_cache_dir.expanduser().resolve(strict=True)
    destination_root = stage_root / "opt/t300/python-wheelhouse"
    destination_root.mkdir(parents=True)
    for environment in artifact_lock["environments"]:
        name = environment["name"]
        source_dir = cache_root / name
        if source_dir.is_symlink() or not source_dir.is_dir():
            raise StagingError("Python wheelhouse environment is missing or unsafe: %s" % name)
        expected = {item["filename"]: item for item in environment["artifacts"]}
        actual = {
            item.name: item
            for item in source_dir.iterdir()
            if item.is_file() and not item.is_symlink()
        }
        unsafe = [item.name for item in source_dir.iterdir() if item.is_symlink()]
        if unsafe or set(actual) != set(expected):
            raise StagingError("Python wheelhouse does not exactly match its lock: %s" % name)
        destination = destination_root / name
        destination.mkdir()
        for filename in sorted(expected):
            if sha256_file(actual[filename]) != expected[filename]["sha256"]:
                raise StagingError("Python artifact hash mismatch: %s" % filename)
            shutil.copy2(actual[filename], destination / filename)

    for environment in artifact_lock["environments"]:
        logical_path = environment.get("requirements_path")
        if logical_path is None:
            continue
        staged_path = stage_root.joinpath(*PurePosixPath(logical_path).parts[1:])
        if staged_path.is_symlink() or not staged_path.is_file():
            raise StagingError("locked runtime requirement file is missing")
        if sha256_file(staged_path) != environment["requirements_sha256"]:
            raise StagingError("staged runtime requirement file changed: %s" % environment["name"])
    shutil.copy2(lock_path, stage_root / "opt/t300/python-artifacts.lock.json")


def stage_debian_artifacts(
    repo_root: Path,
    stack_lock: dict[str, Any],
    debian_cache_dir: Path,
    stage_root: Path,
) -> None:
    reference = stack_lock["debian_artifacts"]
    lock_unresolved = repo_root / reference["path"]
    policy_unresolved = repo_root / reference["root_policy_path"]
    for path, label in ((lock_unresolved, "lock"), (policy_unresolved, "root policy")):
        if path.is_symlink() or not path.is_file():
            raise StagingError("Debian artifact %s is missing or unsafe" % label)
        try:
            path.resolve(strict=True).relative_to(repo_root)
        except ValueError as exc:
            raise StagingError("Debian artifact %s escapes the repository" % label) from exc
    lock_path = lock_unresolved.resolve(strict=True)
    policy_path = policy_unresolved.resolve(strict=True)
    if sha256_file(lock_path) != reference["sha256"]:
        raise StagingError("Debian artifact lock hash changed")
    if sha256_file(policy_path) != reference["root_policy_sha256"]:
        raise StagingError("Debian root package policy hash changed")
    try:
        artifact_lock = load_debian_lock(lock_path)
    except DebianArtifactError as exc:
        raise StagingError("Debian artifact lock is invalid: %s" % exc) from exc
    if artifact_lock["target"] != reference["target"]:
        raise StagingError("Debian artifact target does not match stack.lock.json")
    if artifact_lock["base_image_sha256"] != stack_lock["base_image"]["sha256"]:
        raise StagingError("Debian artifact lock was solved against another base image")
    if artifact_lock["root_policy_sha256"] != reference["root_policy_sha256"]:
        raise StagingError("Debian artifact lock references another root policy")
    if len(artifact_lock["artifacts"]) != reference["artifact_count"]:
        raise StagingError("Debian artifact count differs from stack.lock.json")

    cache_root = debian_cache_dir.expanduser().resolve(strict=True)
    expected = {item["filename"]: item for item in artifact_lock["artifacts"]}
    actual = {
        path.name: path
        for path in cache_root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if any(path.is_symlink() for path in cache_root.iterdir()) or set(actual) != set(expected):
        raise StagingError("Debian package cache does not exactly match its lock")
    destination = stage_root / "opt/t300/debian-packages"
    destination.mkdir(parents=True)
    for filename in sorted(expected):
        source = actual[filename]
        item = expected[filename]
        if source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
            raise StagingError("Debian package bytes do not match the lock: %s" % filename)
        shutil.copy2(source, destination / filename)
    shutil.copy2(lock_path, stage_root / "opt/t300/debian-artifacts.lock.json")
    shutil.copy2(policy_path, stage_root / "opt/t300/debian-root-packages.json")


def _render(source: Path, destination: Path, replacements: dict[str, str]) -> None:
    text = source.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        text = text.replace("@%s@" % (marker,), value)
    leftovers = re.findall(r"@[A-Z0-9_]+@", text)
    if leftovers:
        raise StagingError("unresolved template markers: %s" % (", ".join(leftovers),))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def _manifest(root: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "stage.manifest.json":
            continue
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "mode": oct(path.stat().st_mode & 0o777),
                "sha256": sha256_file(path),
            }
        )
    return {"schema_version": 1, "metadata": metadata, "files": files}


def stage_rootfs(
    repo_root: Path,
    lock_path: Path,
    cache_dir: Path,
    output: Path,
    mcu_serial: str,
    trusted_laptop_cidr: str,
    printer_hostname: str = "t300",
    calibration: Path | None = None,
    python_cache_dir: Path | None = None,
    debian_cache_dir: Path | None = None,
    data_usb_uuid: str = "C66C-ADD5",
    gergo_source: Path | None = None,
    deploy_public_key: Path | None = None,
    touchscreen_runtime: Path | None = None,
) -> Path:
    repo_root = repo_root.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    cache_dir = cache_dir.resolve(strict=True)
    output = output.expanduser().resolve()
    if output.exists():
        raise StagingError("stage output already exists: %s" % (output,))
    if not MCU_SERIAL_RE.fullmatch(mcu_serial):
        raise StagingError("MCU serial must be one stable /dev/serial/by-id path")
    if not HOSTNAME_RE.fullmatch(printer_hostname):
        raise StagingError("printer hostname is malformed")
    data_usb_uuid = data_usb_uuid.upper()
    if not FAT_UUID_RE.fullmatch(data_usb_uuid):
        raise StagingError("timelapse USB UUID must use the FAT XXXX-XXXX form")
    network = validate_trusted_laptop_network(trusted_laptop_cidr)
    if calibration is not None and gergo_source is None:
        raise StagingError("a calibrated release stage requires the purchased GerGo package")
    gergo_macro = None
    if gergo_source is not None:
        try:
            gergo_macro = load_purchased_gergo(gergo_source)
        except PrivateConfigError as exc:
            raise StagingError(str(exc)) from exc
    touchscreen_files = None
    if touchscreen_runtime is not None:
        try:
            touchscreen_files = load_touchscreen_runtime(touchscreen_runtime)
        except PrivateTouchscreenError as exc:
            raise StagingError(str(exc)) from exc
    deploy_key = None
    if deploy_public_key is not None:
        try:
            deploy_key = validate_public_key(deploy_public_key)
        except TransferError as exc:
            raise StagingError(str(exc)) from exc

    lock = load_lock(lock_path)
    if python_cache_dir is None:
        python_cache_dir = repo_root / ".cache/mainline/python-wheelhouse"
    if debian_cache_dir is None:
        debian_cache_dir = repo_root / ".cache/mainline/debian-packages"
    verify_cache(lock_path, cache_dir, include_base=False)
    temporary = output.with_name(".%s.partial-%s" % (output.name, uuid.uuid4().hex))
    temporary.mkdir(parents=True)
    try:
        source_root = temporary / "opt/t300/src"
        source_root.mkdir(parents=True)
        sources: dict[str, Path] = {}
        for component in lock["components"]:
            name = component["name"]
            destination = source_root / name
            extract_source(cache_dir / component_archive_name(component), destination)
            sources[name] = destination
        mainsail_component = next(
            component for component in lock["components"]
            if component["name"] == "mainsail"
        )
        mainsail_asset = mainsail_component["release_asset"]
        extract_web_release(
            cache_dir / mainsail_asset["name"],
            temporary / "opt/t300/www/mainsail",
            mainsail_asset["version"],
        )
        apply_locked_patches(lock, repo_root, sources, temporary)
        try:
            firmware_info = load_firmware_inputs(repo_root / "mainline/firmware", lock)
            write_source_version(sources["klipper"], firmware_info["version"])
        except FirmwareError as exc:
            raise StagingError(str(exc)) from exc
        _copy_tree(
            repo_root / "mainline/firmware",
            temporary / "opt/t300/firmware-inputs",
        )
        stage_python_artifacts(repo_root, lock, python_cache_dir, temporary)
        stage_debian_artifacts(repo_root, lock, debian_cache_dir, temporary)

        config_root = temporary / "etc/t300/klipper"
        _copy_tree(repo_root / "mainline/config/production", config_root)
        _copy_tree(
            repo_root / "mainline/config/maintenance",
            temporary / "etc/t300/maintenance",
        )
        local = config_root / "local"
        local.mkdir()
        _render(
            repo_root / "mainline/config/templates/identity.cfg.in",
            local / "identity.cfg",
            {"MCU_SERIAL": mcu_serial},
        )
        calibration_ready = calibration is not None
        if not calibration_ready:
            shutil.copy2(
                repo_root / "mainline/config/templates/calibration-bootstrap.cfg",
                local / "calibration.cfg",
            )
        else:
            (local / "calibration.cfg").write_bytes(validate_calibration(calibration))
            safety_path = config_root / "safety.cfg"
            safety_text = safety_path.read_text(encoding="utf-8")
            marker = "commissioning_lock: True"
            if safety_text.count(marker) != 1:
                raise StagingError("production safety config lost its commissioning lock")
            safety_path.write_text(
                safety_text.replace(marker, "commissioning_lock: False"),
                encoding="utf-8",
            )

        private = config_root / "private"
        private.mkdir(mode=0o700)
        if gergo_macro is not None:
            private_macro = private / GERGO_MACRO_FILENAME
            private_macro.write_bytes(gergo_macro)
            private_macro.chmod(0o600)

        if touchscreen_files is not None:
            touchscreen_root = temporary / "opt/t300/private/touchscreen"
            for name, value in touchscreen_files.items():
                destination = touchscreen_root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(value)
                destination.chmod(0o500 if name == "zhongchuang_klipper" else 0o400)

        vendor = config_root / "vendor"
        (vendor / "mainsail").mkdir(parents=True)
        mainsail_source = sources["mainsail-config"] / "client.cfg"
        mainsail_text = mainsail_source.read_text(encoding="utf-8")
        old_virtual_sd = "path: ~/printer_data/gcodes"
        if mainsail_text.count(old_virtual_sd) != 1:
            raise StagingError("pinned Mainsail virtual-SD path changed unexpectedly")
        (vendor / "mainsail/client.cfg").write_text(
            mainsail_text.replace(
                old_virtual_sd,
                "path: /var/lib/t300/moonraker-data/gcodes",
            ),
            encoding="utf-8",
        )
        (vendor / "kamp").mkdir()
        for filename in ("KAMP_Settings.cfg", "Line_Purge.cfg", "Smart_Park.cfg"):
            shutil.copy2(sources["kamp"] / "Configuration" / filename, vendor / "kamp" / filename)
        shutil.copy2(
            sources["moonraker-timelapse"] / "component/timelapse.py",
            sources["moonraker"] / "moonraker/components/timelapse_upstream.py",
        )
        shutil.copy2(
            repo_root / "mainline/moonraker/t300_timelapse.py",
            sources["moonraker"] / "moonraker/components/timelapse.py",
        )
        shutil.copy2(
            repo_root / "mainline/klippy/extras/t300_safety.py",
            sources["klipper"] / "klippy/extras/t300_safety.py",
        )

        shutil.copy2(repo_root / "mainline/policy/gcode-policy.json", temporary / "etc/t300/gcode-policy.json")
        touchscreen_config = temporary / "etc/t300/touchscreen"
        touchscreen_config.mkdir(parents=True)
        shutil.copy2(
            repo_root / "mainline/touchscreen/button-contract.json",
            touchscreen_config / "button-contract.json",
        )
        _copy_tree(repo_root / "mainline/systemd", temporary / "etc/systemd/system")
        for unit in ("klipper.service", "klipper-maintenance.service"):
            _render(
                repo_root / "mainline/config/templates/klipper-device.conf.in",
                temporary / "etc/systemd/system" / (unit + ".d") / "10-mcu-device.conf",
                {"MCU_SERIAL": mcu_serial},
            )
        _render(
            repo_root / "mainline/config/templates/t300-data.mount.in",
            temporary / "etc/systemd/system" / r"mnt-t300\x2ddata.mount",
            {"DATA_USB_UUID": data_usb_uuid},
        )
        _render(
            repo_root / "mainline/config/host/moonraker.conf.in",
            temporary / "etc/t300/moonraker/moonraker.conf",
            {
                "PRINTER_HOSTNAME": printer_hostname,
            },
        )
        _render(
            repo_root / "mainline/config/host/nginx.conf.in",
            temporary / "etc/t300/nginx/nginx.conf",
            {"TRUSTED_LAPTOP_CIDR": str(network)},
        )
        for name in ("crowsnest.conf",):
            source = repo_root / "mainline/config/host" / name
            if name.startswith("crowsnest"):
                destination_dir = "crowsnest"
            destination = temporary / "etc/t300" / destination_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        mainsail_defaults = temporary / "etc/t300/mainsail/default.json"
        mainsail_defaults.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            repo_root / "mainline/config/host/mainsail-default.json",
            mainsail_defaults,
        )
        journald = temporary / "etc/systemd/journald.conf.d/60-t300.conf"
        journald.parent.mkdir(parents=True)
        shutil.copy2(repo_root / "mainline/config/host/journald-t300.conf", journald)
        udev_rule = temporary / "etc/udev/rules.d/60-t300-host-mcu.rules"
        udev_rule.parent.mkdir(parents=True)
        shutil.copy2(repo_root / "mainline/udev/60-t300-host-mcu.rules", udev_rule)

        ssh_config = temporary / "etc/ssh/sshd_config.d/60-t300.conf"
        _render(
            repo_root / "mainline/config/templates/sshd-t300.conf.in",
            ssh_config,
            {"TRUSTED_LAPTOP_CIDR": str(network)},
        )
        authorized_key = temporary / "etc/t300/deploy_authorized_keys"
        if deploy_key is None:
            authorized_key.write_text(
                "# No deployment key was staged; restricted transport cannot be armed.\n",
                encoding="ascii",
            )
        else:
            authorized_key.write_text(
                'restrict,no-user-rc,command="/opt/t300/venvs/control/bin/python '
                '/opt/t300/control/bin/t300-transfer-receive.py" %s\n'
                % deploy_key["key"],
                encoding="ascii",
            )
        authorized_key.chmod(0o400)

        control_root = temporary / "opt/t300/control/t300_mainline"
        _copy_tree(repo_root / "t300_mainline", control_root)
        control_bin = temporary / "opt/t300/control/bin"
        control_bin.mkdir(parents=True)
        shutil.copy2(repo_root / "bin/t300-provision.py", control_bin / "t300-provision.py")
        shutil.copy2(repo_root / "bin/t300-candidate.py", control_bin / "t300-candidate.py")
        shutil.copy2(repo_root / "bin/t300-config-deploy.py", control_bin / "t300-config-deploy.py")
        shutil.copy2(repo_root / "bin/t300-transfer-receive.py", control_bin / "t300-transfer-receive.py")
        shutil.copy2(lock_path, temporary / "opt/t300/stack.lock.json")
        metadata = {
            "profile": lock["profile"],
            "release_ready": False,
            "calibration_ready": calibration_ready,
            "calibration": "measured" if calibration_ready else "commissioning-watermark",
            "stage_kind": "source-and-configuration-overlay",
            "private_gergo_present": gergo_macro is not None,
            "private_touchscreen_present": touchscreen_files is not None,
            "deploy_transport_present": deploy_key is not None,
            "deploy_public_key_fingerprint": (
                deploy_key["fingerprint"] if deploy_key is not None else None
            ),
            "target": lock["target"],
            "data_usb_uuid": data_usb_uuid,
            "firmware_preparation": "build-and-verify-only",
            "firmware_provenance_sha256": firmware_info["provenance_sha256"],
        }
        manifest = _manifest(temporary, metadata)
        (temporary / "stage.manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output / "stage.manifest.json"


def stage_recovery_overlay(
    repo_root: Path, output: Path, recovery_public_key: Path
) -> Path:
    repo_root = repo_root.resolve(strict=True)
    output = output.expanduser().resolve()
    if output.exists():
        raise StagingError("recovery overlay output already exists")
    try:
        recovery_key = validate_public_key(recovery_public_key)
    except TransferError as exc:
        raise StagingError(str(exc)) from exc
    temporary = output.with_name(".%s.partial-%s" % (output.name, uuid.uuid4().hex))
    temporary.mkdir(parents=True)
    try:
        agent = temporary / "usr/local/sbin/t300-recovery-agent"
        agent.parent.mkdir(parents=True)
        shutil.copy2(repo_root / "bin/t300-recovery-agent.py", agent)
        os.chmod(agent, 0o700)
        gate = temporary / "usr/local/sbin/t300-recovery-ssh-gate"
        shutil.copy2(repo_root / "bin/t300-recovery-ssh-gate.py", gate)
        os.chmod(gate, 0o700)
        _render(
            repo_root / "mainline/recovery/t300-recovery.json.in",
            temporary / "etc/t300-recovery.json",
            {"RECOVERY_ID": str(uuid.uuid4())},
        )
        os.chmod(temporary / "etc/t300-recovery.json", 0o600)
        ssh_config = temporary / "etc/ssh/sshd_config_t300_recovery"
        ssh_config.parent.mkdir(parents=True)
        shutil.copy2(
            repo_root / "mainline/recovery/sshd-t300-recovery.conf", ssh_config
        )
        os.chmod(ssh_config, 0o600)
        ssh_override = temporary / "etc/systemd/system/ssh.service.d/20-t300-recovery.conf"
        ssh_override.parent.mkdir(parents=True)
        shutil.copy2(
            repo_root / "mainline/recovery/ssh.service.override.conf",
            ssh_override,
        )
        os.chmod(ssh_override, 0o644)
        authorized_key = temporary / "etc/t300-recovery-authorized_keys"
        authorized_key.write_text(
            'restrict,no-user-rc,command="/usr/local/sbin/t300-recovery-ssh-gate" %s\n'
            % recovery_key["key"],
            encoding="ascii",
        )
        os.chmod(authorized_key, 0o400)
        manifest = _manifest(
            temporary,
            {
                "purpose": "marked T300 USB recovery overlay",
                "network_access": "forced-command-only",
                "ssh_configuration": "standalone-explicit-systemd-config",
                "recovery_public_key_fingerprint": recovery_key["fingerprint"],
            },
        )
        target = temporary / "stage.manifest.json"
        target.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output / "stage.manifest.json"

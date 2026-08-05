"""Lock Debian packages selected by the signed image's own APT solver."""

from __future__ import annotations

from email.parser import Parser
from email.policy import compat32
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any
from urllib.parse import unquote

from .lockfile import sha256_file


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
OFFICIAL_BASES = {
    "deb.debian.org": "https://deb.debian.org/debian/",
    "security.debian.org": "https://security.debian.org/debian-security/",
}


class DebianArtifactError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DebianArtifactError("could not read JSON: %s" % path) from exc
    if not isinstance(value, dict):
        raise DebianArtifactError("JSON root must be an object: %s" % path)
    return value


def parse_control_file(path: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DebianArtifactError("could not read Debian control data: %s" % path) from exc
    records: list[dict[str, str]] = []
    for paragraph in re.split(r"\n[ \t]*\n", text.strip()):
        message = Parser(policy=compat32).parsestr(paragraph + "\n\n", headersonly=True)
        record = {key: str(value).strip() for key, value in message.items()}
        if record:
            records.append(record)
    return records


def load_roots(path: Path) -> tuple[list[dict[str, str]], bool]:
    value = _read_json(path)
    roots = value.get("roots")
    if value.get("schema_version") != 1 or not isinstance(roots, list) or not roots:
        raise DebianArtifactError("Debian root package policy is malformed")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in roots:
        if not isinstance(item, dict):
            raise DebianArtifactError("Debian root package entry must be an object")
        name = item.get("name")
        purpose = item.get("purpose")
        if (
            not isinstance(name, str)
            or not PACKAGE_RE.fullmatch(name)
            or name in seen
            or not isinstance(purpose, str)
            or not purpose.strip()
        ):
            raise DebianArtifactError("Debian root package entry is incomplete or duplicated")
        seen.add(name)
        result.append({"name": name, "purpose": purpose.strip()})
    install_recommends = value.get("install_recommends")
    if not isinstance(install_recommends, bool):
        raise DebianArtifactError("install_recommends must be boolean")
    return result, install_recommends


def apt_plan(apt_command: Path, roots: list[dict[str, str]], recommends: bool) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="t300-apt-empty-") as directory:
        empty_cache = Path(directory)
        (empty_cache / "partial").mkdir()
        command = [
            str(apt_command),
            "--print-uris",
            "--yes",
            "-o",
            "Dir::Cache::archives=%s" % empty_cache,
        ]
        if not recommends:
            command.append("--no-install-recommends")
        command.extend(["install", *[item["name"] for item in roots]])
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DebianArtifactError("could not run the isolated APT solver") from exc
    if result.returncode != 0:
        raise DebianArtifactError("isolated APT solver failed: %s" % result.stderr.strip())
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("'https://"):
            continue
        try:
            fields = shlex.split(line)
            url, filename, size_text = fields[:3]
            size = int(size_text)
        except (ValueError, IndexError) as exc:
            raise DebianArtifactError("APT emitted a malformed package URI") from exc
        if filename in seen:
            raise DebianArtifactError("APT emitted a duplicate package artifact")
        seen.add(filename)
        packages.append({"url": url, "filename": filename, "size": size})
    if not packages:
        raise DebianArtifactError("APT solver selected no package artifacts")
    return packages


def _cache_filename(record: dict[str, str]) -> str:
    version = record["Version"].replace(":", "%3a")
    return "%s_%s_%s.deb" % (record["Package"], version, record["Architecture"])


def _index_identity(path: Path) -> tuple[str, str]:
    name = path.name
    if name.startswith("security.debian.org_"):
        origin = "security.debian.org"
    elif name.startswith("deb.debian.org_"):
        origin = "deb.debian.org"
    else:
        raise DebianArtifactError("package index is not from an approved Debian host")
    match = re.search(r"_dists_([^_]+)_main_binary-arm64_Packages$", name)
    if match is None:
        raise DebianArtifactError("package index filename is malformed: %s" % name)
    return origin, match.group(1)


def _load_indexes(index_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_filename: dict[str, list[dict[str, Any]]] = {}
    manifests: list[dict[str, Any]] = []
    index_paths = sorted(index_dir.glob("*_main_binary-arm64_Packages"))
    if not index_paths:
        raise DebianArtifactError("no authenticated ARM64 package indexes were found")
    for path in index_paths:
        if path.is_symlink() or not path.is_file():
            raise DebianArtifactError("package index is missing or unsafe")
        origin, suite = _index_identity(path)
        index_id = "%s:%s:main:arm64" % (origin, suite)
        inrelease_name = path.name.split("_main_binary-arm64_Packages", 1)[0] + "_InRelease"
        inrelease = index_dir / inrelease_name
        if inrelease.is_symlink() or not inrelease.is_file():
            raise DebianArtifactError("authenticated package index lacks its InRelease file")
        manifests.append(
            {
                "id": index_id,
                "origin": origin,
                "suite": suite,
                "component": "main",
                "architecture": "arm64",
                "inrelease_url": OFFICIAL_BASES[origin] + "dists/%s/InRelease" % suite,
                "inrelease_sha256": sha256_file(inrelease),
                "packages_sha256": sha256_file(path),
                "verified_by": "APT 3.0.3 using the signed base image Debian keyring",
            }
        )
        for record in parse_control_file(path):
            required = {
                "Package",
                "Version",
                "Architecture",
                "Filename",
                "Size",
                "SHA256",
            }
            if not required.issubset(record):
                raise DebianArtifactError("Debian Packages record lacks required fields")
            entry = dict(record)
            entry["_index_id"] = index_id
            entry["_origin"] = origin
            by_filename.setdefault(_cache_filename(record), []).append(entry)
    return by_filename, manifests


def _installed_packages(status_path: Path) -> dict[str, str]:
    installed: dict[str, str] = {}
    for record in parse_control_file(status_path):
        if record.get("Status") != "install ok installed":
            continue
        name = record.get("Package")
        version = record.get("Version")
        if isinstance(name, str) and isinstance(version, str):
            installed[name] = version
    return installed


def create_debian_lock(
    roots_path: Path,
    apt_command: Path,
    index_dir: Path,
    archive_dir: Path,
    base_status: Path,
    base_image_sha256: str,
    resolved_on: str,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(base_image_sha256):
        raise DebianArtifactError("base image SHA-256 is malformed")
    if not DATE_RE.fullmatch(resolved_on):
        raise DebianArtifactError("resolution date is malformed")
    roots, recommends = load_roots(roots_path)
    plan = apt_plan(apt_command.resolve(strict=True), roots, recommends)
    indexes, index_manifests = _load_indexes(index_dir.resolve(strict=True))
    archive_dir = archive_dir.resolve(strict=True)
    actual_files = {
        path.name: path
        for path in archive_dir.glob("*.deb")
        if path.is_file() and not path.is_symlink()
    }
    planned_names = {item["filename"] for item in plan}
    if set(actual_files) != planned_names:
        missing = sorted(planned_names - set(actual_files))
        extra = sorted(set(actual_files) - planned_names)
        raise DebianArtifactError(
            "Debian archive cache differs from APT plan; missing=%s extra=%s"
            % (missing, extra)
        )

    artifacts: list[dict[str, Any]] = []
    for item in plan:
        path = actual_files[item["filename"]]
        actual_hash = sha256_file(path)
        actual_size = path.stat().st_size
        candidates = [
            record
            for record in indexes.get(item["filename"], [])
            if record["SHA256"] == actual_hash
            and int(record["Size"]) == actual_size
            and unquote(item["url"]).endswith("/" + record["Filename"])
            and item["url"].startswith(OFFICIAL_BASES[record["_origin"]])
        ]
        if not candidates:
            raise DebianArtifactError(
                "package does not map to an authenticated index: %s"
                % item["filename"]
            )
        identities = {
            (
                record["Package"],
                record["Version"],
                record["Architecture"],
                record["Filename"],
                record.get("Installed-Size"),
                record["SHA256"],
            )
            for record in candidates
        }
        if len(identities) != 1:
            raise DebianArtifactError(
                "authenticated indexes disagree about package identity: %s"
                % item["filename"]
            )
        record = sorted(candidates, key=lambda value: value["_index_id"])[0]
        source = record.get("Source", record["Package"]).split(" ", 1)[0]
        installed_size_text = record.get("Installed-Size")
        try:
            installed_size_kib = int(installed_size_text or "")
        except ValueError as exc:
            raise DebianArtifactError(
                "package has a malformed authenticated Installed-Size: %s"
                % item["filename"]
            ) from exc
        if installed_size_kib < 0:
            raise DebianArtifactError(
                "package has a negative authenticated Installed-Size: %s"
                % item["filename"]
            )
        artifacts.append(
            {
                "package": record["Package"],
                "source_package": source,
                "version": record["Version"],
                "architecture": record["Architecture"],
                "filename": item["filename"],
                "size": actual_size,
                "installed_size_kib": installed_size_kib,
                "sha256": actual_hash,
                "url": item["url"],
                "indexes": sorted({value["_index_id"] for value in candidates}),
                "component": "main",
                "license_policy": "Debian main (DFSG); package copyright file retained",
            }
        )
    artifacts.sort(key=lambda value: (value["package"], value["architecture"]))

    installed = _installed_packages(base_status)
    selected = {item["package"] for item in artifacts}
    root_results: list[dict[str, str]] = []
    for root in roots:
        if root["name"] in selected:
            disposition = "install-or-upgrade"
        elif root["name"] in installed:
            disposition = "already-in-signed-base"
        else:
            raise DebianArtifactError("APT plan did not satisfy root package: %s" % root["name"])
        root_results.append({**root, "disposition": disposition})
    upgraded = [item for item in artifacts if item["package"] in installed]
    total_installed_size_kib = sum(
        item["installed_size_kib"] for item in artifacts
    )
    return {
        "schema_version": 1,
        "resolved_on": resolved_on,
        "target": {"distribution": "Debian 13 Trixie", "architecture": "arm64"},
        "base_image_sha256": base_image_sha256,
        "base_dpkg_status_sha256": sha256_file(base_status),
        "root_policy_sha256": sha256_file(roots_path),
        "solver": {
            "name": "APT",
            "version": "3.0.3",
            "install_recommends": recommends,
            "selected": len(artifacts),
            "upgrades_from_signed_base": len(upgraded),
            "new_on_signed_base": len(artifacts) - len(upgraded),
            "total_installed_size_kib": total_installed_size_kib,
        },
        "roots": root_results,
        "repository_indexes": sorted(index_manifests, key=lambda value: value["id"]),
        "artifacts": artifacts,
    }


def lock_bytes(lock: dict[str, Any]) -> bytes:
    return (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")


def lock_digest(lock: dict[str, Any]) -> str:
    return hashlib.sha256(lock_bytes(lock)).hexdigest()


def validate_debian_lock(lock: dict[str, Any]) -> None:
    if lock.get("schema_version") != 1 or not DATE_RE.fullmatch(
        str(lock.get("resolved_on", ""))
    ):
        raise DebianArtifactError("Debian artifact lock header is malformed")
    if lock.get("target") != {
        "distribution": "Debian 13 Trixie",
        "architecture": "arm64",
    }:
        raise DebianArtifactError("Debian artifact lock target is not the pinned base")
    for key in ("base_image_sha256", "base_dpkg_status_sha256", "root_policy_sha256"):
        value = lock.get(key)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise DebianArtifactError("Debian artifact lock %s is malformed" % key)

    roots = lock.get("roots")
    if not isinstance(roots, list) or not roots:
        raise DebianArtifactError("Debian artifact lock has no root policy")
    root_names: set[str] = set()
    for root in roots:
        if not isinstance(root, dict):
            raise DebianArtifactError("Debian root entry must be an object")
        name = root.get("name")
        if not isinstance(name, str) or not PACKAGE_RE.fullmatch(name) or name in root_names:
            raise DebianArtifactError("Debian root package is malformed or duplicated")
        root_names.add(name)
        if root.get("disposition") not in {"install-or-upgrade", "already-in-signed-base"}:
            raise DebianArtifactError("Debian root package disposition is invalid")
        if not isinstance(root.get("purpose"), str) or not root["purpose"]:
            raise DebianArtifactError("Debian root package purpose is missing")

    indexes = lock.get("repository_indexes")
    if not isinstance(indexes, list) or not indexes:
        raise DebianArtifactError("Debian artifact lock has no repository indexes")
    index_ids: set[str] = set()
    for index in indexes:
        if not isinstance(index, dict):
            raise DebianArtifactError("Debian repository index must be an object")
        index_id = index.get("id")
        if not isinstance(index_id, str) or not index_id or index_id in index_ids:
            raise DebianArtifactError("Debian repository index ID is malformed or duplicated")
        index_ids.add(index_id)
        if index.get("architecture") != "arm64" or index.get("component") != "main":
            raise DebianArtifactError("Debian repository index is outside main/arm64")
        url = index.get("inrelease_url")
        if not isinstance(url, str) or not any(url.startswith(base) for base in OFFICIAL_BASES.values()):
            raise DebianArtifactError("Debian InRelease URL is not official HTTPS")
        for key in ("inrelease_sha256", "packages_sha256"):
            value = index.get(key)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise DebianArtifactError("Debian repository index hash is malformed")

    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise DebianArtifactError("Debian artifact lock has no packages")
    filenames: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise DebianArtifactError("Debian artifact must be an object")
        package = artifact.get("package")
        source = artifact.get("source_package")
        version = artifact.get("version")
        architecture = artifact.get("architecture")
        filename = artifact.get("filename")
        if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
            raise DebianArtifactError("Debian package name is malformed")
        if not isinstance(source, str) or not PACKAGE_RE.fullmatch(source):
            raise DebianArtifactError("Debian source package name is malformed")
        if not isinstance(version, str) or not version:
            raise DebianArtifactError("Debian package version is missing")
        if architecture not in {"arm64", "all"}:
            raise DebianArtifactError("Debian package architecture is not arm64/all")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".deb")
            or filename in filenames
        ):
            raise DebianArtifactError("Debian package filename is unsafe or duplicated")
        filenames.add(filename)
        identity = (package, architecture)
        if identity in identities:
            raise DebianArtifactError("Debian package identity is duplicated")
        identities.add(identity)
        size = artifact.get("size")
        if not isinstance(size, int) or size <= 0:
            raise DebianArtifactError("Debian package size is invalid")
        installed_size_kib = artifact.get("installed_size_kib")
        if not isinstance(installed_size_kib, int) or installed_size_kib < 0:
            raise DebianArtifactError("Debian package installed size is invalid")
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise DebianArtifactError("Debian package SHA-256 is malformed")
        url = artifact.get("url")
        if not isinstance(url, str) or not any(url.startswith(base) for base in OFFICIAL_BASES.values()):
            raise DebianArtifactError("Debian package URL is not official HTTPS")
        artifact_indexes = artifact.get("indexes")
        if (
            not isinstance(artifact_indexes, list)
            or not artifact_indexes
            or not set(artifact_indexes).issubset(index_ids)
        ):
            raise DebianArtifactError("Debian package has no valid authenticated index")
        if artifact.get("component") != "main":
            raise DebianArtifactError("Debian package is outside the main component")
        if not isinstance(artifact.get("license_policy"), str) or not artifact["license_policy"]:
            raise DebianArtifactError("Debian package license policy is missing")

    solver = lock.get("solver")
    if not isinstance(solver, dict) or solver.get("name") != "APT" or solver.get("version") != "3.0.3":
        raise DebianArtifactError("Debian solver identity is not pinned")
    if solver.get("install_recommends") is not False:
        raise DebianArtifactError("Debian solver must disable unreviewed recommendations")
    if solver.get("selected") != len(artifacts):
        raise DebianArtifactError("Debian solver selected count does not match artifacts")
    if solver.get("total_installed_size_kib") != sum(
        item["installed_size_kib"] for item in artifacts
    ):
        raise DebianArtifactError(
            "Debian solver installed-size total does not match artifacts"
        )
    counts = solver.get("upgrades_from_signed_base"), solver.get("new_on_signed_base")
    if not all(isinstance(value, int) and value >= 0 for value in counts):
        raise DebianArtifactError("Debian solver base counts are malformed")
    if sum(counts) != len(artifacts):
        raise DebianArtifactError("Debian solver base counts do not add up")


def load_debian_lock(path: Path) -> dict[str, Any]:
    lock = _read_json(path)
    validate_debian_lock(lock)
    return lock

"""Create and verify the offline Python artifact lock for T300 mainline."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Any, Callable
from urllib.request import Request, urlopen
import zipfile

from .lockfile import sha256_file


NAME_RE = re.compile(r"[-_.]+")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"
MAX_METADATA_BYTES = 2 * 1024 * 1024


class PythonArtifactError(RuntimeError):
    pass


def canonical_name(value: str) -> str:
    return NAME_RE.sub("-", value).lower()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PythonArtifactError("could not read JSON file %s" % (path,)) from exc
    if not isinstance(value, dict):
        raise PythonArtifactError("JSON root must be an object: %s" % (path,))
    return value


def report_packages(
    report_path: Path, extras: tuple[tuple[str, str], ...] = ()
) -> dict[str, dict[str, Any]]:
    report = _read_json(report_path)
    if report.get("version") != "1" or not isinstance(report.get("install"), list):
        raise PythonArtifactError("unsupported pip report: %s" % (report_path,))
    packages: dict[str, dict[str, Any]] = {}
    for item in report["install"]:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise PythonArtifactError("pip report contains malformed install metadata")
        metadata = item["metadata"]
        name = metadata.get("name")
        version = metadata.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise PythonArtifactError("pip report package lacks name or version")
        canonical = canonical_name(name)
        if canonical in packages:
            raise PythonArtifactError("pip report contains a duplicate package: %s" % (name,))
        packages[canonical] = {
            "name": name,
            "version": version,
            "requested": bool(item.get("requested")),
        }
    for name, version in extras:
        canonical = canonical_name(name)
        if canonical in packages:
            raise PythonArtifactError("extra package duplicates pip report: %s" % (name,))
        packages[canonical] = {"name": name, "version": version, "requested": False}
    return packages


def _fetch_release(
    name: str,
    version: str,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    request = Request(
        PYPI_URL.format(name=name, version=version),
        headers={"User-Agent": "t300-mainline-lock/1"},
    )
    try:
        with opener(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PythonArtifactError(
            "could not read official PyPI metadata for %s==%s" % (name, version)
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
        raise PythonArtifactError("PyPI returned malformed package metadata")
    if canonical_name(str(payload["info"].get("name", ""))) != canonical_name(name):
        raise PythonArtifactError("PyPI package name does not match the request")
    if str(payload["info"].get("version", "")) != version:
        raise PythonArtifactError("PyPI package version does not match the request")
    if not isinstance(payload.get("urls"), list):
        raise PythonArtifactError("PyPI release has no artifact list")
    return payload


def _distribution_metadata(path: Path, packagetype: str) -> bytes:
    try:
        if packagetype == "bdist_wheel":
            with zipfile.ZipFile(path) as bundle:
                matches = [
                    item
                    for item in bundle.infolist()
                    if len(PurePosixPath(item.filename).parts) == 2
                    and PurePosixPath(item.filename).parts[0].endswith(".dist-info")
                    and PurePosixPath(item.filename).parts[1] == "METADATA"
                    and not item.is_dir()
                ]
                if len(matches) != 1 or matches[0].file_size > MAX_METADATA_BYTES:
                    raise PythonArtifactError(
                        "wheel must contain one bounded top-level METADATA file: %s"
                        % (path.name,)
                    )
                return bundle.read(matches[0])
        if packagetype == "sdist":
            with tarfile.open(path, "r:*") as bundle:
                matches = [
                    item
                    for item in bundle.getmembers()
                    if len(PurePosixPath(item.name).parts) == 2
                    and PurePosixPath(item.name).parts[1] == "PKG-INFO"
                    and item.isfile()
                ]
                if len(matches) != 1 or matches[0].size > MAX_METADATA_BYTES:
                    raise PythonArtifactError(
                        "sdist must contain one bounded top-level PKG-INFO file: %s"
                        % (path.name,)
                    )
                extracted = bundle.extractfile(matches[0])
                if extracted is None:
                    raise PythonArtifactError(
                        "could not read sdist metadata: %s" % (path.name,)
                    )
                with extracted:
                    return extracted.read(MAX_METADATA_BYTES + 1)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PythonArtifactError(
            "could not inspect distribution metadata: %s" % (path.name,)
        ) from exc
    raise PythonArtifactError("unsupported PyPI artifact type: %s" % (packagetype,))


def _clean_license(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or cleaned.upper() in {"UNKNOWN", "UNKNOWN LICENSE"}:
        return None
    return cleaned


def _artifact_license(
    path: Path,
    packagetype: str,
    expected_name: str,
    expected_version: str,
    pypi_info: dict[str, Any],
) -> tuple[str, str]:
    metadata_bytes = _distribution_metadata(path, packagetype)
    if len(metadata_bytes) > MAX_METADATA_BYTES:
        raise PythonArtifactError("distribution metadata exceeds the size limit")
    metadata = BytesParser(policy=compat32).parsebytes(metadata_bytes, headersonly=True)
    if canonical_name(metadata.get("Name", "")) != canonical_name(expected_name):
        raise PythonArtifactError("distribution metadata package name does not match")
    if metadata.get("Version") != expected_version:
        raise PythonArtifactError("distribution metadata package version does not match")

    for field in ("License-Expression", "License"):
        value = _clean_license(metadata.get(field))
        if value is not None:
            return value, "artifact-metadata:%s" % (field,)
    artifact_classifiers = [
        item
        for value in metadata.get_all("Classifier", [])
        if (item := _clean_license(value)) is not None
        and item.startswith("License ::")
    ]
    if artifact_classifiers:
        return "; ".join(sorted(set(artifact_classifiers))), "artifact-metadata:Classifier"

    for field in ("license_expression", "license"):
        value = _clean_license(pypi_info.get(field))
        if value is not None:
            return value, "pypi:%s" % (field,)
    pypi_classifiers = [
        item
        for value in pypi_info.get("classifiers", [])
        if (item := _clean_license(value)) is not None
        and item.startswith("License ::")
    ]
    if pypi_classifiers:
        return "; ".join(sorted(set(pypi_classifiers))), "pypi:Classifier"
    raise PythonArtifactError(
        "package has no declared license metadata: %s==%s"
        % (expected_name, expected_version)
    )


def lock_environment(
    name: str,
    report_path: Path,
    wheelhouse: Path,
    requirements_path: Path | None = None,
    requirements_lock_path: str | None = None,
    extras: tuple[tuple[str, str], ...] = (),
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    if (requirements_path is None) != (requirements_lock_path is None):
        raise PythonArtifactError(
            "requirements_path and requirements_lock_path must be supplied together"
        )
    if requirements_lock_path is not None:
        logical_path = PurePosixPath(requirements_lock_path)
        if (
            not logical_path.is_absolute()
            or logical_path.parts[:4] != ("/", "opt", "t300", "src")
            or ".." in logical_path.parts
        ):
            raise PythonArtifactError(
                "requirements_lock_path must be below /opt/t300/src"
            )
    wheelhouse = wheelhouse.resolve(strict=True)
    local_files = {
        path.name: path
        for path in wheelhouse.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if not local_files:
        raise PythonArtifactError("wheelhouse is empty: %s" % (wheelhouse,))
    packages = report_packages(report_path, extras)
    artifacts: list[dict[str, Any]] = []
    selected: set[str] = set()
    for canonical in sorted(packages):
        package = packages[canonical]
        payload = _fetch_release(package["name"], package["version"], opener)
        matches = [item for item in payload["urls"] if item.get("filename") in local_files]
        if len(matches) != 1:
            raise PythonArtifactError(
                "expected exactly one cached target artifact for %s==%s"
                % (package["name"], package["version"])
            )
        release = matches[0]
        if bool(release.get("yanked")):
            raise PythonArtifactError(
                "refusing yanked PyPI artifact: %s" % (release["filename"],)
            )
        filename = release["filename"]
        selected.add(filename)
        expected = release.get("digests", {}).get("sha256")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise PythonArtifactError("PyPI artifact lacks a SHA-256 digest")
        actual = sha256_file(local_files[filename])
        if actual != expected:
            raise PythonArtifactError("cached PyPI artifact hash mismatch: %s" % (filename,))
        url = release.get("url")
        if not isinstance(url, str) or not url.startswith("https://files.pythonhosted.org/"):
            raise PythonArtifactError("PyPI artifact URL is not an official HTTPS file URL")
        info = payload["info"]
        packagetype = release.get("packagetype")
        if packagetype not in {"bdist_wheel", "sdist"}:
            raise PythonArtifactError("PyPI artifact has an unsupported package type")
        license_name, license_source = _artifact_license(
            local_files[filename],
            packagetype,
            package["name"],
            package["version"],
            info,
        )
        artifacts.append(
            {
                "package": info["name"],
                "version": package["version"],
                "requested": package["requested"],
                "filename": filename,
                "sha256": expected,
                "url": url,
                "packagetype": packagetype,
                "requires_python": info.get("requires_python"),
                "license": license_name,
                "license_source": license_source,
                "yanked": False,
            }
        )
    extras_on_disk = sorted(set(local_files) - selected)
    if extras_on_disk:
        raise PythonArtifactError(
            "wheelhouse contains unlocked artifacts: %s" % (", ".join(extras_on_disk),)
        )
    result: dict[str, Any] = {
        "name": name,
        "pip_report_sha256": sha256_file(report_path),
        "artifacts": artifacts,
    }
    if requirements_path is not None:
        result["requirements_path"] = requirements_lock_path
        result["requirements_sha256"] = sha256_file(requirements_path)
    return result


def create_lock(
    environments: list[dict[str, Any]], resolved_on: str
) -> dict[str, Any]:
    names = [item["name"] for item in environments]
    if len(names) != len(set(names)):
        raise PythonArtifactError("Python environment names must be unique")
    return {
        "schema_version": 2,
        "resolved_on": resolved_on,
        "bootstrap": {
            "pip": "25.1.1",
            "source": "locked Debian python3-pip-whl 25.1.1+dfsg-1",
        },
        "target": {
            "implementation": "CPython",
            "python": "3.13.5",
            "abi": "cp313",
            "architecture": "aarch64",
            "platforms": ["manylinux_2_28_aarch64", "manylinux2014_aarch64"],
        },
        "environments": environments,
    }


def lock_bytes(lock: dict[str, Any]) -> bytes:
    return (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")


def lock_digest(lock: dict[str, Any]) -> str:
    return hashlib.sha256(lock_bytes(lock)).hexdigest()


def validate_artifact_lock(lock: dict[str, Any]) -> None:
    if lock.get("schema_version") != 2:
        raise PythonArtifactError("unsupported Python artifact lock schema")
    if lock.get("bootstrap") != {
        "pip": "25.1.1",
        "source": "locked Debian python3-pip-whl 25.1.1+dfsg-1",
    }:
        raise PythonArtifactError("Python build bootstrap is not exactly pinned")
    if not isinstance(lock.get("resolved_on"), str) or not DATE_RE.fullmatch(
        lock["resolved_on"]
    ):
        raise PythonArtifactError("Python artifact lock has an invalid resolution date")
    if lock.get("target") != {
        "implementation": "CPython",
        "python": "3.13.5",
        "abi": "cp313",
        "architecture": "aarch64",
        "platforms": ["manylinux_2_28_aarch64", "manylinux2014_aarch64"],
    }:
        raise PythonArtifactError("Python artifact lock target is not the pinned runtime")

    environments = lock.get("environments")
    if not isinstance(environments, list) or not environments:
        raise PythonArtifactError("Python artifact environments must be a non-empty list")
    expected_names = {"build", "klipper", "moonraker"}
    names = {item.get("name") for item in environments if isinstance(item, dict)}
    if names != expected_names or len(environments) != len(expected_names):
        raise PythonArtifactError("Python artifact environments do not match the runtime set")

    for environment in environments:
        name = environment["name"]
        report_hash = environment.get("pip_report_sha256")
        if not isinstance(report_hash, str) or not SHA256_RE.fullmatch(report_hash):
            raise PythonArtifactError("environment pip report hash is malformed: %s" % name)
        requirement_path = environment.get("requirements_path")
        requirement_hash = environment.get("requirements_sha256")
        if name == "build":
            if requirement_path is not None or requirement_hash is not None:
                raise PythonArtifactError("build environment must not claim a runtime requirement file")
        else:
            logical = PurePosixPath(str(requirement_path))
            if (
                not isinstance(requirement_path, str)
                or not logical.is_absolute()
                or logical.parts[:4] != ("/", "opt", "t300", "src")
                or ".." in logical.parts
            ):
                raise PythonArtifactError("runtime requirement path is unsafe: %s" % name)
            if not isinstance(requirement_hash, str) or not SHA256_RE.fullmatch(
                requirement_hash
            ):
                raise PythonArtifactError("runtime requirement hash is malformed: %s" % name)

        artifacts = environment.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise PythonArtifactError("environment has no locked artifacts: %s" % name)
        filenames: set[str] = set()
        package_versions: set[tuple[str, str]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise PythonArtifactError("artifact entry must be an object")
            filename = artifact.get("filename")
            if (
                not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or filename in filenames
            ):
                raise PythonArtifactError("artifact filename is unsafe or duplicated")
            filenames.add(filename)
            package = artifact.get("package")
            version = artifact.get("version")
            if not isinstance(package, str) or not package or not isinstance(version, str) or not version:
                raise PythonArtifactError("artifact package identity is incomplete")
            identity = (canonical_name(package), version)
            if identity in package_versions:
                raise PythonArtifactError("artifact package identity is duplicated")
            package_versions.add(identity)
            digest = artifact.get("sha256")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise PythonArtifactError("artifact SHA-256 is malformed")
            url = artifact.get("url")
            if (
                not isinstance(url, str)
                or not url.startswith("https://files.pythonhosted.org/")
                or url.rsplit("/", 1)[-1] != filename
            ):
                raise PythonArtifactError("artifact URL is not the exact official filename")
            if artifact.get("packagetype") not in {"bdist_wheel", "sdist"}:
                raise PythonArtifactError("artifact package type is unsupported")
            if artifact.get("yanked") is not False:
                raise PythonArtifactError("artifact must be explicitly non-yanked")
            if not isinstance(artifact.get("requested"), bool):
                raise PythonArtifactError("artifact requested marker must be boolean")
            for key in ("license", "license_source"):
                if not isinstance(artifact.get(key), str) or not artifact[key]:
                    raise PythonArtifactError("artifact %s is missing" % key)


def load_artifact_lock(path: Path) -> dict[str, Any]:
    lock = _read_json(path)
    validate_artifact_lock(lock)
    return lock

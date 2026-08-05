"""Validation helpers for the reproducible T300 stack lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT_RE = re.compile(r"^[0-9A-F]{40}$")
HTTPS_RE = re.compile(r"^https://")


class LockfileError(ValueError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def load_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockfileError(f"could not read stack lock {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LockfileError("stack lock must contain one JSON object")
    validate_lock(value)
    return value


def _require_sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise LockfileError(f"{label} must be a lowercase SHA-256 digest")


def _require_https(value: Any, label: str) -> None:
    if not isinstance(value, str) or not HTTPS_RE.match(value):
        raise LockfileError(f"{label} must be an https URL")


def validate_lock(lock: dict[str, Any]) -> None:
    if lock.get("schema_version") != 1:
        raise LockfileError("unsupported stack lock schema")
    if lock.get("profile") != "stable":
        raise LockfileError("the deployable lock must use the stable profile")

    base = lock.get("base_image")
    if not isinstance(base, dict):
        raise LockfileError("base_image must be an object")
    for key in ("url", "signature_url", "checksum_url"):
        _require_https(base.get(key), f"base_image.{key}")
    _require_sha(base.get("sha256"), "base_image.sha256")
    _require_sha(base.get("signature_sha256"), "base_image.signature_sha256")
    _require_sha(base.get("checksum_sha256"), "base_image.checksum_sha256")
    signing_key = base.get("signing_key")
    if not isinstance(signing_key, dict):
        raise LockfileError("base_image.signing_key must be an object")
    if not isinstance(signing_key.get("name"), str) or not signing_key["name"]:
        raise LockfileError("base_image.signing_key.name is required")
    _require_https(signing_key.get("url"), "base_image.signing_key.url")
    _require_sha(signing_key.get("sha256"), "base_image.signing_key.sha256")
    fingerprint = signing_key.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
        raise LockfileError(
            "base_image.signing_key.fingerprint must be a full uppercase fingerprint"
        )
    _require_https(
        signing_key.get("verification_guide"),
        "base_image.signing_key.verification_guide",
    )

    recovery_boot = lock.get("recovery_boot")
    if not isinstance(recovery_boot, dict):
        raise LockfileError("recovery_boot must be an object")
    if recovery_boot.get("method") != "interactive-serial-u-boot-usb0":
        raise LockfileError("recovery_boot.method must require interactive serial U-Boot")
    if recovery_boot.get("serial_baud") != 1500000:
        raise LockfileError("recovery_boot.serial_baud must match the Klipad50 console")
    root_uuid = recovery_boot.get("root_uuid")
    if (
        not isinstance(root_uuid, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            root_uuid,
        )
        is None
    ):
        raise LockfileError("recovery_boot.root_uuid must be a lowercase filesystem UUID")
    if recovery_boot.get("fdtfile") != "rockchip/rk3328-mksklipad50.dtb":
        raise LockfileError("recovery_boot.fdtfile must select the Klipad50 device tree")
    for key in (
        "dtb_sha256",
        "image_sha256",
        "uinitrd_sha256",
        "boot_cmd_sha256",
        "boot_scr_sha256",
    ):
        _require_sha(recovery_boot.get(key), f"recovery_boot.{key}")

    components = lock.get("components")
    if not isinstance(components, list) or not components:
        raise LockfileError("components must be a non-empty list")
    names: set[str] = set()
    release_assets: set[str] = set()
    for index, component in enumerate(components):
        label = f"components[{index}]"
        if not isinstance(component, dict):
            raise LockfileError(f"{label} must be an object")
        name = component.get("name")
        if not isinstance(name, str) or not name:
            raise LockfileError(f"{label}.name is required")
        if name in names:
            raise LockfileError(f"duplicate component name: {name}")
        names.add(name)
        commit = component.get("commit")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise LockfileError(f"{label}.commit must be a full lowercase Git commit")
        _require_https(component.get("repository"), f"{label}.repository")
        _require_https(component.get("archive_url"), f"{label}.archive_url")
        _require_sha(component.get("archive_sha256"), f"{label}.archive_sha256")
        if not isinstance(component.get("license"), str):
            raise LockfileError(f"{label}.license is required")
        asset = component.get("release_asset")
        if asset is not None:
            release_assets.add(name)
            if not isinstance(asset, dict):
                raise LockfileError(f"{label}.release_asset must be an object")
            asset_name = asset.get("name")
            if (
                not isinstance(asset_name, str)
                or not asset_name
                or "/" in asset_name
                or "\\" in asset_name
                or asset_name in (".", "..")
            ):
                raise LockfileError(
                    f"{label}.release_asset.name must be one safe filename"
                )
            _require_https(asset.get("url"), f"{label}.release_asset.url")
            _require_sha(asset.get("sha256"), f"{label}.release_asset.sha256")
            size = asset.get("size")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or size > 64 * 1024 * 1024
            ):
                raise LockfileError(
                    f"{label}.release_asset.size must be in (0, 64 MiB]"
                )
            if asset.get("format") != "zip":
                raise LockfileError(f"{label}.release_asset.format must be zip")
            if asset.get("version") != component.get("version"):
                raise LockfileError(
                    f"{label}.release_asset.version must match the component"
                )
    if release_assets != {"mainsail"}:
        raise LockfileError(
            "the stable stack must lock exactly the compiled Mainsail release asset"
        )

    patches = lock.get("compatibility_patches")
    if not isinstance(patches, list):
        raise LockfileError("compatibility_patches must be a list")
    patch_names: set[str] = set()
    for index, patch in enumerate(patches):
        label = f"compatibility_patches[{index}]"
        if not isinstance(patch, dict):
            raise LockfileError(f"{label} must be an object")
        name = patch.get("name")
        if not isinstance(name, str) or not name or name in patch_names:
            raise LockfileError(f"{label}.name must be unique and non-empty")
        patch_names.add(name)
        if patch.get("component") not in names:
            raise LockfileError(f"{label}.component is not a locked component")
        base_commit = patch.get("base_commit")
        if not isinstance(base_commit, str) or not COMMIT_RE.fullmatch(base_commit):
            raise LockfileError(f"{label}.base_commit must be a full lowercase commit")
        component = next(
            item for item in components if item["name"] == patch["component"]
        )
        if patch["base_commit"] != component["commit"]:
            raise LockfileError(f"{label}.base_commit does not match its component")
        origin = patch.get("origin", "upstream")
        if origin == "upstream":
            value = patch.get("upstream_commit")
            if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
                raise LockfileError(
                    f"{label}.upstream_commit must be a full lowercase commit"
                )
            _require_https(patch.get("upstream_url"), f"{label}.upstream_url")
        elif origin == "local":
            _require_https(
                patch.get("design_reference_url"),
                f"{label}.design_reference_url",
            )
            if "upstream_commit" in patch or "upstream_url" in patch:
                raise LockfileError(
                    f"{label} local patch must not claim an upstream commit"
                )
        else:
            raise LockfileError(f"{label}.origin must be upstream or local")
        for key in ("sha256", "input_sha256", "output_sha256"):
            _require_sha(patch.get(key), f"{label}.{key}")
        for key in ("condition", "path", "input_path", "license"):
            if not isinstance(patch.get(key), str) or not patch[key]:
                raise LockfileError(f"{label}.{key} is required")
        for key in ("path", "input_path"):
            path = patch[key]
            if path.startswith("/") or ".." in path.split("/"):
                raise LockfileError(f"{label}.{key} must be a safe relative path")

    firmware = lock.get("firmware")
    if not isinstance(firmware, dict):
        raise LockfileError("firmware lock is missing")
    provenance_path = firmware.get("provenance_path")
    if (
        not isinstance(provenance_path, str)
        or not provenance_path
        or provenance_path.startswith("/")
        or ".." in provenance_path.split("/")
    ):
        raise LockfileError("firmware.provenance_path must be a safe relative path")
    _require_sha(firmware.get("provenance_sha256"), "firmware.provenance_sha256")
    if firmware.get("build_count") != 2 or firmware.get("flash_capability") is not False:
        raise LockfileError("firmware lock must contain two non-flashing builds")

    python_artifacts = lock.get("python_artifacts")
    if not isinstance(python_artifacts, dict):
        raise LockfileError("python_artifacts must be an object")
    artifact_path = python_artifacts.get("path")
    if (
        not isinstance(artifact_path, str)
        or not artifact_path
        or artifact_path.startswith("/")
        or ".." in artifact_path.split("/")
    ):
        raise LockfileError("python_artifacts.path must be a safe relative path")
    _require_sha(python_artifacts.get("sha256"), "python_artifacts.sha256")
    python_target = python_artifacts.get("target")
    expected_python_target = {
        "python": "3.13.5",
        "abi": "cp313",
        "architecture": lock.get("target", {}).get("architecture"),
    }
    if python_target != expected_python_target:
        raise LockfileError(
            "python_artifacts.target must match the signed base runtime and target"
        )

    debian_artifacts = lock.get("debian_artifacts")
    if not isinstance(debian_artifacts, dict):
        raise LockfileError("debian_artifacts must be an object")
    for key in ("path", "root_policy_path"):
        value = debian_artifacts.get(key)
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("/")
            or ".." in value.split("/")
        ):
            raise LockfileError(f"debian_artifacts.{key} must be a safe relative path")
    for key in ("sha256", "root_policy_sha256"):
        _require_sha(debian_artifacts.get(key), f"debian_artifacts.{key}")
    if debian_artifacts.get("artifact_count") != 353:
        raise LockfileError("debian_artifacts.artifact_count must match the reviewed closure")
    expected_debian_target = {
        "distribution": lock.get("target", {}).get("distribution"),
        "architecture": "arm64"
        if lock.get("target", {}).get("architecture") == "aarch64"
        else None,
    }
    if debian_artifacts.get("target") != expected_debian_target:
        raise LockfileError("debian_artifacts.target must match the signed base target")

    next_config = lock.get("next")
    if not isinstance(next_config, dict) or next_config.get("deployable") is not False:
        raise LockfileError("next must be explicitly marked non-deployable")
    resolved = next_config.get("last_resolved_commit")
    if not isinstance(resolved, str) or not COMMIT_RE.fullmatch(resolved):
        raise LockfileError("next.last_resolved_commit must be a full lowercase commit")


def artifact_records(lock: dict[str, Any]) -> list[dict[str, str]]:
    base = lock["base_image"]
    records = [
        {
            "name": base["name"],
            "url": base["url"],
            "sha256": base["sha256"],
        },
        {
            "name": f"{base['name']}.asc",
            "url": base["signature_url"],
            "sha256": base["signature_sha256"],
        },
        {
            "name": f"{base['name']}.sha",
            "url": base["checksum_url"],
            "sha256": base["checksum_sha256"],
        },
        {
            "name": base["signing_key"]["name"],
            "url": base["signing_key"]["url"],
            "sha256": base["signing_key"]["sha256"],
        },
    ]
    for component in lock["components"]:
        records.append(
            {
                "name": f"{component['name']}-{component['commit'][:8]}.tar.gz",
                "url": component["archive_url"],
                "sha256": component["archive_sha256"],
            }
        )
        asset = component.get("release_asset")
        if asset is not None:
            records.append(
                {
                    "name": asset["name"],
                    "url": asset["url"],
                    "sha256": asset["sha256"],
                }
            )
    return records

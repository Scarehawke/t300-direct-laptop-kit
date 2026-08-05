"""Owner-gated commissioning transitions for a provisioned T300 candidate.

Every mutating transition is previewable without ``--apply``. The controller
can start host-only services and locked Klipper, but it has no G-code client and
cannot request movement, homing, heating, calibration, or firmware operations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from .lockfile import sha256_file


HOST_MARKER = "host-validated"
STORAGE_MARKER = "storage-enabled"
PRODUCTION_MARKER = "production-enabled"
MAINTENANCE_MARKER = "maintenance-enabled"
TRANSPORT_MARKER = "transport-enabled"
RELEASE_MARKER = "release-validated"
SSH_UNIT = "ssh.service"
DATA_MOUNT_UNIT = r"mnt-t300\x2ddata.mount"
GCODE_BIND_MOUNT_UNIT = r"var-lib-t300-moonraker\x2ddata-gcodes.mount"
STORAGE_UNITS = (DATA_MOUNT_UNIT, GCODE_BIND_MOUNT_UNIT)
HOST_UNITS = (
    "t300-xorg.service",
    "crowsnest.service",
    "mainsail.service",
    "klipperscreen.service",
)
PRODUCTION_UNITS = (
    "t300-admission.service",
    "klipper.service",
    "moonraker.service",
)
NORMAL_PRINTER_UNITS = (*PRODUCTION_UNITS, "klipper-maintenance.service")
REQUIRED_HOST_EVIDENCE = (
    "screen_visible",
    "touch_works",
    "backlight_works",
    "cooling_fan_observed",
    "ethernet_works",
    "wifi_works",
    "usb_camera_detected",
    "camera_stream_works",
    "data_usb_mount_verified",
)
REQUIRED_MAINTENANCE_EVIDENCE = (
    "owner_at_printer",
    "bed_clear",
    "printer_idle",
    "hotend_below_50c",
    "bed_below_50c",
    "normal_services_stopped",
)
REQUIRED_RELEASE_EVIDENCE = (
    "owner_at_printer",
    "steel_sheet_installed",
    "printer_idle",
    "hotend_target_zero",
    "bed_target_zero",
    "hotend_below_50c",
    "bed_below_50c",
    "normal_services_stopped",
    "mcu_connected_without_motion_or_heat",
    "thermal_sensor_readings_plausible",
    "endstop_and_probe_states_reviewed",
    "calibration_values_reviewed",
)
REQUIRED_FINAL_RELEASE_EVIDENCE = (
    "owner_at_printer",
    "printer_idle",
    "hotend_target_zero",
    "bed_target_zero",
    "full_safety_validation_passed",
    "ui_peripheral_parity_passed",
    "first_layer_test_passed",
    "dimensional_print_passed",
    "orange_camera_bracket_passed",
    "normal_end_and_cancel_passed",
    "rendered_timelapse_verified",
    "network_camera_soak_passed",
    "recovery_image_filesystems_verified",
    "vendor_rollback_drill_passed",
)
CONFIG_DEPLOY_JOURNAL = "/var/lib/t300/config-deploy-journal.json"
FAT_UUID_RE = re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_EVIDENCE_BYTES = 1024 * 1024


class CommissioningError(RuntimeError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommissioningError("could not read JSON: %s" % path) from exc
    if not isinstance(value, dict):
        raise CommissioningError("JSON root must be an object: %s" % path)
    return value


def _read_evidence_snapshot(path: Path) -> tuple[dict[str, Any], str]:
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
            raise CommissioningError("evidence must be one bounded regular file")
        blocks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                raise CommissioningError("evidence changed while it was read")
            blocks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CommissioningError("evidence changed while it was read")
        after = os.fstat(descriptor)
        if not os.path.samestat(info, after) or info.st_mtime_ns != after.st_mtime_ns:
            raise CommissioningError("evidence changed while it was read")
    except OSError as exc:
        raise CommissioningError("evidence must be one bounded regular file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    content = b"".join(blocks)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommissioningError("evidence is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CommissioningError("evidence JSON root must be an object")
    return value, hashlib.sha256(content).hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any], mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
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
        temporary.unlink(missing_ok=True)


def inventory_configuration_tree(
    config_root: Path, strict_owner: bool = True
) -> dict[str, Any]:
    if not config_root.is_dir() or config_root.is_symlink():
        raise CommissioningError("T300 configuration root is missing or unsafe")
    records: list[dict[str, Any]] = []
    digest_lines: list[str] = []
    for path in sorted(config_root.rglob("*")):
        relative = path.relative_to(config_root)
        if relative.parts and relative.parts[0] == "commissioning":
            continue
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise CommissioningError("configuration contains a symlink: %s" % relative)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise CommissioningError("configuration contains a hard-linked file: %s" % relative)
        if strict_owner and (info.st_uid != 0 or info.st_mode & 0o022):
            raise CommissioningError("configuration is not immutable: %s" % relative)
        mode = info.st_mode & 0o777
        if stat.S_ISDIR(info.st_mode):
            record = {"type": "directory", "path": relative.as_posix(), "mode": mode}
            digest_lines.append("D\t%s\t%o" % (relative.as_posix(), mode))
        elif stat.S_ISREG(info.st_mode):
            digest = sha256_file(path)
            record = {
                "type": "file",
                "path": relative.as_posix(),
                "mode": mode,
                "size": info.st_size,
                "sha256": digest,
            }
            digest_lines.append(
                "F\t%s\t%o\t%d\t%s"
                % (relative.as_posix(), mode, info.st_size, digest)
            )
        else:
            raise CommissioningError(
                "configuration contains an unsupported object: %s" % relative
            )
        records.append(record)
    digest = hashlib.sha256(("\n".join(digest_lines) + "\n").encode("utf-8")).hexdigest()
    return {"schema_version": 1, "config_sha256": digest, "files": records}


class CandidateController:
    def __init__(self, root: Path = Path("/")):
        self.root = root.resolve()

    def path(self, absolute: str) -> Path:
        if not absolute.startswith("/"):
            raise CommissioningError("internal candidate path is not absolute")
        return self.root / absolute.lstrip("/")

    @property
    def marker_dir(self) -> Path:
        return self.path("/etc/t300/commissioning")

    def marker(self, name: str) -> Path:
        if name not in {
            HOST_MARKER,
            STORAGE_MARKER,
            PRODUCTION_MARKER,
            MAINTENANCE_MARKER,
            TRANSPORT_MARKER,
            RELEASE_MARKER,
        }:
            raise CommissioningError("unknown commissioning marker")
        return self.marker_dir / name

    def _require_live_root(self) -> None:
        if self.root != Path("/") or os.geteuid() != 0:
            raise CommissioningError("--apply requires root on the candidate itself")

    @staticmethod
    def _regular_root_file(path: Path, description: str) -> os.stat_result:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise CommissioningError("%s is unavailable" % description) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CommissioningError("%s is not one regular file" % description)
        if info.st_nlink != 1:
            raise CommissioningError("%s must not have another hard link" % description)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise CommissioningError("%s is not root-owned and read-only" % description)
        return info

    def candidate_identity(self, strict_owner: bool = True) -> dict[str, Any]:
        candidate_path = self.path("/opt/t300/candidate.manifest.json")
        stage_path = self.path("/opt/t300/stage.manifest.json")
        if strict_owner:
            self._regular_root_file(candidate_path, "candidate manifest")
            self._regular_root_file(stage_path, "stage manifest")
        candidate = _read_json(candidate_path)
        if (
            candidate.get("schema_version") != 1
            or candidate.get("release_ready") is not False
            or candidate.get("production_enabled") is not False
            or candidate.get("host_validated") is not False
        ):
            raise CommissioningError("candidate manifest is not an uncommissioned image")
        stage_digest = candidate.get("stage_manifest_sha256")
        if not isinstance(stage_digest, str) or sha256_file(stage_path) != stage_digest:
            raise CommissioningError("candidate and staged manifest identities differ")
        return {
            "candidate": candidate,
            "candidate_sha256": sha256_file(candidate_path),
            "stage_sha256": stage_digest,
        }

    def configuration_digest(self, strict_owner: bool = True) -> str:
        return inventory_configuration_tree(
            self.path("/etc/t300"), strict_owner
        )["config_sha256"]

    def configuration_inventory(self, strict_owner: bool = True) -> dict[str, Any]:
        value = inventory_configuration_tree(self.path("/etc/t300"), strict_owner)
        identity = self.candidate_identity(strict_owner)
        value.update(
            {
                "kind": "t300-config-inventory",
                "candidate_sha256": identity["candidate_sha256"],
                "stage_sha256": identity["stage_sha256"],
            }
        )
        return value

    def _run_systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=90,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommissioningError("systemd control failed") from exc
        if check and result.returncode:
            raise CommissioningError(
                "systemctl %s failed: %s"
                % (arguments[0], result.stderr.strip() or "no detail")
            )
        return result

    def _unit_active(self, unit: str) -> bool:
        return self._run_systemctl("is-active", "--quiet", unit, check=False).returncode == 0

    def _require_units_inactive(self, units: Iterable[str]) -> None:
        active = [unit for unit in units if self._unit_active(unit)]
        if active:
            raise CommissioningError("services must already be stopped: %s" % ", ".join(active))

    def _require_transport_closed(self) -> None:
        self._require_units_inactive((SSH_UNIT,))
        if self.marker(TRANSPORT_MARKER).exists():
            raise CommissioningError("restricted transfer gate must be closed")

    def _require_storage_ready(self) -> dict[str, Any]:
        marker = self._read_marker(STORAGE_MARKER, "enabled")
        uuid = marker.get("data_usb_uuid")
        if not isinstance(uuid, str) or FAT_UUID_RE.fullmatch(uuid) is None:
            raise CommissioningError("storage marker lacks the exact data USB UUID")
        if not self._data_usb_connected(uuid):
            raise CommissioningError("the commissioned data USB identity is unavailable")
        inactive = [unit for unit in STORAGE_UNITS if not self._unit_active(unit)]
        if inactive:
            raise CommissioningError(
                "commissioned storage mounts are inactive: %s" % ", ".join(inactive)
            )
        return marker

    def _data_usb_connected(self, uuid: str) -> bool:
        device = self.path("/dev/disk/by-uuid/%s" % uuid)
        try:
            target = device.resolve(strict=True)
        except OSError:
            return False
        return device.is_symlink() and target.is_block_device()

    def _prepare_data_gcode_directory(self) -> None:
        mount = self.path("/mnt/t300-data")
        for relative in (
            "gcodes",
            "timelapse",
            "timelapse/frames",
            "timelapse/videos",
            "timelapse/retained",
        ):
            path = mount / relative
            try:
                path.mkdir(mode=0o750)
            except FileExistsError:
                pass
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise CommissioningError(
                    "data USB directory is unavailable: %s" % relative
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CommissioningError(
                    "data USB path is not one real directory: %s" % relative
                )

    def _read_marker(self, name: str, expected_state: str | None = None) -> dict[str, Any]:
        path = self.marker(name)
        self._regular_root_file(path, "%s marker" % name)
        value = _read_json(path)
        if value.get("schema_version") != 1 or value.get("gate") != name:
            raise CommissioningError("%s marker is malformed" % name)
        if expected_state is not None and value.get("state") != expected_state:
            raise CommissioningError("%s marker is not %s" % (name, expected_state))
        identity = self.candidate_identity()
        if value.get("candidate_sha256") != identity["candidate_sha256"]:
            raise CommissioningError("%s marker belongs to another candidate" % name)
        if value.get("config_sha256") != self.configuration_digest():
            raise CommissioningError("configuration changed after %s was created" % name)
        return value

    def _marker_value(self, name: str, state: str, **extra: Any) -> dict[str, Any]:
        identity = self.candidate_identity()
        value = {
            "schema_version": 1,
            "gate": name,
            "state": state,
            "created_at": _utc_now(),
            "candidate_sha256": identity["candidate_sha256"],
            "stage_sha256": identity["stage_sha256"],
            "config_sha256": self.configuration_digest(),
        }
        value.update(extra)
        return value

    @staticmethod
    def _require_confirmation(apply: bool, confirmation: str | None, expected: str) -> None:
        if apply and confirmation != expected:
            raise CommissioningError("typed confirmation must be exactly: %s" % expected)

    def _evidence(self, path: Path, required: Iterable[str]) -> tuple[dict[str, Any], str]:
        value, digest = _read_evidence_snapshot(path)
        identity = self.candidate_identity(strict_owner=False)
        checks = value.get("checks")
        if (
            value.get("schema_version") != 1
            or value.get("candidate_sha256") != identity["candidate_sha256"]
            or value.get("config_sha256")
            != self.configuration_digest(strict_owner=False)
            or not isinstance(checks, dict)
        ):
            raise CommissioningError(
                "evidence does not identify this candidate and configuration"
            )
        missing = [name for name in required if checks.get(name) is not True]
        if missing:
            raise CommissioningError("evidence checks are not confirmed: %s" % ", ".join(missing))
        return value, digest

    def _recent_config_evidence(
        self, path: Path, required: Iterable[str]
    ) -> tuple[dict[str, Any], str]:
        value, digest = self._evidence(path, required)
        if value.get("config_sha256") != self.configuration_digest(strict_owner=False):
            raise CommissioningError("evidence belongs to another configuration")
        try:
            captured = dt.datetime.fromisoformat(value["captured_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommissioningError("evidence timestamp is invalid") from exc
        if captured.tzinfo is None:
            raise CommissioningError("evidence timestamp lacks a timezone")
        age = dt.datetime.now(dt.timezone.utc) - captured.astimezone(dt.timezone.utc)
        if age < dt.timedelta(minutes=-1) or age > dt.timedelta(minutes=10):
            raise CommissioningError("evidence is not recent")
        return value, digest

    def _release_deployment(self) -> dict[str, Any]:
        path = self.path(CONFIG_DEPLOY_JOURNAL)
        self._regular_root_file(path, "configuration deployment journal")
        value = _read_json(path)
        identity = self.candidate_identity()
        current_config = self.configuration_digest()
        if (
            value.get("schema_version") != 1
            or value.get("status") != "complete-revalidation-required"
            or value.get("candidate_sha256") != identity["candidate_sha256"]
            or value.get("new_config_sha256") != current_config
            or value.get("calibration_ready") is not True
            or value.get("commissioning_lock") is not False
            or not isinstance(value.get("bundle_sha256"), str)
            or SHA256_RE.fullmatch(value["bundle_sha256"]) is None
        ):
            raise CommissioningError(
                "current config is not a completed calibrated deployment"
            )
        return value

    def status(self) -> dict[str, Any]:
        identity = self.candidate_identity(strict_owner=False)
        markers: dict[str, Any] = {}
        for name in (
            HOST_MARKER,
            STORAGE_MARKER,
            PRODUCTION_MARKER,
            MAINTENANCE_MARKER,
            TRANSPORT_MARKER,
            RELEASE_MARKER,
        ):
            path = self.marker(name)
            if path.is_file() and not path.is_symlink():
                try:
                    value = _read_json(path)
                    markers[name] = value.get("state", "malformed")
                except CommissioningError:
                    markers[name] = "malformed"
            else:
                markers[name] = "absent"
        units = {
            unit: ("active" if self._unit_active(unit) else "inactive")
            for unit in (*HOST_UNITS, *NORMAL_PRINTER_UNITS, SSH_UNIT)
        }
        return {
            "candidate_sha256": identity["candidate_sha256"],
            "stage_sha256": identity["stage_sha256"],
            "config_sha256": self.configuration_digest(strict_owner=False),
            "markers": markers,
            "units": units,
        }

    def host_test_start(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        identity = self.candidate_identity(strict_owner=apply)
        expected = "START HOST-ONLY TEST %s" % identity["candidate_sha256"][:12]
        self._require_confirmation(apply, confirmation, expected)
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        if self.marker(HOST_MARKER).exists() or self.marker(HOST_MARKER).is_symlink():
            raise CommissioningError("host validation gate already exists")
        if self.marker(PRODUCTION_MARKER).exists() or self.marker(MAINTENANCE_MARKER).exists():
            raise CommissioningError("printer-control or maintenance gate already exists")
        result = {"action": "host-test-start", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(HOST_MARKER, "testing")
        _write_json_atomic(self.marker(HOST_MARKER), marker)
        try:
            self._run_systemctl("start", *HOST_UNITS)
            failed = [unit for unit in HOST_UNITS if not self._unit_active(unit)]
            if failed:
                raise CommissioningError("host-only services failed: %s" % ", ".join(failed))
            active_control = [
                unit for unit in NORMAL_PRINTER_UNITS if self._unit_active(unit)
            ]
            if active_control:
                raise CommissioningError(
                    "host-only test activated printer-control services: %s"
                    % ", ".join(active_control)
                )
        except BaseException:
            self._run_systemctl(
                "stop",
                *reversed(HOST_UNITS),
                *reversed(NORMAL_PRINTER_UNITS),
                check=False,
            )
            self.marker(HOST_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "testing"
        return result

    def host_test_accept(
        self, evidence_path: Path, apply: bool, confirmation: str | None
    ) -> dict[str, Any]:
        marker = self._read_marker(HOST_MARKER, "testing")
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        _evidence, evidence_digest = self._evidence(evidence_path, REQUIRED_HOST_EVIDENCE)
        failed = [unit for unit in HOST_UNITS if not self._unit_active(unit)]
        if failed:
            raise CommissioningError("host-only services are not healthy: %s" % ", ".join(failed))
        expected = "ACCEPT HOST VALIDATION %s" % evidence_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "host-test-accept", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        marker.update(
            {
                "state": "validated",
                "validated_at": _utc_now(),
                "evidence_sha256": evidence_digest,
            }
        )
        _write_json_atomic(self.marker(HOST_MARKER), marker)
        try:
            self._run_systemctl("enable", *HOST_UNITS)
        except BaseException:
            self._run_systemctl("disable", "--now", *HOST_UNITS, check=False)
            self.marker(HOST_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "validated"
        return result

    def host_test_abort(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        marker = self._read_marker(HOST_MARKER)
        if marker.get("state") != "testing":
            raise CommissioningError("only an unfinished host test may be aborted")
        expected = "ABORT HOST-ONLY TEST"
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "host-test-abort", "expected_confirmation": expected, "apply": apply}
        if apply:
            self._require_live_root()
            self._run_systemctl("disable", "--now", *reversed(HOST_UNITS), check=False)
            self.marker(HOST_MARKER).unlink(missing_ok=True)
            result["state"] = "absent"
        return result

    def storage_enable(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_transport_closed()
        if self.marker(STORAGE_MARKER).exists():
            raise CommissioningError("storage is already commissioned")
        self._require_units_inactive(STORAGE_UNITS)
        stage = _read_json(self.path("/opt/t300/stage.manifest.json"))
        uuid = stage.get("metadata", {}).get("data_usb_uuid")
        if not isinstance(uuid, str) or FAT_UUID_RE.fullmatch(uuid) is None:
            raise CommissioningError("staged data USB UUID is missing or invalid")
        expected = "MOUNT DATA USB %s" % uuid
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "storage-enable", "uuid": uuid, "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        device = self.path("/dev/disk/by-uuid/%s" % uuid)
        if not self._data_usb_connected(uuid):
            raise CommissioningError("the exact staged data USB is not connected")
        probe = subprocess.run(
            ["/usr/sbin/blkid", "-o", "export", str(device)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        fields = dict(
            line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line
        )
        if probe.returncode or fields.get("UUID") != uuid or fields.get("TYPE") != "vfat":
            raise CommissioningError("connected data USB identity or filesystem differs")
        marker = self._marker_value(STORAGE_MARKER, "enabled", data_usb_uuid=uuid)
        _write_json_atomic(self.marker(STORAGE_MARKER), marker)
        try:
            self._run_systemctl("enable", DATA_MOUNT_UNIT)
            self._run_systemctl("start", DATA_MOUNT_UNIT)
            if not self._unit_active(DATA_MOUNT_UNIT):
                raise CommissioningError("data USB mount unit did not become active")
            self._prepare_data_gcode_directory()
            self._run_systemctl("enable", GCODE_BIND_MOUNT_UNIT)
            self._run_systemctl("start", GCODE_BIND_MOUNT_UNIT)
            if not self._unit_active(GCODE_BIND_MOUNT_UNIT):
                raise CommissioningError("G-code bind mount did not become active")
        except BaseException:
            self._run_systemctl("disable", "--now", GCODE_BIND_MOUNT_UNIT, check=False)
            self._run_systemctl("disable", "--now", DATA_MOUNT_UNIT, check=False)
            self.marker(STORAGE_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "enabled"
        return result

    def production_arm(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        self._read_marker(HOST_MARKER, "validated")
        self._require_storage_ready()
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_transport_closed()
        if self.marker(MAINTENANCE_MARKER).exists():
            raise CommissioningError("maintenance gate exists")
        if self.marker(PRODUCTION_MARKER).exists() or self.marker(PRODUCTION_MARKER).is_symlink():
            raise CommissioningError("production gate already exists")
        safety = self.path("/etc/t300/klipper/safety.cfg").read_text(encoding="utf-8")
        if re.search(r"^commissioning_lock:\s*True\s*$", safety, re.MULTILINE) is None:
            raise CommissioningError("initial production start requires the commissioning lock")
        config_digest = self.configuration_digest()
        expected = "ARM LOCKED PRODUCTION %s" % config_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "production-arm", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(PRODUCTION_MARKER, "armed", commissioning_lock=True)
        _write_json_atomic(self.marker(PRODUCTION_MARKER), marker)
        try:
            self._run_systemctl("enable", *PRODUCTION_UNITS)
        except BaseException:
            self._run_systemctl("disable", "--now", *PRODUCTION_UNITS, check=False)
            self.marker(PRODUCTION_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "armed"
        return result

    def release_arm(
        self, evidence_path: Path, apply: bool, confirmation: str | None
    ) -> dict[str, Any]:
        self._read_marker(HOST_MARKER, "validated")
        self._require_storage_ready()
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_transport_closed()
        if self.marker(MAINTENANCE_MARKER).exists():
            raise CommissioningError("maintenance gate exists")
        if self.marker(PRODUCTION_MARKER).exists() or self.marker(PRODUCTION_MARKER).is_symlink():
            raise CommissioningError("production gate already exists")
        safety = self.path("/etc/t300/klipper/safety.cfg").read_text(
            encoding="utf-8"
        )
        if (
            re.search(r"^commissioning_lock:\s*False\s*$", safety, re.MULTILINE)
            is None
            or re.search(r"^commissioning_lock:\s*True\s*$", safety, re.MULTILINE)
            is not None
        ):
            raise CommissioningError(
                "release arming requires exactly one disabled commissioning lock"
            )
        deployment = self._release_deployment()
        _evidence, evidence_digest = self._recent_config_evidence(
            evidence_path, REQUIRED_RELEASE_EVIDENCE
        )
        expected = "ARM T300 RELEASE CANDIDATE %s" % evidence_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {
            "action": "release-arm",
            "expected_confirmation": expected,
            "apply": apply,
        }
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(
            PRODUCTION_MARKER,
            "armed",
            commissioning_lock=False,
            release_evidence_sha256=evidence_digest,
            configuration_deployment_sha256=deployment["bundle_sha256"],
        )
        _write_json_atomic(self.marker(PRODUCTION_MARKER), marker)
        try:
            self._run_systemctl("enable", *PRODUCTION_UNITS)
        except BaseException:
            self._run_systemctl("disable", "--now", *PRODUCTION_UNITS, check=False)
            self.marker(PRODUCTION_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "armed"
        return result

    def production_start(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        marker = self._read_marker(PRODUCTION_MARKER, "armed")
        self._require_units_inactive(("klipper-maintenance.service",))
        self._require_storage_ready()
        marker_digest = sha256_file(self.marker(PRODUCTION_MARKER))
        locked = marker.get("commissioning_lock") is True
        if not locked and marker.get("commissioning_lock") is not False:
            raise CommissioningError("production marker lacks its lock state")
        label = "LOCKED KLIPPER" if locked else "T300 RELEASE CANDIDATE"
        expected = "START %s %s" % (label, marker_digest[:12])
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "production-start", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        armed_marker = dict(marker)
        marker.update({"state": "started", "started_at": _utc_now()})
        _write_json_atomic(self.marker(PRODUCTION_MARKER), marker)
        try:
            self._run_systemctl("start", *PRODUCTION_UNITS)
            failed = [unit for unit in PRODUCTION_UNITS if not self._unit_active(unit)]
            if failed:
                raise CommissioningError(
                    "production services failed: %s" % ", ".join(failed)
                )
        except BaseException:
            self._run_systemctl("stop", *reversed(PRODUCTION_UNITS), check=False)
            try:
                _write_json_atomic(self.marker(PRODUCTION_MARKER), armed_marker)
            except BaseException:
                self.marker(PRODUCTION_MARKER).unlink(missing_ok=True)
                raise CommissioningError(
                    "production start failed and its authorization was revoked"
                )
            raise
        result["state"] = "started"
        return result

    def release_accept(
        self, evidence_path: Path, apply: bool, confirmation: str | None
    ) -> dict[str, Any]:
        self._read_marker(HOST_MARKER, "validated")
        self._require_storage_ready()
        self._require_transport_closed()
        production = self._read_marker(PRODUCTION_MARKER, "started")
        if production.get("commissioning_lock") is not False:
            raise CommissioningError(
                "only an unlocked, tested release candidate may be accepted"
            )
        if self.marker(MAINTENANCE_MARKER).exists():
            raise CommissioningError("maintenance gate exists")
        if self.marker(RELEASE_MARKER).exists() or self.marker(RELEASE_MARKER).is_symlink():
            raise CommissioningError("final release validation already exists")
        inactive = [
            unit
            for unit in (*HOST_UNITS, *PRODUCTION_UNITS)
            if not self._unit_active(unit)
        ]
        if inactive:
            raise CommissioningError(
                "release-candidate services are not healthy: %s"
                % ", ".join(inactive)
            )
        _evidence, evidence_digest = self._evidence(
            evidence_path, REQUIRED_FINAL_RELEASE_EVIDENCE
        )
        expected = "ACCEPT T300 MAINLINE RELEASE %s" % evidence_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {
            "action": "release-accept",
            "expected_confirmation": expected,
            "apply": apply,
        }
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(
            RELEASE_MARKER,
            "validated",
            evidence_sha256=evidence_digest,
            production_marker_sha256=sha256_file(self.marker(PRODUCTION_MARKER)),
        )
        _write_json_atomic(self.marker(RELEASE_MARKER), marker)
        result["state"] = "validated"
        return result

    def maintenance_arm(
        self, evidence_path: Path, apply: bool, confirmation: str | None
    ) -> dict[str, Any]:
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_transport_closed()
        _evidence, evidence_digest = self._recent_config_evidence(
            evidence_path, REQUIRED_MAINTENANCE_EVIDENCE
        )
        expected = "ARM LOCAL MAINTENANCE %s" % evidence_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "maintenance-arm", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(
            MAINTENANCE_MARKER,
            "armed-once",
            evidence_sha256=evidence_digest,
        )
        _write_json_atomic(self.marker(MAINTENANCE_MARKER), marker)
        result["state"] = "armed-once"
        return result

    def maintenance_start(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_transport_closed()
        self._read_marker(MAINTENANCE_MARKER, "armed-once")
        marker_digest = sha256_file(self.marker(MAINTENANCE_MARKER))
        expected = "START LOCAL MAINTENANCE %s" % marker_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {"action": "maintenance-start", "expected_confirmation": expected, "apply": apply}
        if not apply:
            return result
        self._require_live_root()
        try:
            self._run_systemctl("start", "klipper-maintenance.service")
            if not self._unit_active("klipper-maintenance.service"):
                raise CommissioningError("maintenance Klipper did not become active")
            if self.marker(MAINTENANCE_MARKER).exists():
                raise CommissioningError(
                    "maintenance service did not consume its one-use gate"
                )
        except BaseException:
            self._run_systemctl(
                "stop", "klipper-maintenance.service", check=False
            )
            raise
        result["state"] = "started"
        return result

    def consume_maintenance_marker(self) -> dict[str, Any]:
        self._require_live_root()
        self._require_units_inactive(PRODUCTION_UNITS)
        self._read_marker(MAINTENANCE_MARKER, "armed-once")
        self.marker(MAINTENANCE_MARKER).unlink()
        return {"action": "consume-maintenance-marker", "state": "consumed"}

    def transport_enable(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        self._require_units_inactive(NORMAL_PRINTER_UNITS)
        self._require_units_inactive((SSH_UNIT,))
        if self.marker(TRANSPORT_MARKER).exists():
            raise CommissioningError("restricted transfer gate already exists")
        identity = self.candidate_identity(strict_owner=apply)
        stage = _read_json(self.path("/opt/t300/stage.manifest.json"))
        if stage.get("metadata", {}).get("deploy_transport_present") is not True:
            raise CommissioningError("candidate has no staged deployment public key")
        fingerprint = identity["candidate"].get("transport_host_key_fingerprint")
        if not isinstance(fingerprint, str) or re.fullmatch(
            r"SHA256:[A-Za-z0-9+/]{43}", fingerprint
        ) is None:
            raise CommissioningError("candidate SSH host fingerprint is unavailable")
        expected = "ENABLE RESTRICTED TRANSFER %s" % fingerprint
        self._require_confirmation(apply, confirmation, expected)
        result = {
            "action": "transport-enable",
            "apply": apply,
            "host_key_fingerprint": fingerprint,
            "expected_confirmation": expected,
        }
        if not apply:
            return result
        self._require_live_root()
        marker = self._marker_value(
            TRANSPORT_MARKER,
            "enabled",
            host_key_fingerprint=fingerprint,
            temporary=True,
        )
        _write_json_atomic(self.marker(TRANSPORT_MARKER), marker)
        try:
            self._run_systemctl("start", SSH_UNIT)
            if not self._unit_active(SSH_UNIT):
                raise CommissioningError("restricted SSH transport did not start")
        except BaseException:
            self._run_systemctl("stop", SSH_UNIT, check=False)
            self.marker(TRANSPORT_MARKER).unlink(missing_ok=True)
            raise
        result["state"] = "enabled-temporarily"
        return result

    def transport_disable(self, apply: bool, confirmation: str | None) -> dict[str, Any]:
        self._read_marker(TRANSPORT_MARKER, "enabled")
        marker_digest = sha256_file(self.marker(TRANSPORT_MARKER))
        expected = "DISABLE RESTRICTED TRANSFER %s" % marker_digest[:12]
        self._require_confirmation(apply, confirmation, expected)
        result = {
            "action": "transport-disable",
            "apply": apply,
            "expected_confirmation": expected,
        }
        if not apply:
            return result
        self._require_live_root()
        self._run_systemctl("stop", SSH_UNIT, check=False)
        if self._unit_active(SSH_UNIT):
            raise CommissioningError("restricted SSH transport did not stop")
        self.marker(TRANSPORT_MARKER).unlink()
        result["state"] = "disabled"
        return result

    def check_service_gate(self, gate: str) -> dict[str, Any]:
        self._require_live_root()
        if gate == "host":
            marker = self._read_marker(HOST_MARKER)
            if marker.get("state") not in {"testing", "validated"}:
                raise CommissioningError("host service gate is closed")
            if marker.get("state") == "testing":
                self._require_units_inactive(NORMAL_PRINTER_UNITS)
        elif gate == "production":
            self._require_transport_closed()
            marker = self._read_marker(PRODUCTION_MARKER)
            if marker.get("state") != "started":
                raise CommissioningError("production service gate is closed")
            self._read_marker(HOST_MARKER, "validated")
            self._require_storage_ready()
        elif gate == "transport":
            self._require_units_inactive(NORMAL_PRINTER_UNITS)
            marker = self._read_marker(TRANSPORT_MARKER, "enabled")
        else:
            raise CommissioningError("unknown service gate")
        return {"action": "check-service-gate", "gate": gate, "state": marker["state"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("inventory")
    for name in (
        "host-test-start",
        "host-test-abort",
        "storage-enable",
        "production-arm",
        "production-start",
        "maintenance-start",
        "transport-enable",
        "transport-disable",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--apply", action="store_true")
        command.add_argument("--confirm")
    for name in (
        "host-test-accept",
        "maintenance-arm",
        "release-arm",
        "release-accept",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--evidence", type=Path, required=True)
        command.add_argument("--apply", action="store_true")
        command.add_argument("--confirm")
    subparsers.add_parser("consume-maintenance-marker", help=argparse.SUPPRESS)
    service_gate = subparsers.add_parser("check-service-gate", help=argparse.SUPPRESS)
    service_gate.add_argument("gate", choices=("host", "production", "transport"))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    controller = CandidateController()
    try:
        if args.command == "status":
            result = controller.status()
        elif args.command == "inventory":
            result = controller.configuration_inventory()
        elif args.command == "host-test-start":
            result = controller.host_test_start(args.apply, args.confirm)
        elif args.command == "host-test-accept":
            result = controller.host_test_accept(args.evidence, args.apply, args.confirm)
        elif args.command == "host-test-abort":
            result = controller.host_test_abort(args.apply, args.confirm)
        elif args.command == "storage-enable":
            result = controller.storage_enable(args.apply, args.confirm)
        elif args.command == "production-arm":
            result = controller.production_arm(args.apply, args.confirm)
        elif args.command == "release-arm":
            result = controller.release_arm(args.evidence, args.apply, args.confirm)
        elif args.command == "release-accept":
            result = controller.release_accept(args.evidence, args.apply, args.confirm)
        elif args.command == "production-start":
            result = controller.production_start(args.apply, args.confirm)
        elif args.command == "maintenance-arm":
            result = controller.maintenance_arm(args.evidence, args.apply, args.confirm)
        elif args.command == "maintenance-start":
            result = controller.maintenance_start(args.apply, args.confirm)
        elif args.command == "transport-enable":
            result = controller.transport_enable(args.apply, args.confirm)
        elif args.command == "transport-disable":
            result = controller.transport_disable(args.apply, args.confirm)
        elif args.command == "consume-maintenance-marker":
            result = controller.consume_maintenance_marker()
        else:
            result = controller.check_service_gate(args.gate)
    except (OSError, ValueError, CommissioningError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

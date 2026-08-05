"""Generate evidence-backed validation reports for T300 configuration bundles."""

from __future__ import annotations

import configparser
import datetime as dt
import hashlib
import ipaddress
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
from .provision import ProvisionError, verify_stage


REPORT_GENERATOR = "t300-validation-v1"
CHECK_NAMES = (
    "stage_verified",
    "unit_tests_passed",
    "vendor_v012_harness_passed",
    "klipper_v013_harness_passed",
    "gcode_policy_tests_passed",
    "klipper_lifecycle_reviewed",
    "systemd_units_reviewed",
    "host_network_boundary_reviewed",
    "operator_ui_reviewed",
    "secret_scan_passed",
)
EXPECTED_SERVICES = (
    "klipper.service",
    "klipper-maintenance.service",
    "moonraker.service",
    "t300-admission.service",
    "crowsnest.service",
    "mainsail.service",
    "t300-xorg.service",
    "klipperscreen.service",
    "t300-host-mcu.service",
)
EXPECTED_MOUNTS = (
    r"mnt-t300\x2ddata.mount",
    r"var-lib-t300-moonraker\x2ddata-gcodes.mount",
)
HOST_GATE_SERVICES = {
    "crowsnest.service",
    "mainsail.service",
    "t300-xorg.service",
    "klipperscreen.service",
}
PRODUCTION_GATE_SERVICES = {
    "klipper.service",
    "moonraker.service",
    "t300-admission.service",
}
COMMON_HARDENING = (
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
)
SERVICE_RESOURCE_REQUIREMENTS = {
    "klipper.service": (
        "MemoryMax=512M",
        "CPUWeight=1000",
        "IOWeight=1000",
        "LimitFSIZE=33554432",
        "OOMScoreAdjust=-500",
    ),
    "klipper-maintenance.service": (
        "MemoryMax=512M",
        "CPUWeight=1000",
        "IOWeight=1000",
        "LimitFSIZE=33554432",
        "OOMScoreAdjust=-500",
    ),
    "moonraker.service": (
        "MemoryMax=768M",
        "CPUWeight=20",
        "IOWeight=20",
        "OOMScoreAdjust=400",
    ),
    "t300-admission.service": (
        "MemoryMax=256M",
        "CPUQuota=50%",
        "CPUWeight=10",
        "IOWeight=10",
        "OOMScoreAdjust=500",
    ),
    "crowsnest.service": (
        "MemoryMax=256M",
        "CPUWeight=10",
        "IOWeight=10",
        "LimitFSIZE=16777216",
        "OOMScoreAdjust=600",
    ),
    "mainsail.service": (
        "MemoryMax=128M",
        "CPUWeight=10",
        "IOWeight=10",
        "OOMScoreAdjust=500",
    ),
    "t300-xorg.service": (
        "MemoryMax=256M",
        "CPUWeight=20",
        "IOWeight=20",
        "LimitFSIZE=16777216",
        "OOMScoreAdjust=400",
    ),
    "klipperscreen.service": (
        "MemoryMax=384M",
        "CPUWeight=50",
        "IOWeight=50",
        "LimitFSIZE=16777216",
        "OOMScoreAdjust=300",
    ),
    "t300-host-mcu.service": (
        "MemoryMax=64M",
        "CPUWeight=900",
        "IOWeight=500",
        "LimitMEMLOCK=infinity",
        "OOMScoreAdjust=-400",
    ),
}
PRIVATE_BASENAMES = {
    "macro_z_tilt_via_knob.cfg",
    "id_rsa",
    "id_ed25519",
    ".env",
}
IGNORED_REPO_PARTS = {
    ".git",
    ".cache",
    "__pycache__",
    "t300-backups",
}
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
)


class ValidationError(RuntimeError):
    pass


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _command_evidence(
    command: list[str], cwd: Path, timeout: int
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
        output = result.stdout
        returncode = result.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"") + (exc.stderr or b"")
        returncode = None
        timed_out = True
    except OSError as exc:
        output = str(exc).encode("utf-8", "replace")
        returncode = None
        timed_out = False
    if len(output) > MAX_CAPTURE_BYTES:
        retained = output[-MAX_CAPTURE_BYTES:]
        truncated = True
    else:
        retained = output
        truncated = False
    return {
        "passed": returncode == 0 and not timed_out,
        "argv": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_bytes": len(output),
        "output_tail": retained.decode("utf-8", "replace")[-8000:],
        "output_tail_truncated": truncated,
    }


def review_systemd_units(stage_root: Path) -> dict[str, Any]:
    unit_root = stage_root / "etc/systemd/system"
    failures: list[str] = []
    reviewed: list[str] = []
    for name in EXPECTED_SERVICES:
        path = unit_root / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append("missing or unreadable unit: %s" % name)
            continue
        reviewed.append(name)
        for directive in COMMON_HARDENING:
            if directive not in text:
                failures.append("%s lacks %s" % (name, directive))
        for directive in SERVICE_RESOURCE_REQUIREMENTS[name]:
            if directive not in text:
                failures.append("%s lacks resource boundary %s" % (name, directive))
        if "UMask=" not in text:
            failures.append("%s lacks an explicit UMask" % name)
        if not re.search(r"(?m)^ReadOnlyPaths=.*?/etc/t300(?:\s|$)", text):
            failures.append("%s does not mount /etc/t300 read-only" % name)
        if re.search(r"(?m)^Exec(?:Start|StartPre|Condition)=.*(?:/bin/(?:ba)?sh|sudo)", text):
            failures.append("%s invokes a shell or sudo" % name)
        if re.search(r"(?m)^Restart=always\s*$", text):
            failures.append("%s restarts unconditionally" % name)
        if name in HOST_GATE_SERVICES and "check-service-gate host" not in text:
            failures.append("%s lacks the host gate" % name)
        if name in HOST_GATE_SERVICES and re.search(
            r"(?m)^(?:Wants|Requires|BindsTo)=.*(?:klipper|moonraker|t300-admission)",
            text,
        ):
            failures.append("%s pulls in a printer-control service" % name)
        if name in PRODUCTION_GATE_SERVICES and "check-service-gate production" not in text:
            failures.append("%s lacks the production gate" % name)
        if name == "t300-admission.service":
            for required in (
                "--startup-preflight",
                "Group=t300-gcode",
                "SupplementaryGroups=t300-policy",
                "MemoryMax=256M",
                "CPUQuota=50%",
                "TasksMax=32",
                "LimitNOFILE=256",
                "IOSchedulingClass=idle",
            ):
                if required not in text:
                    failures.append("admission service lacks %s" % required)
        if name == "klipper-maintenance.service":
            for required in (
                "PrivateNetwork=yes",
                "Restart=no",
                "consume-maintenance-marker",
                "SupplementaryGroups=t300-gcode dialout tty",
                "DeviceAllow=char-pts rw",
                "ConditionPathExists=/etc/t300/commissioning/maintenance-enabled",
            ):
                if required not in text:
                    failures.append("maintenance unit lacks %s" % required)
            if re.search(r"(?m)^\[Install\]\s*$", text):
                failures.append("maintenance unit must not be enableable")
        if name == "t300-host-mcu.service":
            for required in (
                "PartOf=klipper-maintenance.service",
                "Group=t300-comms",
                "SupplementaryGroups=t300-host-mcu",
                "DevicePolicy=closed",
                "DeviceAllow=/dev/spidev0.0 rw",
                "DeviceAllow=/dev/ptmx rw",
                "DeviceAllow=char-pts rw",
                "CapabilityBoundingSet=CAP_IPC_LOCK CAP_SYS_NICE",
                "PrivateNetwork=yes",
            ):
                if required not in text:
                    failures.append("host MCU unit lacks %s" % required)
            if re.search(r"(?m)^\[Install\]\s*$", text):
                failures.append("host MCU unit must not be enableable")
        if name == "t300-xorg.service":
            for required in (
                "-nolisten tcp",
                "RestrictAddressFamilies=AF_UNIX",
            ):
                if required not in text:
                    failures.append("Xorg unit lacks local-only boundary %s" % required)
            if "PrivateNetwork=yes" in text:
                failures.append(
                    "Xorg must share the host Unix-socket namespace with KlipperScreen"
                )
    for name in EXPECTED_MOUNTS:
        path = unit_root / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append("missing or unreadable mount unit: %s" % name)
            continue
        reviewed.append(name)
        if "ConditionPathExists=/etc/t300/commissioning/storage-enabled" not in text:
            failures.append("%s lacks the storage gate" % name)
        if "nosuid" not in text or "nodev" not in text or "noexec" not in text:
            failures.append("%s lacks removable-storage mount hardening" % name)
    for name in ("klipper.service", "klipper-maintenance.service"):
        dropin = unit_root / (name + ".d/10-mcu-device.conf")
        try:
            text = dropin.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append("%s lacks its rendered MCU device boundary" % name)
            continue
        if "DevicePolicy=closed" not in text:
            failures.append("%s MCU boundary is not closed" % name)
        conditions = re.findall(
            r"(?m)^(?:ConditionPathExists|DeviceAllow)=([^\s]+)(?:\s+rw)?\s*$",
            text,
        )
        serials = [value for value in conditions if value.startswith("/dev/serial/by-id/")]
        if len(serials) != 2 or len(set(serials)) != 1:
            failures.append("%s MCU boundary does not name one exact serial path" % name)
    ssh_config = stage_root / "etc/ssh/sshd_config.d/60-t300.conf"
    try:
        ssh_text = ssh_config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        failures.append("restricted SSH configuration is missing or unreadable")
    else:
        for directive in (
            "PermitRootLogin no",
            "AuthenticationMethods publickey",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "X11Forwarding no",
            "AllowAgentForwarding no",
            "AllowTcpForwarding no",
            "PermitTunnel no",
            "PermitTTY no",
            "ForceCommand /opt/t300/venvs/control/bin/python /opt/t300/control/bin/t300-transfer-receive.py",
            "DisableForwarding yes",
            "Match all",
        ):
            if directive not in ssh_text:
                failures.append("restricted SSH config lacks %s" % directive)
        if re.search(r"(?m)^AllowUsers t300-deploy@[^\s]+/\d+\s*$", ssh_text) is None:
            failures.append("restricted SSH config lacks its source CIDR")
    ssh_gate = unit_root / "ssh.service.d/60-t300-gate.conf"
    try:
        gate_text = ssh_gate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        failures.append("restricted SSH service gate is missing")
    else:
        if "ConditionPathExists=/etc/t300/commissioning/transport-enabled" not in gate_text:
            failures.append("restricted SSH service lacks its marker condition")
        if "check-service-gate transport" not in gate_text:
            failures.append("restricted SSH service lacks its validated gate")
    authorized = stage_root / "etc/t300/deploy_authorized_keys"
    try:
        authorized_text = authorized.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        failures.append("deployment authorized-key file is missing or unreadable")
    else:
        if not (
            authorized_text.startswith("# No deployment key was staged;")
            or authorized_text.startswith("restrict,no-user-rc,command=")
        ):
            failures.append("deployment authorized key lacks forced-command restrictions")
    return {
        "passed": not failures,
        "reviewed_units": reviewed,
        "failures": failures,
    }


def _config_section(text: str, name: str) -> str | None:
    match = re.search(
        r"(?ms)^\[" + re.escape(name) + r"\]\s*\n(.*?)(?=^\[|\Z)", text
    )
    return None if match is None else match.group(1)


def review_host_network_boundary(stage_root: Path) -> dict[str, Any]:
    """Prove that nginx is the candidate's only remotely reachable UI endpoint."""
    paths = {
        "moonraker": stage_root / "etc/t300/moonraker/moonraker.conf",
        "nginx": stage_root / "etc/t300/nginx/nginx.conf",
        "crowsnest": stage_root / "etc/t300/crowsnest/crowsnest.conf",
        "klipperscreen": stage_root / "etc/t300/klipperscreen/KlipperScreen.conf",
    }
    failures: list[str] = []
    content: dict[str, str] = {}
    for name, path in paths.items():
        try:
            content[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append("missing or unreadable host configuration: %s" % path)
    if failures:
        return {"passed": False, "reviewed_files": [], "failures": failures}

    if any("@" in text for text in content.values()):
        failures.append("host configuration retains an unresolved staging placeholder")

    moonraker = content["moonraker"]
    server = _config_section(moonraker, "server")
    authorization = _config_section(moonraker, "authorization")
    if server is None or re.search(r"(?m)^host:\s*127\.0\.0\.1\s*$", server) is None:
        failures.append("Moonraker is not bound exclusively to IPv4 loopback")
    if server is None or re.search(r"(?m)^port:\s*7125\s*$", server) is None:
        failures.append("Moonraker does not use the reviewed local proxy port")
    if authorization is None:
        failures.append("Moonraker authorization section is missing")
    else:
        for option, value in (("force_logins", "False"), ("enable_api_key", "False")):
            if re.search(
                r"(?m)^" + re.escape(option) + r":\s*" + value + r"\s*$",
                authorization,
            ) is None:
                failures.append("Moonraker authorization lacks %s: %s" % (option, value))
        trusted = re.search(
            r"(?m)^trusted_clients:\s*\n((?:[ \t]+[^\n]+(?:\n|\Z))*)",
            authorization,
        )
        entries = [] if trusted is None else [
            line.strip() for line in trusted.group(1).splitlines() if line.strip()
        ]
        if entries != ["127.0.0.0/8", "::1/128"]:
            failures.append("Moonraker trusts clients other than local loopback")

    nginx = content["nginx"]
    if re.search(r"(?m)^\s*listen\s+80\s+default_server;\s*$", nginx) is None:
        failures.append("nginx does not expose the reviewed HTTP gateway")
    allow_values = re.findall(r"(?m)^\s*allow\s+([^;\s]+);\s*$", nginx)
    if len(allow_values) != 2 or allow_values[0] != "127.0.0.1":
        failures.append("nginx allow-list must contain loopback and one laptop network")
    else:
        try:
            laptop_network = ipaddress.ip_network(allow_values[1], strict=True)
        except ValueError:
            failures.append("nginx laptop allow-list entry is not a canonical CIDR")
        else:
            if (
                not isinstance(laptop_network, ipaddress.IPv4Network)
                or laptop_network.prefixlen < 24
                or not laptop_network.is_private
                or laptop_network.is_loopback
            ):
                failures.append("nginx laptop allow-list is not a narrow private IPv4 network")
    if len(re.findall(r"(?m)^\s*deny\s+all;\s*$", nginx)) != 1:
        failures.append("nginx does not default-deny clients outside the laptop network")
    if "satisfy any" in nginx.lower():
        failures.append("nginx can bypass the source-network allow-list")
    for endpoint in (
        "proxy_pass http://127.0.0.1:7125/websocket;",
        "proxy_pass http://127.0.0.1:7125;",
        "proxy_pass http://127.0.0.1:8080/;",
    ):
        if endpoint not in nginx:
            failures.append("nginx lacks loopback-only proxy endpoint: %s" % endpoint)

    crowsnest = content["crowsnest"]
    if re.search(r"(?mi)^no_proxy:\s*false\s*$", crowsnest) is None:
        failures.append("Crowsnest is not constrained to proxy mode")
    if re.search(r"(?mi)^port:\s*8080\s*$", crowsnest) is None:
        failures.append("Crowsnest does not use the reviewed loopback camera port")
    if re.search(r"(?mi)^custom_flags:.*--host\b", crowsnest):
        failures.append("Crowsnest custom flags override its loopback binding")

    screen = content["klipperscreen"]
    if re.search(r"(?mi)^moonraker_host:\s*127\.0\.0\.1\s*$", screen) is None:
        failures.append("KlipperScreen does not use local-only Moonraker")
    if re.search(r"(?mi)^moonraker_port:\s*7125\s*$", screen) is None:
        failures.append("KlipperScreen does not use the reviewed Moonraker port")

    return {
        "passed": not failures,
        "reviewed_files": [str(path.relative_to(stage_root)) for path in paths.values()],
        "failures": failures,
    }


def _klipper_section(text: str, name: str) -> str | None:
    return _config_section(text, name)


def review_operator_ui(stage_root: Path) -> dict[str, Any]:
    """Review the small-screen and tablet defaults as one constrained workflow."""
    paths = {
        "screen": stage_root / "etc/t300/klipperscreen/KlipperScreen.conf",
        "defaults": stage_root / "etc/t300/mainsail/default.json",
        "nginx": stage_root / "etc/t300/nginx/nginx.conf",
        "mainsail_unit": stage_root / "etc/systemd/system/mainsail.service",
        "index": stage_root / "opt/t300/www/mainsail/index.html",
        "version": stage_root / "opt/t300/www/mainsail/.version",
    }
    failures: list[str] = []
    text: dict[str, str] = {}
    for name, path in paths.items():
        try:
            text[name] = path.read_text(
                encoding="ascii" if name == "version" else "utf-8"
            )
        except (OSError, UnicodeDecodeError):
            failures.append("missing or unreadable operator UI file: %s" % path)
    if failures:
        return {"passed": False, "reviewed_files": [], "failures": failures}

    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(text["screen"])
    except configparser.Error as exc:
        failures.append("KlipperScreen configuration is malformed: %s" % exc)
    else:
        if not parser.has_section("main"):
            failures.append("KlipperScreen main section is missing")
        else:
            expected_main = {
                "theme": "material-light",
                "use_default_menu": "False",
                "confirm_estop": "False",
                "job_complete_timeout": "0",
                "job_error_timeout": "0",
            }
            for option, expected in expected_main.items():
                if parser.get("main", option, fallback=None) != expected:
                    failures.append(
                        "KlipperScreen %s must remain %s" % (option, expected)
                    )
        expected_menus = {
            "menu __main",
            "menu __main print_workflow",
            "menu __main print_workflow plate_ready",
            "menu __main print_workflow select_file",
            "menu __main home_printer",
            "menu __main camera",
            "menu __main notifications",
            "menu __print",
            "menu __print camera",
            "menu __print notifications",
        }
        actual_menus = {name for name in parser.sections() if name.startswith("menu ")}
        if actual_menus != expected_menus:
            failures.append("KlipperScreen exposes an unreviewed or missing menu item")
        required_scripts = {
            "menu __main print_workflow plate_ready": "BUILD_PLATE_READY",
            "menu __main home_printer": "HOME_PRINTER",
        }
        for section, script in required_scripts.items():
            if script not in parser.get(section, "params", fallback=""):
                failures.append("KlipperScreen lacks the reviewed %s action" % script)
        if parser.get(
            "menu __main print_workflow plate_ready", "name", fallback=""
        ) != "Clean & Rearm Plate":
            failures.append(
                "KlipperScreen build-plate control is not phrased as an action"
            )
        forbidden_panels = {
            "bed_level",
            "bed_mesh",
            "console",
            "extrude",
            "input_shaper",
            "limits",
            "move",
            "network",
            "pins",
            "system",
            "temperature",
            "updater",
            "zcalibrate",
        }
        exposed_panels = {
            parser.get(section, "panel")
            for section in actual_menus
            if parser.has_option(section, "panel")
        }
        if exposed_panels & forbidden_panels:
            failures.append("KlipperScreen exposes raw production controls")

    try:
        defaults = json.loads(text["defaults"])
    except json.JSONDecodeError as exc:
        failures.append("Mainsail defaults are malformed: %s" % exc)
        defaults = {}
    if not isinstance(defaults, dict):
        failures.append("Mainsail defaults must be one JSON object")
        defaults = {}
    settings = defaults.get("uiSettings", {})
    expected_settings = {
        "confirmOnEmergencyStop": False,
        "confirmOnCancelJob": True,
        "boolHideUploadAndPrintButton": True,
        "lockSlidersOnTouchDevices": True,
        "hideSaveConfigForBedMash": True,
        "navigationStyle": "iconsAndText",
        "defaultNavigationStateSetting": "alwaysOpen",
    }
    if not isinstance(settings, dict) or any(
        settings.get(name) != value for name, value in expected_settings.items()
    ):
        failures.append("Mainsail touch, cancel, or Emergency Stop defaults changed")

    navigation = defaults.get("navigation", {}).get("entries", [])
    if not isinstance(navigation, list):
        failures.append("Mainsail navigation defaults are malformed")
    else:
        visibility = {
            item.get("title"): item.get("visible")
            for item in navigation
            if isinstance(item, dict)
        }
        expected_visibility = {
            "Dashboard": True,
            "Webcam": True,
            "Console": False,
            "Heightmap": False,
            "G-Code Files": True,
            "G-Code Viewer": False,
            "History": True,
            "Timelapse": True,
            "Machine": False,
        }
        if visibility != expected_visibility:
            failures.append("Mainsail navigation no longer matches the reviewed workflow")

    macros = defaults.get("macros", {})
    groups = macros.get("macrogroups", {}) if isinstance(macros, dict) else {}
    group = groups.get("t300-owner-actions", {}) if isinstance(groups, dict) else {}
    macro_entries = group.get("macros", []) if isinstance(group, dict) else []
    macro_names = {
        item.get("name") for item in macro_entries if isinstance(item, dict)
    }
    if (
        macros.get("mode") != "expert"
        or set(groups) != {"t300-owner-actions"}
        or macro_names
        != {"BUILD_PLATE_READY", "HOME_PRINTER", "LOAD_FILAMENT", "UNLOAD_FILAMENT"}
        or group.get("showInPrinting") is not False
    ):
        failures.append("Mainsail Owner Actions group is missing or overbroad")

    dashboard = defaults.get("dashboard", {})
    expected_panels = {
        "afc",
        "toolhead-control",
        "extruder-control",
        "macros",
        "led-effects",
        "machine-settings",
        "miniconsole",
        "miscellaneous",
        "spoolman",
        "mmu",
        "temperature",
        "webcam",
        "macrogroup_t300-owner-actions",
    }
    layouts = {
        "mobile": ("mobileLayout",),
        "tablet": ("tabletLayout1", "tabletLayout2"),
        "desktop": ("desktopLayout1", "desktopLayout2"),
        "widescreen": (
            "widescreenLayout1",
            "widescreenLayout2",
            "widescreenLayout3",
        ),
    }
    for viewport, names in layouts.items():
        entries: list[dict[str, Any]] = []
        for name in names:
            value = dashboard.get(name, []) if isinstance(dashboard, dict) else []
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
        panel_names = [item.get("name") for item in entries]
        visible = {item.get("name") for item in entries if item.get("visible") is True}
        if len(panel_names) != len(expected_panels) or set(panel_names) != expected_panels:
            failures.append("Mainsail %s dashboard has missing or duplicate panels" % viewport)
        if visible != {"webcam", "macrogroup_t300-owner-actions"}:
            failures.append("Mainsail %s dashboard exposes raw controls" % viewport)

    if text["version"].strip() != "v2.18.2":
        failures.append("compiled Mainsail version differs from the production pin")
    references = re.findall(r'(?:src|href)="(/assets/[^"]+)"', text["index"])
    if (
        not any(value.endswith(".js") for value in references)
        or not any(value.endswith(".css") for value in references)
        or "/src/" in text["index"]
    ):
        failures.append("Mainsail is not a compiled static release")
    for reference in references:
        target = stage_root / "opt/t300/www/mainsail" / reference.lstrip("/")
        if target.is_symlink() or not target.is_file():
            failures.append("Mainsail index references a missing compiled asset")
            break
    if "root /opt/t300/www/mainsail;" not in text["nginx"]:
        failures.append("nginx does not serve the immutable compiled Mainsail release")
    if "/opt/t300/www/mainsail" not in text["mainsail_unit"]:
        failures.append("Mainsail service does not declare the compiled release read-only")

    return {
        "passed": not failures,
        "reviewed_files": [str(path.relative_to(stage_root)) for path in paths.values()],
        "failures": failures,
    }


def _ordered(section: str, tokens: tuple[str, ...]) -> bool:
    position = -1
    for token in tokens:
        position = section.find(token, position + 1)
        if position < 0:
            return False
    return True


def review_klipper_lifecycle(stage_root: Path) -> dict[str, Any]:
    """Review cross-file lifecycle properties the Klipper parser cannot prove."""
    config_root = stage_root / "etc/t300/klipper"
    paths = {
        "lifecycle": config_root / "lifecycle.cfg",
        "maintenance": stage_root / "etc/t300/maintenance/printer.cfg",
        "machine": config_root / "machine.cfg",
        "printer": config_root / "printer.cfg",
        "safety": config_root / "safety.cfg",
        "timelapse": config_root / "timelapse.cfg",
        "mainsail": config_root / "vendor/mainsail/client.cfg",
        "kamp_purge": config_root / "vendor/kamp/Line_Purge.cfg",
    }
    failures: list[str] = []
    content: dict[str, str] = {}
    for name, path in paths.items():
        try:
            content[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append("missing or unreadable Klipper file: %s" % path)
    if failures:
        return {"passed": False, "reviewed_files": [], "failures": failures}

    lifecycle = content["lifecycle"]
    maintenance = content["maintenance"]
    client = content["mainsail"]
    machine = content["machine"]
    printer = content["printer"]
    safety = content["safety"]
    timelapse = content["timelapse"]
    kamp_purge = content["kamp_purge"]

    if re.search(r"(?m)^variable_cancel_retract:\s*0(?:\.0+)?\s*$", lifecycle) is None:
        failures.append("Mainsail cancel retraction is not disabled before heater shutdown")
    if re.search(
        r'(?m)^variable_user_cancel_macro:\s*"_T_CANCEL_PARK"\s*$', lifecycle
    ) is None:
        failures.append("Mainsail cancel does not delegate to the reviewed T300 hook")

    client_cancel = _klipper_section(client, "gcode_macro CANCEL_PRINT")
    if client_cancel is None:
        failures.append("pinned Mainsail CANCEL_PRINT macro is missing")
    elif not _ordered(
        client_cancel,
        ("_CLIENT_RETRACT", "TURN_OFF_HEATERS", "{client.user_cancel_macro"),
    ):
        failures.append("pinned Mainsail cancel no longer shuts heaters off before the T300 hook")

    cancel_hook = _klipper_section(lifecycle, "gcode_macro _T_CANCEL_PARK")
    if cancel_hook is None:
        failures.append("T300 cancel hook is missing")
    elif not _ordered(cancel_hook, ("_T_RETRACT_IF_HOT", "_T_SAFE_PARK")):
        failures.append("T300 cancel hook does not retract conditionally before parking")

    retract = _klipper_section(lifecycle, "gcode_macro _T_RETRACT_IF_HOT")
    if retract is None or "can_extrude" not in retract:
        failures.append("T300 retraction is not guarded by Klipper's hot-extrusion state")

    safe_park = _klipper_section(lifecycle, "gcode_macro _T_SAFE_PARK")
    if safe_park is None:
        failures.append("T300 clearance-aware park is missing")
    else:
        for required in (
            '"xyz" in printer.toolhead.homed_axes',
            "current_z + 5.0",
            "z_min",
            "max_z",
            "G1 Z{target_z}",
            "target_z - current_z >= 2.0",
            "G1 X297 Y297",
        ):
            if required not in safe_park:
                failures.append("T300 safe park lacks %s" % required)

    start = _klipper_section(lifecycle, "gcode_macro START_PRINT")
    if start is None:
        failures.append("START_PRINT is missing")
    else:
        for required in (
            "params.BED_TEMP is not defined",
            "params.EXTRUDER_TEMP is not defined",
            "printer.exclude_object.objects|length == 0",
            "printer.t300_safety.build_plate_ready",
        ):
            if required not in start:
                failures.append("START_PRINT lacks %s" % required)
        if not _ordered(
            start,
            (
                "_T_VALIDATE_PURGE_LANE",
                "T_RESERVE_PRINT_HOME",
                "M140 S{bed}",
                "M104 S150",
                "M190 S{bed}",
                "G28",
                "BED_MESH_CLEAR",
                "BED_MESH_CALIBRATE",
                "SMART_PARK",
                "M109 S{nozzle}",
                "LINE_PURGE",
            ),
        ):
            failures.append("START_PRINT operation order changed")

    plate_prompt = _klipper_section(lifecycle, "gcode_macro BUILD_PLATE_READY")
    if plate_prompt is None:
        failures.append("human-facing build-plate check macro is missing")
    else:
        for required in (
            "printer.t300_safety.build_plate_ready",
            "printer.virtual_sdcard.file_path is not none",
            "action:prompt_begin Build Plate Safety Check",
            "✓ Cleaned and rearmed|_T_CONFIRM_BUILD_PLATE|primary",
        ):
            if required not in plate_prompt:
                failures.append("build-plate check macro lacks %s" % required)

    home_prompt = _klipper_section(lifecycle, "gcode_macro HOME_PRINTER")
    if home_prompt is None:
        failures.append("human-facing HOME_PRINTER macro is missing")
    else:
        for required in (
            "printer.virtual_sdcard.file_path is not none",
            "printer.t300_safety.build_plate_ready",
            "action:prompt_begin Home T300",
            "_T_CONFIRM_AND_HOME",
        ):
            if required not in home_prompt:
                failures.append("HOME_PRINTER lacks %s" % required)

    for name in ("LOAD_FILAMENT", "UNLOAD_FILAMENT"):
        filament = _klipper_section(lifecycle, "gcode_macro " + name)
        if filament is None:
            failures.append("KlipperScreen-compatible %s macro is missing" % name)
        elif (
            "printer.pause_resume.is_paused" not in filament
            or "can_extrude" not in filament
            or "T_MARK_BUILD_PLATE_DIRTY" not in filament
        ):
            failures.append("%s lacks paused, hot, or plate-dirty protection" % name)

    purge_preflight = _klipper_section(
        lifecycle, "gcode_macro _T_VALIDATE_PURGE_LANE"
    )
    if purge_preflight is None:
        failures.append("pre-heat KAMP purge-lane validation is missing")
    else:
        for required in (
            "settings.purge_margin",
            "settings.purge_amount",
            "breakaway = 10.0",
            "has_front_lane",
            "has_left_lane",
            "action_raise_error",
        ):
            if required not in purge_preflight:
                failures.append("pre-heat KAMP purge validation lacks %s" % required)

    end = _klipper_section(lifecycle, "gcode_macro END_PRINT")
    if end is None or not _ordered(
        end, ("M400", "_T_RETRACT_IF_HOT", "TURN_OFF_HEATERS", "_T_SAFE_PARK")
    ):
        failures.append("END_PRINT no longer retracts, shuts down, and parks in order")

    idle = _klipper_section(machine, "idle_timeout")
    if idle is None or "TURN_OFF_HEATERS" not in idle or re.search(
        r"(?mi)^\s*M(?:18|84)\b", idle
    ):
        failures.append("idle timeout must shut heaters down without releasing motors")

    include_lines = [
        line.strip() for line in printer.splitlines() if line.strip().startswith("[include ")
    ]
    if not include_lines or include_lines[-1] != "[include safety.cfg]":
        failures.append("safety.cfg is not the final production include")
    if any("private/" in line for line in include_lines):
        failures.append("private maintenance macros are included in production")
    if "[include ../klipper/private/*.cfg]" not in maintenance:
        failures.append("maintenance configuration lacks the private workflow include")
    if re.search(r"(?m)^commissioning_lock:\s*True\s*$", safety) is None:
        failures.append("candidate stage does not retain the commissioning lock")
    if "M112" in lifecycle or "M112" in client:
        failures.append("a lifecycle macro attempts to redefine or invoke M112")

    frame = _klipper_section(timelapse, "gcode_macro TIMELAPSE_TAKE_FRAME")
    if frame is None:
        failures.append("non-moving TIMELAPSE_TAKE_FRAME macro is missing")
    else:
        if 'action_call_remote_method("timelapse_newframe"' not in frame:
            failures.append("timelapse frame macro lacks the reviewed remote request")
        for required in ("macropark=False", "hyperlapse=False"):
            if required not in frame:
                failures.append("timelapse frame macro lacks %s" % required)
        if re.search(
            r"(?mi)^\s*(?:G[0-3]|M10[49]|M1[49]0|PAUSE|RESUME|SET_GCODE_VARIABLE)\b",
            frame,
        ):
            failures.append("timelapse frame macro can alter printer state")
    if timelapse.count("[gcode_macro ") != 1:
        failures.append("production timelapse exposes more than one printer macro")
    if "vendor/timelapse" in printer:
        failures.append("full upstream motion-capable timelapse macros remain included")

    line_purge = _klipper_section(kamp_purge, "gcode_macro LINE_PURGE")
    if line_purge is None:
        failures.append("pinned KAMP LINE_PURGE macro is missing")
    else:
        for required in (
            "printer.toolhead.axis_maximum.x",
            "printer.toolhead.axis_maximum.y",
            "max_x_start",
            "max_y_start",
            "has_front_lane",
            "has_left_lane",
            "purge_on_x",
            "objects leave no bounded front or left purge lane",
            "purge_x_center + purge_amount + breakaway_distance",
            "purge_y_center + purge_amount + breakaway_distance",
        ):
            if required not in line_purge:
                failures.append("KAMP bounded purge lacks %s" % required)
        if not _ordered(
            line_purge,
            (
                "if not has_front_lane and not has_left_lane",
                "action_raise_error",
                "SAVE_GCODE_STATE NAME=Prepurge_State",
            ),
        ):
            failures.append("KAMP does not reject an unsafe lane before motion state is saved")
        if re.search(r"purge_(?:x|y)_center\s*\+\s*purge_amount\s*\+\s*10\b", line_purge):
            failures.append("KAMP retains an unbounded literal breakaway move")

    return {
        "passed": not failures,
        "reviewed_files": [str(path.relative_to(stage_root)) for path in paths.values()],
        "failures": failures,
    }


def _publishable_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, filenames in os.walk(repo_root, topdown=True):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            if name in IGNORED_REPO_PARTS:
                continue
            path = current_path / name
            relative = path.relative_to(repo_root)
            if path.is_symlink():
                raise ValidationError(
                    "publishable tree contains a symlink: %s" % relative
                )
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames):
            path = current_path / name
            relative = path.relative_to(repo_root)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise ValidationError(
                    "publishable tree contains a symlink: %s" % relative
                )
            if not stat.S_ISREG(info.st_mode):
                raise ValidationError(
                    "publishable tree contains a special file: %s" % relative
                )
            files.append(path)
    return files


def scan_publishable_tree(repo_root: Path) -> dict[str, Any]:
    failures: list[str] = []
    digest = hashlib.sha256()
    files = _publishable_files(repo_root)
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        if path.name in PRIVATE_BASENAMES or path.suffix.lower() == ".zip":
            failures.append("private or archive file is publishable: %s" % relative)
            continue
        content = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(content).digest())
        if any(marker in content for marker in PRIVATE_KEY_MARKERS):
            failures.append("private key material found: %s" % relative)
    return {
        "passed": not failures,
        "files_scanned": len(files),
        "source_tree_sha256": digest.hexdigest(),
        "failures": failures,
    }


def generate_report(
    repo_root: Path,
    stage: Path,
    stage_manifest_sha256: str,
    output: Path,
) -> dict[str, Any]:
    repo_root = repo_root.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise ValidationError("validation report output already exists")
    evidence: dict[str, Any] = {}
    try:
        stage_info = verify_stage(stage, stage_manifest_sha256)
        evidence["stage_verified"] = {
            "passed": True,
            "manifest_sha256": stage_manifest_sha256,
            "file_count": len(stage_info["files"]),
        }
    except (OSError, ProvisionError, ValueError) as exc:
        evidence["stage_verified"] = {"passed": False, "error": str(exc)}
        stage_info = None

    python = sys.executable
    evidence["unit_tests_passed"] = _command_evidence(
        [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        repo_root,
        900,
    )
    evidence["vendor_v012_harness_passed"] = _command_evidence(
        [python, "bin/test-klipper-v012.py"],
        repo_root,
        900,
    )
    evidence["klipper_v013_harness_passed"] = _command_evidence(
        [
            python,
            "bin/test-klipper-v013-mainline.py",
            "--stage",
            str(stage),
            "--stage-manifest-sha256",
            stage_manifest_sha256,
        ],
        repo_root,
        900,
    )
    evidence["gcode_policy_tests_passed"] = _command_evidence(
        [
            python,
            "-m",
            "unittest",
            "tests.test_mainline_policy",
            "tests.test_timelapse_policy",
            "tests.test_commissioning",
            "tests.test_config_deploy",
        ],
        repo_root,
        300,
    )
    if stage_info is None:
        evidence["klipper_lifecycle_reviewed"] = {
            "passed": False,
            "failures": ["stage verification failed"],
        }
        evidence["systemd_units_reviewed"] = {
            "passed": False,
            "failures": ["stage verification failed"],
        }
        evidence["host_network_boundary_reviewed"] = {
            "passed": False,
            "failures": ["stage verification failed"],
        }
        evidence["operator_ui_reviewed"] = {
            "passed": False,
            "failures": ["stage verification failed"],
        }
    else:
        evidence["klipper_lifecycle_reviewed"] = review_klipper_lifecycle(
            stage_info["root"]
        )
        evidence["systemd_units_reviewed"] = review_systemd_units(stage_info["root"])
        evidence["host_network_boundary_reviewed"] = review_host_network_boundary(
            stage_info["root"]
        )
        evidence["operator_ui_reviewed"] = review_operator_ui(stage_info["root"])
    try:
        evidence["secret_scan_passed"] = scan_publishable_tree(repo_root)
    except (OSError, ValidationError) as exc:
        evidence["secret_scan_passed"] = {"passed": False, "error": str(exc)}

    checks = {name: evidence[name].get("passed") is True for name in CHECK_NAMES}
    report = {
        "schema_version": 1,
        "generated_by": REPORT_GENERATOR,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage_manifest_sha256": stage_manifest_sha256,
        "checks": checks,
        "evidence": evidence,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".%s." % output.name, dir=output.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_canonical_json(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return report


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--stage-manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = generate_report(
            Path(__file__).resolve().parents[1],
            args.stage,
            args.stage_manifest_sha256,
            args.output,
        )
    except (OSError, ValidationError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(report["checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())

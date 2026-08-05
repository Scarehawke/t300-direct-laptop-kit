#!/usr/bin/env python3
"""Safe, dependency-free helper for a directly connected T300/Moonraker host."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import difflib
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sys
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from t300_mainline.imaging import (  # noqa: E402
    ImagingError,
    RecoveryClient,
    capture_image,
    verify_image,
    verify_image_filesystems,
    write_image,
)


MAX_CONFIG_FILE = 20 * 1024 * 1024
MAX_CONFIG_TOTAL = 250 * 1024 * 1024
MAX_MACRO_FILE = 2 * 1024 * 1024
DEFAULT_SUBNET = "10.42.42.0/24"
GERGO_MACRO_FILENAME = "macro_z_tilt_via_knob.cfg"
OPEN_MACRO_FILENAME = "t300_gantry_level.cfg"
OPEN_MACRO_PATH = Path(__file__).resolve().parents[1] / "macros" / OPEN_MACRO_FILENAME
CORE_MACRO_FILENAME = "t300_core.cfg"
CORE_MACRO_PATH = Path(__file__).resolve().parents[1] / "macros" / CORE_MACRO_FILENAME
RUNTIME_MACRO_FILENAME = "t300_runtime.cfg"
RUNTIME_MACRO_PATH = Path(__file__).resolve().parents[1] / "macros" / RUNTIME_MACRO_FILENAME
MAINSAIL_CLIENT_FILENAME = "mainsail_client.cfg"
MAINSAIL_CLIENT_REVISION = "ff3869a621db17ce3ef660adbbd3fa321995ac42"
MAINSAIL_CLIENT_SHA256 = "29d4c97b099e481c25c0a875b3f0696850a6aafa67775aee8d05e8682352ffb4"
MAINSAIL_CLIENT_PATH = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "community-sources"
    / "mainsail-config"
    / "client.cfg"
)
KAMP_MACRO_FILENAME = "kamp_t300.cfg"
KAMP_REVISION = "b0dad8ec9ee31cb644b94e39d4b8a8fb9d6c9ba0"
KAMP_SOURCE_DIR = (
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "community-sources"
    / "kamp"
    / "Configuration"
)
KAMP_FILES = (
    ("KAMP_Settings.cfg", "a8c78ba4518942abaa3ff34d468b83d50b0f3af0ea8f048fa0fe714ef2397f13"),
    ("Line_Purge.cfg", "7fa5b694710fbe5288be06503e44a827d62474d79b284726eb41f659c99d66ec"),
    ("Smart_Park.cfg", "5edd9ed2a8dddb2d3d49d7a08f4aef66cd653e7babef188edb8bd098af971fcb"),
)
KAMP_TIP_DISTANCE = "0"
KAMP_PURGE_MARGIN = "20"
DEFAULT_END_CLEAN_HEIGHT = 200.0


class T300Error(RuntimeError):
    pass


def normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise T300Error("Printer host is empty")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise T300Error("Printer URL must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise T300Error("Printer URL must contain only a host and optional port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise T300Error("Printer URL must not contain a path, query, or fragment")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{netloc}"


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "error" in payload:
        error = payload["error"]
        if isinstance(error, dict):
            raise T300Error(str(error.get("message", error)))
        raise T300Error(str(error))
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


class Moonraker:
    def __init__(self, host: str, api_key: str | None = None, timeout: float = 5.0):
        self.base_url = normalize_base_url(host)
        self.api_key = api_key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "t300-direct-kit/1.0"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            raise T300Error(f"Moonraker returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise T300Error(f"Could not reach {self.base_url}: {exc}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise T300Error("Moonraker JSON response exceeded the 4 MiB safety limit")
        try:
            return unwrap(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise T300Error("Moonraker returned an invalid JSON response") from exc

    def get_json(self, path: str) -> Any:
        return self.request_json("GET", path)

    def post_json(self, path: str, payload: Any | None = None) -> Any:
        if payload is None:
            return self.request_json("POST", path, b"")
        body = json.dumps(payload).encode("utf-8")
        return self.request_json("POST", path, body, "application/json")

    def download_bytes(self, root: str, filename: str, limit: int) -> bytes:
        safe_path = validate_remote_path(filename)
        encoded = urllib.parse.quote(str(safe_path), safe="/")
        request = urllib.request.Request(
            f"{self.base_url}/server/files/{root}/{encoded}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            raise T300Error(f"Could not download {filename}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise T300Error(f"Could not download {filename}: {exc}") from exc
        if len(data) > limit:
            raise T300Error(f"Refusing to load {filename}: it exceeds {limit} bytes")
        return data

    def upload_config(self, filename: str, content: bytes) -> Any:
        if len(content) > MAX_CONFIG_FILE:
            raise T300Error(f"Refusing to upload {filename}: file is too large")
        boundary = "----t300-" + uuid.uuid4().hex
        checksum = hashlib.sha256(content).hexdigest()
        fields: list[bytes] = []

        def field(name: str, value: str) -> None:
            fields.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                ]
            )

        field("root", "config")
        field("checksum", checksum)
        fields.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return self.request_json(
            "POST",
            "/server/files/upload",
            b"".join(fields),
            f"multipart/form-data; boundary={boundary}",
        )

    def delete_file(self, root: str, filename: str) -> Any:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", root):
            raise T300Error(f"Unsafe Moonraker root: {root!r}")
        safe_path = validate_remote_path(filename)
        encoded = urllib.parse.quote(str(safe_path), safe="/")
        return self.request_json("DELETE", f"/server/files/{root}/{encoded}")


def validate_remote_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise T300Error(f"Unsafe path returned by printer: {value!r}")
    return path


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def make_backup(client: Moonraker, output: Path | None = None) -> Path:
    files = client.get_json("/server/files/list?root=config")
    if not isinstance(files, list):
        raise T300Error("Moonraker returned an unexpected config file list")

    declared_total = 0
    normalized: list[tuple[PurePosixPath, int, dict[str, Any]]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise T300Error("Moonraker returned malformed config file metadata")
        remote = validate_remote_path(item["path"])
        size = int(item.get("size", 0))
        if size < 0 or size > MAX_CONFIG_FILE:
            raise T300Error(f"Refusing backup: {remote} declares an unsafe size of {size} bytes")
        declared_total += size
        normalized.append((remote, size, item))
    if declared_total > MAX_CONFIG_TOTAL:
        raise T300Error(
            f"Refusing backup: declared config data is {declared_total} bytes, above the safety cap"
        )

    target = output or Path.cwd() / "t300-backups" / f"config-{timestamp()}"
    target = target.expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise T300Error(f"Backup destination exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()):
        raise T300Error(f"Backup destination already exists and is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    config_root = target / "config-root"
    config_root.mkdir()

    checksums: list[str] = []
    actual_total = 0
    for remote, _, _ in normalized:
        content = client.download_bytes("config", str(remote), MAX_CONFIG_FILE)
        actual_total += len(content)
        if actual_total > MAX_CONFIG_TOTAL:
            raise T300Error("Backup exceeded the total-size safety cap while downloading")
        destination = config_root.joinpath(*remote.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        checksums.append(f"{digest}  config-root/{remote}")

    manifest = {
        "schema": 2,
        "scope": "moonraker-config-root-only",
        "created": dt.datetime.now().astimezone().isoformat(),
        "source": client.base_url,
        "declared_bytes": declared_total,
        "downloaded_bytes": actual_total,
        "files": [item for _, _, item in normalized],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (target / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return target


def verify_backup(value: Path) -> int:
    root = value.expanduser().resolve()
    sums_path = root / "SHA256SUMS"
    if not sums_path.is_file():
        raise T300Error(f"Backup has no SHA256SUMS file: {root}")
    try:
        lines = sums_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise T300Error(f"Could not read {sums_path}: {exc}") from exc
    if not lines:
        raise T300Error("Backup checksum list is empty")
    checked = 0
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise T300Error(f"Malformed backup checksum line: {line!r}")
        recorded = validate_remote_path(match.group(2))
        relative = (
            recorded
            if recorded.parts[0] == "config-root"
            else PurePosixPath("config-root") / recorded
        )
        path = root.joinpath(*relative.parts)
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise T300Error(f"Backup checksum path escapes its root: {relative}") from exc
        if not path.is_file():
            raise T300Error(f"Backup file is missing: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != match.group(1):
            raise T300Error(f"Backup checksum mismatch: {relative}")
        checked += 1
    return checked


def patch_printer_cfg(
    text: str, include_filename: str = GERGO_MACRO_FILENAME
) -> tuple[str, bool]:
    include_line = f"[include {include_filename}]"
    include_pattern = re.compile(
        rf"^\s*\[include\s+{re.escape(include_filename)}\]\s*(?:#.*)?$", re.IGNORECASE
    )
    macro_pattern = re.compile(r"^\s*\[include\s+Macro\.cfg\]\s*(?:#.*)?$", re.IGNORECASE)
    lines = text.splitlines(keepends=True)
    if any(include_pattern.match(line.rstrip("\r\n")) for line in lines):
        return text, False

    for index, line in enumerate(lines):
        if macro_pattern.match(line.rstrip("\r\n")):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines.insert(index + 1, include_line + newline)
            return "".join(lines), True
    raise T300Error("printer.cfg does not contain the expected [include Macro.cfg] line")


def patch_core_printer_cfg(text: str) -> tuple[str, bool]:
    include_line = f"[include {CORE_MACRO_FILENAME}]"
    include_pattern = re.compile(
        rf"^\s*\[include\s+{re.escape(CORE_MACRO_FILENAME)}\]\s*(?:#.*)?$",
        re.IGNORECASE,
    )
    macro_pattern = re.compile(
        r"^\s*\[include\s+Macro\.cfg\]\s*(?:#.*)?$", re.IGNORECASE
    )
    lines = text.splitlines(keepends=True)
    if not any(macro_pattern.match(line.rstrip("\r\n")) for line in lines):
        raise T300Error("printer.cfg does not contain the expected [include Macro.cfg] line")

    # Klipper applies includes in textual order. Keep the overlay after every
    # ordinary printer section so later factory values cannot override it.
    filtered = [
        line
        for line in lines
        if not include_pattern.match(line.rstrip("\r\n"))
    ]
    save_marker = re.compile(r"^\s*#\*#.*\bSAVE_CONFIG\b", re.IGNORECASE)
    anchor = next(
        (
            index
            for index, line in enumerate(filtered)
            if save_marker.match(line.rstrip("\r\n"))
        ),
        len(filtered),
    )
    newline = "\r\n" if "\r\n" in text else "\n"
    if anchor and not filtered[anchor - 1].endswith(("\n", "\r")):
        filtered[anchor - 1] += newline
    filtered.insert(anchor, include_line + newline)
    proposed = "".join(filtered)
    return proposed, proposed != text


def patch_runtime_printer_cfg(text: str) -> tuple[str, bool]:
    filenames = (MAINSAIL_CLIENT_FILENAME, RUNTIME_MACRO_FILENAME)
    include_patterns = {
        filename: re.compile(
            rf"^\s*\[include\s+{re.escape(filename)}\]\s*(?:#.*)?$",
            re.IGNORECASE,
        )
        for filename in filenames
    }
    macro_pattern = re.compile(
        r"^\s*\[include\s+Macro\.cfg\]\s*(?:#.*)?$", re.IGNORECASE
    )
    lines = text.splitlines(keepends=True)
    if not any(macro_pattern.match(line.rstrip("\r\n")) for line in lines):
        raise T300Error("printer.cfg does not contain the expected [include Macro.cfg] line")

    filtered = [
        line
        for line in lines
        if not any(pattern.match(line.rstrip("\r\n")) for pattern in include_patterns.values())
    ]
    save_marker = re.compile(r"^\s*#\*#.*\bSAVE_CONFIG\b", re.IGNORECASE)
    anchor = next(
        (
            index
            for index, line in enumerate(filtered)
            if save_marker.match(line.rstrip("\r\n"))
        ),
        len(filtered),
    )
    newline = "\r\n" if "\r\n" in text else "\n"
    if anchor and not filtered[anchor - 1].endswith(("\n", "\r")):
        filtered[anchor - 1] += newline
    includes = [f"[include {filename}]{newline}" for filename in filenames]
    filtered[anchor:anchor] = includes
    proposed = "".join(filtered)
    return proposed, proposed != text


def has_config_include(text: str, filename: str) -> bool:
    pattern = re.compile(
        rf"^\s*\[include\s+{re.escape(filename)}\]\s*(?:#.*)?$",
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.search(text) is not None


def validate_leveling_exclusivity(text: str, selected_filename: str) -> None:
    competitors = {
        GERGO_MACRO_FILENAME: OPEN_MACRO_FILENAME,
        OPEN_MACRO_FILENAME: GERGO_MACRO_FILENAME,
    }
    if selected_filename not in competitors:
        raise T300Error(f"Unknown leveling workflow: {selected_filename}")
    competing = competitors[selected_filename]
    if has_config_include(text, competing):
        raise T300Error(
            f"Refusing to install {selected_filename} while competing leveling "
            f"workflow {competing} is included"
        )


def read_macro(source: Path) -> bytes:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise T300Error(f"Macro source does not exist: {source}")
    if source.suffix.lower() == ".cfg":
        content = source.read_bytes()
    elif source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            direct_matches = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).name == GERGO_MACRO_FILENAME
            ]
            if len(direct_matches) == 1:
                info = direct_matches[0]
                if info.file_size > MAX_MACRO_FILE:
                    raise T300Error("Macro in ZIP exceeds the 2 MiB safety limit")
                content = archive.read(info)
            elif len(direct_matches) > 1:
                raise T300Error(
                    f"Expected exactly one {GERGO_MACRO_FILENAME} in the ZIP; "
                    f"found {len(direct_matches)}"
                )
            else:
                nested_matches = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and PurePosixPath(info.filename).name.lower()
                    == "macro_v3(extract!).zip"
                ]
                if len(nested_matches) != 1:
                    raise T300Error(
                        f"Expected {GERGO_MACRO_FILENAME} or one official "
                        "macro_v3(extract!).zip in the supplied archive"
                    )
                nested_info = nested_matches[0]
                if nested_info.file_size > MAX_MACRO_FILE:
                    raise T300Error("Nested macro ZIP exceeds the 2 MiB safety limit")
                nested_bytes = archive.read(nested_info)
                with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested_archive:
                    inner_matches = [
                        info
                        for info in nested_archive.infolist()
                        if not info.is_dir()
                        and PurePosixPath(info.filename).name == GERGO_MACRO_FILENAME
                    ]
                    if len(inner_matches) != 1:
                        raise T300Error(
                            f"Expected exactly one {GERGO_MACRO_FILENAME} in the "
                            f"nested ZIP; found {len(inner_matches)}"
                        )
                    inner_info = inner_matches[0]
                    if inner_info.file_size > MAX_MACRO_FILE:
                        raise T300Error("Macro in nested ZIP exceeds the 2 MiB safety limit")
                    content = nested_archive.read(inner_info)
    else:
        raise T300Error("Macro source must be GerGo's ZIP or macro_z_tilt_via_knob.cfg")

    if len(content) > MAX_MACRO_FILE or b"\x00" in content:
        raise T300Error("Macro file is not a safe, ordinary text configuration")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise T300Error("Macro configuration is not valid UTF-8") from exc
    if not re.search(r"^\s*\[gcode_macro\s+[^]]+\]", text, re.MULTILINE | re.IGNORECASE):
        raise T300Error("No [gcode_macro ...] section was found in the supplied file")
    return content


def read_kamp_macro() -> bytes:
    """Assemble the pinned KAMP park/purge subset without its mesh override."""
    parts = [
        (
            "# SPDX-License-Identifier: GPL-3.0-only\n"
            "# KAMP subset for the stock T300: settings, Line Purge, and Smart Park only.\n"
            f"# Upstream revision: {KAMP_REVISION}\n"
            "# Adaptive_Meshing.cfg is deliberately not installed; the T300 keeps its native mesher.\n\n"
        ).encode("ascii")
    ]
    for filename, expected_digest in KAMP_FILES:
        path = KAMP_SOURCE_DIR / filename
        if not path.is_file():
            raise T300Error(
                "Pinned KAMP source is missing; run python3 ./bin/prepare-community.py first"
            )
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_digest:
            raise T300Error(
                f"Pinned KAMP source failed checksum validation: {filename}"
            )
        if filename == "KAMP_Settings.cfg":
            # The upstream include examples stay commented. The two selected macro
            # files are appended below, yielding one self-contained config file.
            if b"[include ./KAMP/Adaptive_Meshing.cfg]" not in content:
                raise T300Error("KAMP settings no longer match the reviewed revision")
            upstream_tip = b"variable_tip_distance: 0"
            if content.count(upstream_tip) != 1:
                raise T300Error("KAMP tip-distance default no longer matches the reviewed revision")
            content = content.replace(
                upstream_tip,
                f"variable_tip_distance: {KAMP_TIP_DISTANCE}".encode("ascii"),
            )
            upstream_margin = b"variable_purge_margin: 10"
            if content.count(upstream_margin) != 1:
                raise T300Error("KAMP purge-margin default no longer matches the reviewed revision")
            content = content.replace(
                upstream_margin,
                f"variable_purge_margin: {KAMP_PURGE_MARGIN}".encode("ascii"),
            )
        parts.extend(
            [
                f"# --- upstream {filename} ---\n".encode("ascii"),
                content.rstrip() + b"\n\n",
            ]
        )
    combined = b"".join(parts)
    names = set(macro_names(combined))
    expected = {"_KAMP_Settings", "LINE_PURGE", "SMART_PARK"}
    if names != expected or b"[gcode_macro BED_MESH_CALIBRATE]" in combined:
        raise T300Error("KAMP subset contains unexpected macro ownership")
    return combined


def macro_names(content: bytes) -> list[str]:
    text = content.decode("utf-8")
    return re.findall(r"^\s*\[gcode_macro\s+([^]]+)\]", text, re.MULTILINE | re.IGNORECASE)


def read_open_macro() -> bytes:
    if not OPEN_MACRO_PATH.is_file():
        raise T300Error(f"Bundled open macro is missing: {OPEN_MACRO_PATH}")
    content = OPEN_MACRO_PATH.read_bytes()
    if len(content) > MAX_MACRO_FILE or b"\x00" in content:
        raise T300Error("Bundled open macro failed its size/text safety check")
    content.decode("utf-8")
    return content


def read_core_macro() -> bytes:
    if not CORE_MACRO_PATH.is_file():
        raise T300Error(f"Bundled core macro is missing: {CORE_MACRO_PATH}")
    content = CORE_MACRO_PATH.read_bytes()
    if len(content) > MAX_MACRO_FILE or b"\x00" in content:
        raise T300Error("Bundled core macro failed its size/text safety check")
    content.decode("utf-8")
    return content


def read_runtime_macro() -> bytes:
    if not RUNTIME_MACRO_PATH.is_file():
        raise T300Error(f"Bundled runtime macro is missing: {RUNTIME_MACRO_PATH}")
    content = RUNTIME_MACRO_PATH.read_bytes()
    if len(content) > MAX_MACRO_FILE or b"\x00" in content:
        raise T300Error("Bundled runtime macro failed its size/text safety check")
    content.decode("utf-8")
    required = {
        "START_PRINT",
        "END_PRINT",
        "_T_RUNTIME_SAFE_EXIT",
        "_T_RUNTIME_CANCEL_EXIT",
        "T_RELEASE_MOTORS",
        "M600",
        "RESUME_INTERRUPTED",
    }
    names = set(macro_names(content))
    if not required.issubset(names):
        raise T300Error("Bundled runtime macro is missing required sections")
    if config_section(content.decode("utf-8"), "delayed_gcode", "_T_RUNTIME_CANCEL_POST") is None:
        raise T300Error("Bundled runtime macro is missing delayed post-cancel cleanup")
    return content


def read_mainsail_client() -> bytes:
    if not MAINSAIL_CLIENT_PATH.is_file():
        raise T300Error(
            "Pinned Mainsail source is missing; run python3 ./bin/prepare-community.py first"
        )
    source = MAINSAIL_CLIENT_PATH.read_bytes()
    if hashlib.sha256(source).hexdigest() != MAINSAIL_CLIENT_SHA256:
        raise T300Error("Pinned Mainsail client.cfg failed checksum validation")
    old_path = b"path: ~/printer_data/gcodes"
    if source.count(old_path) != 1:
        raise T300Error("Mainsail virtual-SD path no longer matches the reviewed revision")
    generated = (
        "# Generated for the T300 from Mainsail client.cfg.\n"
        f"# Upstream revision: {MAINSAIL_CLIENT_REVISION}\n"
        "# Only virtual_sdcard.path is adapted to the vendor filesystem.\n\n"
    ).encode("ascii") + source.replace(old_path, b"path: ~/gcode_files")
    names = set(macro_names(generated))
    required = {"PAUSE", "RESUME", "CANCEL_PRINT", "_TOOLHEAD_PARK_PAUSE_CANCEL"}
    if not required.issubset(names) or {"START_PRINT", "END_PRINT"} & names:
        raise T300Error("Mainsail client component has unexpected lifecycle ownership")
    return generated


def config_section(text: str, kind: str, name: str = "") -> str | None:
    wanted = (kind + (" " + name if name else "")).casefold()
    starts = list(
        re.finditer(r"^\s*\[\s*([^]]+?)\s*\]\s*(?:[#;].*)?$", text, re.MULTILINE)
    )
    for index, match in enumerate(starts):
        if match.group(1).strip().casefold() != wanted:
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        return text[match.start() : end]
    return None


def patch_end_print_clean_height(text: str, height: float) -> tuple[str, bool]:
    if not 20 <= height <= 340:
        raise T300Error("End cleaning height must be between 20 and 340 mm")
    end_print = config_section(text, "gcode_macro", "END_PRINT")
    if end_print is None:
        raise T300Error("Macro.cfg is missing the factory END_PRINT macro")

    factory_lift_pattern = re.compile(
        r"    \{% if \(printer\.gcode_move\.position\.z \+ 10\) < z_max %\}\s*\n"
        r"        G1 Z\+10 F3000\s*\n"
        r"    \{% else %\}\s*\n"
        r"        G1 Z\+\{\(z_max - printer\.gcode_move\.position\.z\)\} F3000\s*\n"
        r"    \{% endif %\}\s*\n"
        r"    G90\s*\n"
        r"    G1 X0 Y300\s*\n"
    )
    marker = "    # T300 laptop nozzle-cleaning park\n"
    height_pattern = re.compile(
        r"(    # T300 laptop nozzle-cleaning park\n"
        r"    \{% set clean_z = \[printer\.gcode_move\.position\.z \+ 10, )"
        r"[0-9.]+"
        r"(\] \| max %\}\n)"
    )
    replacement = (
        marker
        +
        f"    {{% set clean_z = [printer.gcode_move.position.z + 10, {height:g}] | max %}}\n"
        "    {% set clean_z = [clean_z, z_max] | min %}\n"
        "    G90\n"
        "    G1 Z{clean_z} F3000\n"
        "    G1 X0 Y300\n"
    )

    if factory_lift_pattern.search(end_print):
        proposed_section = factory_lift_pattern.sub(replacement, end_print, count=1)
    elif marker in end_print:
        proposed_section, count = height_pattern.subn(
            lambda match: match.group(1) + f"{height:g}" + match.group(2),
            end_print,
            count=1,
        )
        if count != 1:
            raise T300Error("Existing END_PRINT cleaning park has an unexpected format")
    else:
        raise T300Error("Factory END_PRINT lift block no longer matches the reviewed T300 config")

    proposed = text.replace(end_print, proposed_section, 1)
    end_changed = proposed != text
    proposed, cancel_changed = patch_cancel_print_clean_height(proposed, height)
    return proposed, end_changed or cancel_changed


def patch_cancel_print_clean_height(text: str, height: float) -> tuple[str, bool]:
    cancel_print = config_section(text, "gcode_macro", "CANCEL_PRINT")
    if cancel_print is None:
        raise T300Error("Macro.cfg is missing the factory CANCEL_PRINT macro")
    if not re.search(
        r"(?mi)^\s*rename_existing\s*:\s*CANCEL_PRINT_BASE\s*$", cancel_print
    ):
        raise T300Error("Factory CANCEL_PRINT no longer exposes CANCEL_PRINT_BASE")

    marker = "    # T300 laptop cancel-cleaning park\n"
    height_pattern = re.compile(
        r"(    # T300 laptop cancel-cleaning park\n"
        r"    \{% set homed_axes = printer\.toolhead\.homed_axes\|lower %\}\n"
        r"    \{% if \"xyz\" in homed_axes %\}\n"
        r"        \{% set clean_z = \[printer\.gcode_move\.position\.z \+ 10, )"
        r"[0-9.]+"
        r"(\] \| max %\}\n)"
    )
    replacement = (
        marker
        + "    {% set homed_axes = printer.toolhead.homed_axes|lower %}\n"
        "    {% if \"xyz\" in homed_axes %}\n"
        f"        {{% set clean_z = [printer.gcode_move.position.z + 10, {height:g}] | max %}}\n"
        "        {% set clean_z = [clean_z, z_lift_max|float] | min %}\n"
        "        G90\n"
        "        G1 Z{clean_z} F3000\n"
        "        G1 X{x_park} Y{y_park} F6000\n"
        "    {% else %}\n"
        "        {action_respond_info(\"Cancel cleanup skipped: axes are not homed\")}\n"
        "    {% endif %}\n\n"
    )

    if marker in cancel_print:
        proposed_section, count = height_pattern.subn(
            lambda match: match.group(1) + f"{height:g}" + match.group(2),
            cancel_print,
            count=1,
        )
        if count != 1:
            raise T300Error("Existing CANCEL_PRINT cleaning park has an unexpected format")
    else:
        if "printer.toolhead.homed_axe" not in cancel_print:
            raise T300Error("Factory CANCEL_PRINT motion block no longer matches the reviewed T300 config")
        start_marker = "    {% if printer.pause_resume.is_paused == True %}"
        end_marker = "    TURN_OFF_HEATERS"
        if cancel_print.count(start_marker) != 1 or cancel_print.count(end_marker) != 1:
            raise T300Error("Factory CANCEL_PRINT park boundaries are ambiguous")
        start = cancel_print.index(start_marker)
        end = cancel_print.index(end_marker, start)
        proposed_section = cancel_print[:start] + replacement + cancel_print[end:]

    proposed = text.replace(cancel_print, proposed_section, 1)
    return proposed, proposed != text


def set_end_clean_height(
    client: Moonraker,
    height: float,
    apply: bool,
    output: Path | None,
) -> None:
    raise T300Error(
        "set-end-clean-height is quarantined because patching only the factory end/cancel "
        "macros leaves split lifecycle ownership. Review the offline runtime proposal instead."
    )


def validate_core_compatibility(
    printer_cfg: str,
    factory_macros: str,
    plr_cfg: str,
    klipper_version: str,
) -> None:
    if not klipper_version.startswith("v0.12.0"):
        raise T300Error(
            "The T300 core overlay is pinned to the vendor Klipper 0.12.0 family; "
            f"printer reported {klipper_version or 'unknown'}"
        )
    if not has_config_include(printer_cfg, GERGO_MACRO_FILENAME):
        raise T300Error("The selected GerGo macro include is missing from printer.cfg")
    if has_config_include(printer_cfg, OPEN_MACRO_FILENAME):
        raise T300Error("Remove the competing open gantry macro before installing T300 core")

    required_aliases = {
        "BED_MESH_CALIBRATE": "BED_MESH_CALIBRATE_BASE",
        "PAUSE": "PAUSE_BASE",
        "RESUME": "RESUME_BASE",
        "CANCEL_PRINT": "CANCEL_PRINT_BASE",
        "M109": "M99109",
        "M190": "M99190",
    }
    for name, alias in required_aliases.items():
        section = config_section(factory_macros, "gcode_macro", name)
        if section is None:
            raise T300Error(f"Factory Macro.cfg is missing [gcode_macro {name}]")
        pattern = re.compile(
            rf"^\s*rename_existing\s*:\s*{re.escape(alias)}\s*(?:[#;].*)?$",
            re.IGNORECASE | re.MULTILINE,
        )
        if not pattern.search(section):
            raise T300Error(
                f"Factory [gcode_macro {name}] does not provide expected alias {alias}"
            )

    required_macros = {
        "START_PRINT",
        "END_PRINT",
        "DEFAULT_LOAD_FILAMENT",
        "DEFAULT_UNLOAD_FILAMENT",
        "PRINTING_UNLOAD_FILAMENT",
        "M600",
        "PAUSE_UNLOAD_FILAMENT",
        "LOAD_FILAMENT_RESUME",
    }
    missing = sorted(
        name
        for name in required_macros
        if config_section(factory_macros, "gcode_macro", name) is None
    )
    if missing:
        raise T300Error("Factory Macro.cfg is missing compatibility macros: " + ", ".join(missing))
    if config_section(plr_cfg, "gcode_macro", "RESUME_INTERRUPTED") is None:
        raise T300Error("plr.cfg does not contain the expected RESUME_INTERRUPTED macro")
    if config_section(plr_cfg, "force_move") is None:
        raise T300Error("plr.cfg does not contain the expected force_move section")


def _section_number(text: str, kind: str, option: str) -> float:
    section = config_section(text, kind)
    if section is None:
        raise T300Error(f"Required [{kind}] section is missing")
    match = re.search(
        rf"(?mi)^\s*{re.escape(option)}\s*[:=]\s*(-?[0-9.]+)", section
    )
    if match is None:
        raise T300Error(f"[{kind}] is missing numeric {option}")
    return float(match.group(1))


def validate_runtime_compatibility(
    printer_cfg: str,
    factory_macros: str,
    plr_cfg: str,
    kamp_macro: str,
    klipper_version: str,
) -> None:
    if not klipper_version.startswith("v0.12.0"):
        raise T300Error(
            "The runtime proposal is pinned to the vendor Klipper 0.12.0 family; "
            f"printer reported {klipper_version or 'unknown'}"
        )
    if not has_config_include(printer_cfg, GERGO_MACRO_FILENAME):
        raise T300Error("The selected GerGo leveling include is missing")
    if not has_config_include(printer_cfg, KAMP_MACRO_FILENAME):
        raise T300Error("The approved KAMP park/purge include is missing")
    if has_config_include(printer_cfg, OPEN_MACRO_FILENAME):
        raise T300Error("The competing open gantry macro must not be active")
    if has_config_include(printer_cfg, CORE_MACRO_FILENAME):
        raise T300Error("The quarantined t300_core.cfg include must remain disabled")
    if not re.search(r"(?mi)^\s*\[exclude_object\]\s*$", printer_cfg):
        raise T300Error("printer.cfg must define [exclude_object] for KAMP")

    required_aliases = {
        "PAUSE": "PAUSE_BASE",
        "RESUME": "RESUME_BASE",
        "CANCEL_PRINT": "CANCEL_PRINT_BASE",
        "M109": "M99109",
        "M190": "M99190",
    }
    for name, alias in required_aliases.items():
        section = config_section(factory_macros, "gcode_macro", name)
        if section is None or not re.search(
            rf"(?mi)^\s*rename_existing\s*:\s*{re.escape(alias)}\s*$", section
        ):
            raise T300Error(f"Factory [gcode_macro {name}] does not expose {alias}")
    start = config_section(factory_macros, "gcode_macro", "START_PRINT")
    if start is None or not re.search(r"(?mi)^\s*variable_state\s*:", start):
        raise T300Error("Factory START_PRINT state contract was not found")
    if config_section(plr_cfg, "gcode_macro", "RESUME_INTERRUPTED") is None:
        raise T300Error("plr.cfg does not contain the recovery macro being quarantined")
    if config_section(plr_cfg, "force_move") is None:
        raise T300Error("plr.cfg does not contain the force_move setting being overridden")
    if config_section(plr_cfg, "gcode_macro", "clear_last_file") is None:
        raise T300Error("plr.cfg does not contain the state-cleanup helper")
    if config_section(printer_cfg, "filament_switch_sensor", "my_sensor") is None:
        raise T300Error("The reviewed T300 filament sensor was not found")

    kamp_names = set(macro_names(kamp_macro.encode("utf-8")))
    if kamp_names != {"_KAMP_Settings", "LINE_PURGE", "SMART_PARK"}:
        raise T300Error("KAMP file does not contain only the approved park/purge subset")
    settings = config_section(kamp_macro, "gcode_macro", "_KAMP_Settings") or ""
    if not re.search(r"(?mi)^\s*variable_tip_distance\s*:\s*0(?:\.0+)?\s*(?:#.*)?$", settings):
        raise T300Error("KAMP stationary tip advance is not disabled")
    margin = re.search(r"(?mi)^\s*variable_purge_margin\s*:\s*([0-9.]+)", settings)
    if margin is None or float(margin.group(1)) < 20:
        raise T300Error("KAMP purge margin must be at least 20 mm for this proposal")

    x_min = _section_number(printer_cfg, "stepper_x", "position_min")
    x_max = _section_number(printer_cfg, "stepper_x", "position_max")
    y_min = _section_number(printer_cfg, "stepper_y", "position_min")
    y_max = _section_number(printer_cfg, "stepper_y", "position_max")
    z_max = _section_number(printer_cfg, "stepper_z", "position_max")
    if not (x_min <= 10 <= x_max and y_min <= 290 <= y_max and z_max >= 200):
        raise T300Error("T300 cleanup coordinates exceed the configured axis limits")


def numeric_pair(value: Any, name: str) -> tuple[float, float]:
    if isinstance(value, str):
        parts: Any = [part.strip() for part in value.split(",")]
    else:
        parts = value
    if not isinstance(parts, (list, tuple)) or len(parts) != 2:
        raise T300Error(f"Klipper setting {name} is not an XY pair")
    try:
        return float(parts[0]), float(parts[1])
    except (TypeError, ValueError) as exc:
        raise T300Error(f"Klipper setting {name} contains non-numeric values") from exc


def open_level_geometry(client: Moonraker) -> dict[str, float]:
    response = client.post_json(
        "/printer/objects/query", {"objects": {"configfile": ["settings"]}}
    )
    try:
        settings = response["status"]["configfile"]["settings"]
    except (KeyError, TypeError) as exc:
        raise T300Error("Klipper did not return parsed configfile settings") from exc
    if not isinstance(settings, dict):
        raise T300Error("Klipper returned malformed configfile settings")
    required = {"bed_mesh", "probe", "stepper_x", "stepper_y", "stepper_z"}
    missing = sorted(required.difference(settings))
    if missing:
        raise T300Error(f"Open leveling macro requires config sections: {', '.join(missing)}")

    mesh_min = numeric_pair(settings["bed_mesh"].get("mesh_min"), "bed_mesh.mesh_min")
    mesh_max = numeric_pair(settings["bed_mesh"].get("mesh_max"), "bed_mesh.mesh_max")
    span_x = mesh_max[0] - mesh_min[0]
    span_y = mesh_max[1] - mesh_min[1]
    if span_x <= 40 or span_y <= 40:
        raise T300Error("Configured bed_mesh area is too small or invalid")
    edge_margin = max(5.0, span_x * 0.05)
    probe_left = mesh_min[0] + edge_margin
    probe_right = mesh_max[0] - edge_margin
    probe_y = (mesh_min[1] + mesh_max[1]) / 2.0

    probe = settings["probe"]
    x_offset = float(probe.get("x_offset", 0.0))
    y_offset = float(probe.get("y_offset", 0.0))
    nozzle_left = probe_left - x_offset
    nozzle_right = probe_right - x_offset
    nozzle_y = probe_y - y_offset

    stepper_x = settings["stepper_x"]
    stepper_y = settings["stepper_y"]
    x_min = float(stepper_x.get("position_min", 0.0))
    x_max = float(stepper_x["position_max"])
    y_min = float(stepper_y.get("position_min", 0.0))
    y_max = float(stepper_y["position_max"])
    rotation_distance = float(settings["stepper_z"]["rotation_distance"])
    if rotation_distance <= 0:
        raise T300Error("stepper_z.rotation_distance must be positive")
    if not (x_min <= nozzle_left < nozzle_right <= x_max and y_min <= nozzle_y <= y_max):
        raise T300Error("Calculated nozzle probe positions exceed the configured axis limits")

    return {
        "probe_left": probe_left,
        "probe_right": probe_right,
        "probe_y": probe_y,
        "nozzle_left": nozzle_left,
        "nozzle_right": nozzle_right,
        "nozzle_y": nozzle_y,
        "rotation_distance": rotation_distance,
    }


def config_permissions(client: Moonraker) -> str:
    try:
        roots = client.get_json("/server/files/roots")
    except T300Error as exc:
        if "HTTP 404" not in str(exc):
            raise
        files = client.get_json("/server/files/list?root=config")
        if not isinstance(files, list):
            raise T300Error("Moonraker returned an unexpected config file list") from exc
        for item in files:
            if isinstance(item, dict) and item.get("path") == "printer.cfg":
                return str(item.get("permissions", ""))
        raise T300Error(
            "Moonraker does not report permissions for printer.cfg"
        ) from exc

    if not isinstance(roots, list):
        raise T300Error("Moonraker returned an unexpected roots response")
    for root in roots:
        if isinstance(root, dict) and root.get("name") == "config":
            return str(root.get("permissions", ""))
    raise T300Error("Moonraker does not expose a config root")


def printer_info(client: Moonraker) -> dict[str, Any]:
    info = client.get_json("/printer/info")
    if not isinstance(info, dict):
        raise T300Error("Moonraker returned malformed printer information")
    return info


def ensure_idle_ready(client: Moonraker) -> None:
    info = printer_info(client)
    if info.get("state") != "ready":
        raise T300Error(f"Klipper is not ready: {info.get('state_message', info.get('state'))}")
    status = client.get_json("/printer/objects/query?print_stats=state,filename")
    try:
        state = status["status"]["print_stats"]["state"]
    except (KeyError, TypeError):
        state = None
    if state in {"printing", "paused"}:
        raise T300Error(f"Printer is currently {state}; configuration changes are blocked")


def wait_for_ready(client: Moonraker, seconds: float = 60.0) -> tuple[bool, str]:
    deadline = time.monotonic() + seconds
    last_message = "Printer did not respond"
    first_error_at: float | None = None
    while time.monotonic() < deadline:
        try:
            info = printer_info(client)
            state = str(info.get("state", "unknown"))
            last_message = str(info.get("state_message", state))
            if state == "ready":
                return True, last_message
            if state in {"error", "shutdown"}:
                first_error_at = first_error_at or time.monotonic()
                if time.monotonic() - first_error_at >= 3:
                    return False, last_message
            else:
                first_error_at = None
        except T300Error as exc:
            last_message = str(exc)
        time.sleep(1)
    return False, last_message


def wait_for_restart_ready(client: Moonraker, seconds: float = 60.0) -> tuple[bool, str]:
    """Wait for an observed Klippy restart transition followed by ready.

    A Klipper FIRMWARE_RESTART reuses the same host process, so process_id is
    not a restart marker. Requiring Moonraker to report a non-ready or
    disconnected state prevents a stale pre-restart ready response from being
    accepted as successful configuration validation.
    """
    deadline = time.monotonic() + seconds
    last_message = "Klippy restart transition was not observed"
    restart_observed = False
    first_error_at: float | None = None
    while time.monotonic() < deadline:
        try:
            server = client.get_json("/server/info")
            if not isinstance(server, dict):
                raise T300Error("Moonraker returned malformed server information")
            connected = bool(server.get("klippy_connected", False))
            state = str(server.get("klippy_state", "unknown"))
            last_message = f"Klippy state: {state}"
            if not connected or state != "ready":
                restart_observed = True
            if restart_observed and connected and state == "ready":
                info = printer_info(client)
                printer_state = str(info.get("state", "unknown"))
                last_message = str(info.get("state_message", printer_state))
                if printer_state == "ready":
                    return True, last_message
            if restart_observed and state in {"error", "shutdown"}:
                first_error_at = first_error_at or time.monotonic()
                if time.monotonic() - first_error_at >= 3:
                    return False, last_message
            else:
                first_error_at = None
        except T300Error as exc:
            restart_observed = True
            last_message = str(exc)
        time.sleep(0.25)
    return False, last_message


def firmware_restart_and_wait(
    client: Moonraker, seconds: float = 60.0
) -> tuple[bool, str]:
    client.post_json("/printer/firmware_restart")
    return wait_for_restart_ready(client, seconds)


def remote_config_paths(client: Moonraker) -> set[str]:
    files = client.get_json("/server/files/list?root=config")
    if not isinstance(files, list):
        raise T300Error("Moonraker returned an unexpected config file list")
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise T300Error("Moonraker returned malformed config file metadata")
        paths.add(str(validate_remote_path(item["path"])))
    return paths


def verify_remote_config(client: Moonraker, filename: str, expected: bytes) -> None:
    limit = MAX_MACRO_FILE if filename.endswith(".cfg") and filename != "printer.cfg" else MAX_CONFIG_FILE
    actual = client.download_bytes("config", filename, limit)
    if actual != expected:
        raise T300Error(f"Read-back verification failed for {filename}")


def apply_config_transaction(
    client: Moonraker,
    changes: dict[str, bytes],
    originals: dict[str, bytes | None],
) -> None:
    if not changes:
        return
    if not set(changes).issubset(originals):
        raise T300Error("Configuration transaction is missing original-file state")

    current_paths = remote_config_paths(client)
    for filename, expected in originals.items():
        exists = filename in current_paths
        if expected is None:
            if exists:
                raise T300Error(
                    f"{filename} appeared after review; refusing to overwrite concurrent changes"
                )
            continue
        if not exists:
            raise T300Error(f"{filename} disappeared after review; refusing to apply")
        verify_remote_config(client, filename, expected)

    # Recheck immediately before the first mutation. The higher-level installer
    # also checks, but review and backup work can take long enough for a print to
    # have started in another client.
    ensure_idle_ready(client)

    mutation_started = False
    restart_attempted = False
    try:
        for filename, content in changes.items():
            mutation_started = True
            client.upload_config(filename, content)
            verify_remote_config(client, filename, content)

        # Uploads only change files on disk; they do not reload Klipper. Check
        # every proposed byte again and repeat the idle check immediately before
        # the operation that can actually interrupt a print.
        for filename, content in changes.items():
            verify_remote_config(client, filename, content)
        ensure_idle_ready(client)
        restart_attempted = True
        ready, message = firmware_restart_and_wait(client)
        if not ready:
            raise T300Error(f"Klipper failed to become ready: {message}")
        return
    except (Exception, KeyboardInterrupt) as exc:
        if not mutation_started:
            raise
        if isinstance(exc, KeyboardInterrupt):
            original_error = T300Error("interrupted by user")
        else:
            original_error = exc if isinstance(exc, T300Error) else T300Error(str(exc))

    rollback_errors: list[str] = []
    for filename in reversed(tuple(changes)):
        original = originals[filename]
        try:
            if original is None:
                if filename in remote_config_paths(client):
                    client.delete_file("config", filename)
                if filename in remote_config_paths(client):
                    raise T300Error(f"Rollback could not remove newly created {filename}")
            else:
                client.upload_config(filename, original)
                verify_remote_config(client, filename, original)
        except (Exception, KeyboardInterrupt) as exc:
            rollback_errors.append(f"{filename}: {exc}")

    if restart_attempted:
        try:
            restored, restore_message = firmware_restart_and_wait(client)
            if not restored:
                rollback_errors.append(f"Klipper did not become ready: {restore_message}")
        except (Exception, KeyboardInterrupt) as exc:
            rollback_errors.append(f"restart: {exc}")

    if rollback_errors:
        raise T300Error(
            f"Apply failed ({original_error}); rollback was incomplete: "
            + "; ".join(rollback_errors)
        )
    raise T300Error(f"Apply failed ({original_error}); previous files were restored")


def print_preflight(client: Moonraker) -> None:
    access = client.get_json("/access/info")
    server = client.get_json("/server/info")
    info = printer_info(client)
    permissions = config_permissions(client)
    files = client.get_json("/server/files/list?root=config")
    names = sorted(item["path"] for item in files if isinstance(item, dict) and "path" in item)

    print(f"Moonraker:       {client.base_url}")
    print(f"Klipper state:  {info.get('state')} — {info.get('state_message', '')}")
    print(f"Klipper build:  {info.get('software_version', 'unknown')}")
    print(f"Moonraker:      {server.get('moonraker_version', 'version not reported')}")
    print(f"Trusted client: {access.get('trusted', 'unknown')}")
    print(f"Config access:  {permissions or 'none'}")
    print("Config files:")
    for name in names:
        print(f"  {name}")


def install_config_macro(
    client: Moonraker,
    macro: bytes,
    remote_filename: str,
    apply: bool,
    output: Path | None,
    patcher: Callable[[str], tuple[str, bool]] | None = None,
    allow_macro_update: bool = False,
) -> None:
    ensure_idle_ready(client)
    if "w" not in config_permissions(client):
        raise T300Error("Moonraker's config root is read-only")

    names = macro_names(macro)
    original = client.download_bytes("config", "printer.cfg", MAX_CONFIG_FILE)
    try:
        original_text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise T300Error("Remote printer.cfg is not valid UTF-8") from exc
    if remote_filename in {GERGO_MACRO_FILENAME, OPEN_MACRO_FILENAME}:
        validate_leveling_exclusivity(original_text, remote_filename)
    if patcher is None:
        proposed_text, changed = patch_printer_cfg(original_text, remote_filename)
    else:
        proposed_text, changed = patcher(original_text)
    proposed = proposed_text.encode("utf-8")

    files = client.get_json("/server/files/list?root=config")
    remote_macro = next(
        (item for item in files if isinstance(item, dict) and item.get("path") == remote_filename),
        None,
    )
    existing: bytes | None = None
    if remote_macro is not None:
        existing = client.download_bytes("config", remote_filename, MAX_MACRO_FILE)
        if existing != macro and not allow_macro_update:
            raise T300Error(f"A different {remote_filename} already exists; refusing to overwrite it")
    elif not changed:
        raise T300Error(
            f"printer.cfg includes {remote_filename}, but that file is missing from the config root"
        )

    print("Macro sections:")
    for name in names:
        print(f"  {name}")
    if changed:
        diff = difflib.unified_diff(
            original_text.splitlines(),
            proposed_text.splitlines(),
            fromfile="printer.cfg (current)",
            tofile="printer.cfg (proposed)",
            lineterm="",
        )
        print("\n" + "\n".join(diff))
    else:
        print(f"\n[include {remote_filename}] is already present in printer.cfg")
    if existing is not None and existing != macro:
        try:
            old_macro_text = existing.decode("utf-8")
            new_macro_text = macro.decode("utf-8")
        except UnicodeDecodeError:
            print(f"\n{remote_filename} will be replaced with the bundled text configuration")
        else:
            macro_diff = difflib.unified_diff(
                old_macro_text.splitlines(),
                new_macro_text.splitlines(),
                fromfile=f"{remote_filename} (current)",
                tofile=f"{remote_filename} (proposed)",
                lineterm="",
            )
            print("\n" + "\n".join(macro_diff))

    if not apply:
        print("\nDry run only. Re-run with --apply after reviewing the output.")
        return

    changes: dict[str, bytes] = {}
    if existing != macro:
        changes[remote_filename] = macro
    if changed:
        changes["printer.cfg"] = proposed
    if not changes:
        print("\nNo configuration bytes differ; no upload or restart is needed.")
        return

    backup = make_backup(client, output)
    print(f"\nConfiguration backup: {backup}")
    (backup / "proposed-printer.cfg").write_bytes(proposed)
    (backup / "supplied-macro.sha256").write_text(
        hashlib.sha256(macro).hexdigest() + f"  {remote_filename}\n", encoding="utf-8"
    )
    originals = {"printer.cfg": original, remote_filename: existing}
    apply_config_transaction(client, changes, originals)
    print("Klipper restarted successfully and reports ready.")
    print("Uploaded files passed byte-for-byte read-back verification.")
    print("Test the new macro from Mainsail before relying on the touchscreen shortcut.")


def install_gergo_macro(
    client: Moonraker, source: Path, apply: bool, output: Path | None
) -> None:
    macro = read_macro(source)
    install_config_macro(client, macro, GERGO_MACRO_FILENAME, apply, output)


def validate_kamp_compatibility(
    printer_cfg: str, factory_macros: str, version: str
) -> None:
    if not version.startswith("v0.12.0"):
        raise T300Error(f"KAMP subset is pinned to the T300 Klipper 0.12.0 build, found {version}")
    if not has_config_include(printer_cfg, GERGO_MACRO_FILENAME):
        raise T300Error("The selected GerGo T300 leveling include is missing")
    if has_config_include(printer_cfg, CORE_MACRO_FILENAME):
        raise T300Error("The quarantined t300_core.cfg include must remain disabled")
    if not re.search(r"(?mi)^\s*\[exclude_object\]\s*$", printer_cfg):
        raise T300Error("printer.cfg must define [exclude_object] for object-aware parking and purge")
    mesh = config_section(factory_macros, "gcode_macro", "BED_MESH_CALIBRATE")
    if mesh is None or "rename_existing: BED_MESH_CALIBRATE_BASE" not in mesh:
        raise T300Error("The reviewed T300 BED_MESH_CALIBRATE wrapper was not found")
    if "BED_MESH_CALIBRATE_BASE ADAPTIVE=1" not in mesh:
        raise T300Error("The T300 native adaptive-mesh call was not found")
    extruder = config_section(printer_cfg, "extruder")
    match = re.search(r"(?mi)^\s*max_extrude_cross_section\s*:\s*([0-9.]+)", extruder or "")
    if match is None or float(match.group(1)) < 5:
        raise T300Error("KAMP Line Purge requires max_extrude_cross_section of at least 5")


def install_kamp_subset(client: Moonraker, apply: bool, output: Path | None) -> None:
    ensure_idle_ready(client)
    printer_cfg = client.download_bytes("config", "printer.cfg", MAX_CONFIG_FILE).decode("utf-8")
    factory_macros = client.download_bytes("config", "Macro.cfg", MAX_CONFIG_FILE).decode("utf-8")
    version = str(printer_info(client).get("software_version", ""))
    validate_kamp_compatibility(printer_cfg, factory_macros, version)
    macro = read_kamp_macro()
    print("T300 KAMP subset compatibility check: PASS")
    print("  native T300 adaptive mesh remains the only BED_MESH_CALIBRATE owner")
    print(
        "  installs only SMART_PARK and LINE_PURGE; stationary tip advance is "
        f"disabled ({KAMP_TIP_DISTANCE} mm) and purge margin is {KAMP_PURGE_MARGIN} mm"
    )
    print("  factory motion, heater, extrusion, calibration, and lifecycle values stay unchanged\n")
    install_config_macro(
        client,
        macro,
        KAMP_MACRO_FILENAME,
        apply,
        output,
        allow_macro_update=True,
    )


def install_core_macro(
    client: Moonraker,
    apply: bool,
    output: Path | None,
    acknowledge_plr_quarantine: bool,
) -> None:
    raise T300Error(
        "install-core is quarantined: t300_core.cfg is locally authored and has not "
        "met the owner's requirement for T300-specific community approval. No files "
        "were uploaded and the printer was not restarted."
    )


def install_runtime_proposal(client: Moonraker, apply: bool, output: Path | None) -> None:
    raise T300Error(
        "install-runtime is quarantined pending owner review. No printer files were read, "
        "uploaded, or restarted. Use bin/prepare-runtime-proposal.py on a saved backup."
    )


def install_open_level_macro(client: Moonraker, apply: bool, output: Path | None) -> None:
    ensure_idle_ready(client)
    geometry = open_level_geometry(client)
    print("Live T300 geometry check:")
    print(
        "  Probe points: "
        f"({geometry['probe_right']:.2f}, {geometry['probe_y']:.2f}) right, "
        f"({geometry['probe_left']:.2f}, {geometry['probe_y']:.2f}) left"
    )
    print(
        "  Nozzle moves: "
        f"({geometry['nozzle_right']:.2f}, {geometry['nozzle_y']:.2f}) right, "
        f"({geometry['nozzle_left']:.2f}, {geometry['nozzle_y']:.2f}) left"
    )
    print(f"  Z screw rotation distance: {geometry['rotation_distance']:.3f} mm/revolution\n")
    install_config_macro(client, read_open_macro(), OPEN_MACRO_FILENAME, apply, output)


def discover_one(address: str, timeout: float) -> tuple[str, dict[str, Any]] | None:
    try:
        client = Moonraker(address, timeout=timeout)
        info = client.get_json("/server/info")
        if isinstance(info, dict) and ("klippy_state" in info or "components" in info):
            return address, info
    except T300Error:
        return None
    return None


def discover(subnet: str, timeout: float) -> list[tuple[str, dict[str, Any]]]:
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4 or network.num_addresses > 256:
        raise T300Error("Discovery is limited to one IPv4 /24 or smaller")
    addresses = [str(host) for host in network.hosts()]
    results: list[tuple[str, dict[str, Any]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(discover_one, address, timeout) for address in addresses]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    return sorted(results, key=lambda item: ipaddress.ip_address(item[0]))


def api_key_from_args(args: argparse.Namespace) -> str | None:
    if getattr(args, "api_key_env", None):
        value = os.environ.get(args.api_key_env)
        if not value:
            raise T300Error(f"Environment variable {args.api_key_env} is empty or unset")
        return value
    return None


def add_host_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, help="T300 IP address or base URL")
    parser.add_argument(
        "--api-key-env",
        metavar="NAME",
        help="read a Moonraker API key from environment variable NAME",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="find Moonraker on the private link")
    discover_parser.add_argument("--subnet", default=DEFAULT_SUBNET)
    discover_parser.add_argument("--timeout", type=float, default=0.6)

    check_parser = subparsers.add_parser("check", help="inspect printer and config access")
    add_host_args(check_parser)

    backup_parser = subparsers.add_parser("backup", help="download the complete config root")
    add_host_args(backup_parser)
    backup_parser.add_argument("--output", type=Path)

    verify_parser = subparsers.add_parser(
        "verify-backup", help="verify a local backup's SHA-256 manifest"
    )
    verify_parser.add_argument("path", type=Path)

    open_level_parser = subparsers.add_parser(
        "install-open-level",
        help="safely stage or install the bundled open-source T300 leveling macro",
    )
    add_host_args(open_level_parser)
    open_level_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    open_level_parser.add_argument(
        "--apply", action="store_true", help="perform uploads and restart after backing up"
    )

    install_parser = subparsers.add_parser(
        "install-gergo", help="optionally install a user-supplied GerGo v3 macro"
    )
    add_host_args(install_parser)
    install_parser.add_argument(
        "--source", type=Path, required=True, help="downloaded Cults ZIP, inner ZIP, or CFG"
    )
    install_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    install_parser.add_argument(
        "--apply", action="store_true", help="perform uploads and restart after backing up"
    )

    core_parser = subparsers.add_parser(
        "install-core",
        help="quarantined local overlay; retained only for source review",
    )
    add_host_args(core_parser)
    core_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    core_parser.add_argument(
        "--apply", action="store_true", help="perform uploads and restart after backing up"
    )
    core_parser.add_argument(
        "--acknowledge-plr-quarantine",
        action="store_true",
        help="confirm that factory power-loss resume will remain disabled",
    )
    runtime_parser = subparsers.add_parser(
        "install-runtime",
        help="quarantined preliminary lifecycle proposal; cannot contact the printer",
    )
    add_host_args(runtime_parser)
    runtime_parser.add_argument("--output", type=Path)
    runtime_parser.add_argument("--apply", action="store_true")
    kamp_parser = subparsers.add_parser(
        "install-kamp-subset",
        help="install pinned KAMP Smart Park and Line Purge without KAMP meshing",
    )
    add_host_args(kamp_parser)
    kamp_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    kamp_parser.add_argument(
        "--apply", action="store_true", help="perform uploads and restart after backing up"
    )
    clean_parser = subparsers.add_parser(
        "set-end-clean-height",
        help="quarantined standalone patch; use the whole runtime proposal instead",
    )
    add_host_args(clean_parser)
    clean_parser.add_argument(
        "--height", type=float, default=DEFAULT_END_CLEAN_HEIGHT, help="minimum final Z height"
    )
    clean_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    clean_parser.add_argument(
        "--apply", action="store_true", help="retained for compatibility; command is blocked"
    )

    image_parser = subparsers.add_parser(
        "image", help="inspect, capture, verify, or restore eMMC from a marked recovery USB"
    )
    image_subparsers = image_parser.add_subparsers(dest="image_command", required=True)

    def add_recovery_ssh_args(image_command_parser: argparse.ArgumentParser) -> None:
        image_command_parser.add_argument(
            "--identity-file",
            type=Path,
            required=True,
            help="private key dedicated to the marked recovery USB",
        )
        image_command_parser.add_argument(
            "--known-hosts",
            type=Path,
            required=True,
            help="known-hosts file whose recovery host key was checked over USB-C",
        )

    image_inspect = image_subparsers.add_parser(
        "inspect", help="inspect read-only recovery and eMMC safety gates"
    )
    image_inspect.add_argument("--host", required=True, help="root SSH host for recovery USB")
    add_recovery_ssh_args(image_inspect)
    image_inspect.add_argument("--device", required=True, help="whole eMMC device, e.g. /dev/mmcblk2")
    image_inspect.add_argument(
        "--record-boot", action="store_true", help="record this distinct verified USB boot"
    )
    image_inspect.add_argument("--apply", action="store_true")
    image_inspect.add_argument("--confirm")

    image_capture = image_subparsers.add_parser(
        "capture", help="stream eMMC over SSH, compress it, and verify a second device hash"
    )
    image_capture.add_argument("--host", required=True)
    add_recovery_ssh_args(image_capture)
    image_capture.add_argument("--device", required=True)
    image_capture.add_argument("--output", type=Path, required=True)
    image_capture.add_argument("--manifest", type=Path)

    image_verify = image_subparsers.add_parser(
        "verify", help="verify image hashes and optionally inspect filesystems read-only"
    )
    image_verify.add_argument("--image", type=Path, required=True)
    image_verify.add_argument("--manifest", type=Path, required=True)
    image_verify.add_argument(
        "--filesystem-check",
        action="store_true",
        help="as laptop root, use a read-only loop device, fsck -n, and read-only mounts",
    )
    image_verify.add_argument(
        "--workspace",
        type=Path,
        help="temporary raw-image workspace; defaults to the image directory",
    )

    image_write = image_subparsers.add_parser(
        "write", help="restore a verified image to its matching unmounted eMMC"
    )
    image_write.add_argument("--host", required=True)
    add_recovery_ssh_args(image_write)
    image_write.add_argument("--device", required=True)
    image_write.add_argument("--image", type=Path, required=True)
    image_write.add_argument("--manifest", type=Path, required=True)
    image_write.add_argument("--apply", action="store_true")
    image_write.add_argument("--confirm")
    return parser


def run_image_command(args: argparse.Namespace) -> None:
    if args.image_command == "verify":
        if args.workspace is not None and not args.filesystem_check:
            raise ImagingError("--workspace is only valid with --filesystem-check")
        if args.filesystem_check:
            workspace = args.workspace or args.image.expanduser().absolute().parent
            result = verify_image_filesystems(
                args.image, args.manifest, workspace
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        manifest = verify_image(args.image, args.manifest)
        print(
            "Image hash verification passed: %d raw bytes, SHA-256 %s. "
            "Run again as root with --filesystem-check before accepting recovery."
            % (manifest["raw_size"], manifest["raw_sha256"])
        )
        return

    recovery = RecoveryClient(args.host, args.identity_file, args.known_hosts)
    if args.image_command == "inspect":
        result = recovery.inspect(
            args.device,
            record_boot=args.record_boot,
            apply=args.apply,
            confirmation=args.confirm,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.image_command == "capture":
        manifest_path = capture_image(
            recovery, args.device, args.output, args.manifest
        )
        print(
            "Hash-verified eMMC capture and manifest written to %s; "
            "run image verify --filesystem-check before accepting recovery."
            % (manifest_path,)
        )
    elif args.image_command == "write":
        result = write_image(
            recovery,
            args.device,
            args.image,
            args.manifest,
            args.apply,
            args.confirm,
        )
        print(
            "Restore and full-device verification passed: %s"
            % (result["sha256"],)
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            results = discover(args.subnet, args.timeout)
            if not results:
                print("No Moonraker host found. Check the cable and T300 Show IP screen.")
                return 1
            for address, info in results:
                print(f"{address}  Klipper={info.get('klippy_state', 'unknown')}")
            return 0
        if args.command == "verify-backup":
            checked = verify_backup(args.path)
            print(f"Backup verification passed: {checked} files")
            return 0
        if args.command == "image":
            run_image_command(args)
            return 0

        client = Moonraker(args.host, api_key=api_key_from_args(args))
        if args.command == "check":
            print_preflight(client)
        elif args.command == "backup":
            destination = make_backup(client, args.output)
            print(f"Configuration backup written to {destination}")
        elif args.command == "install-open-level":
            install_open_level_macro(client, args.apply, args.output)
        elif args.command == "install-gergo":
            install_gergo_macro(client, args.source, args.apply, args.output)
        elif args.command == "install-core":
            install_core_macro(
                client,
                args.apply,
                args.output,
                args.acknowledge_plr_quarantine,
            )
        elif args.command == "install-runtime":
            install_runtime_proposal(client, args.apply, args.output)
        elif args.command == "install-kamp-subset":
            install_kamp_subset(client, args.apply, args.output)
        elif args.command == "set-end-clean-height":
            set_end_clean_height(client, args.height, args.apply, args.output)
        return 0
    except (T300Error, ImagingError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

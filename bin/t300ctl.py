#!/usr/bin/env python3
"""Safe, dependency-free helper for a directly connected T300/Moonraker host."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import difflib
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sys
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


MAX_CONFIG_FILE = 20 * 1024 * 1024
MAX_CONFIG_TOTAL = 250 * 1024 * 1024
MAX_MACRO_FILE = 2 * 1024 * 1024
DEFAULT_SUBNET = "10.42.42.0/24"
INCLUDE_LINE = "[include macro_z_tilt_via_knob.cfg]"
MACRO_FILENAME = "macro_z_tilt_via_knob.cfg"


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
        checksums.append(f"{digest}  {remote}")

    manifest = {
        "created": dt.datetime.now().astimezone().isoformat(),
        "source": client.base_url,
        "declared_bytes": declared_total,
        "downloaded_bytes": actual_total,
        "files": [item for _, _, item in normalized],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (target / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return target


def patch_printer_cfg(text: str) -> tuple[str, bool]:
    include_pattern = re.compile(
        r"^\s*\[include\s+macro_z_tilt_via_knob\.cfg\]\s*(?:#.*)?$", re.IGNORECASE
    )
    macro_pattern = re.compile(r"^\s*\[include\s+Macro\.cfg\]\s*(?:#.*)?$", re.IGNORECASE)
    lines = text.splitlines(keepends=True)
    if any(include_pattern.match(line.rstrip("\r\n")) for line in lines):
        return text, False

    for index, line in enumerate(lines):
        if macro_pattern.match(line.rstrip("\r\n")):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines.insert(index + 1, INCLUDE_LINE + newline)
            return "".join(lines), True
    raise T300Error("printer.cfg does not contain the expected [include Macro.cfg] line")


def read_macro(source: Path) -> bytes:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise T300Error(f"Macro source does not exist: {source}")
    if source.suffix.lower() == ".cfg":
        content = source.read_bytes()
    elif source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            matches = [
                info
                for info in archive.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).name == MACRO_FILENAME
            ]
            if len(matches) != 1:
                raise T300Error(
                    f"Expected exactly one {MACRO_FILENAME} in the ZIP; found {len(matches)}"
                )
            info = matches[0]
            if info.file_size > MAX_MACRO_FILE:
                raise T300Error("Macro in ZIP exceeds the 2 MiB safety limit")
            content = archive.read(info)
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


def macro_names(content: bytes) -> list[str]:
    text = content.decode("utf-8")
    return re.findall(r"^\s*\[gcode_macro\s+([^]]+)\]", text, re.MULTILINE | re.IGNORECASE)


def config_permissions(client: Moonraker) -> str:
    roots = client.get_json("/server/files/roots")
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


def install_macro(client: Moonraker, source: Path, apply: bool, output: Path | None) -> None:
    ensure_idle_ready(client)
    if "w" not in config_permissions(client):
        raise T300Error("Moonraker's config root is read-only")

    macro = read_macro(source)
    names = macro_names(macro)
    original = client.download_bytes("config", "printer.cfg", MAX_CONFIG_FILE)
    try:
        original_text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise T300Error("Remote printer.cfg is not valid UTF-8") from exc
    proposed_text, changed = patch_printer_cfg(original_text)
    proposed = proposed_text.encode("utf-8")

    files = client.get_json("/server/files/list?root=config")
    remote_macro = next(
        (item for item in files if isinstance(item, dict) and item.get("path") == MACRO_FILENAME), None
    )
    if remote_macro is not None:
        existing = client.download_bytes("config", MACRO_FILENAME, MAX_MACRO_FILE)
        if existing != macro:
            raise T300Error(
                f"A different {MACRO_FILENAME} already exists; refusing to overwrite it"
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
        print(f"\n{INCLUDE_LINE} is already present in printer.cfg")

    if not apply:
        print("\nDry run only. Re-run with --apply after reviewing the output.")
        return

    backup = make_backup(client, output)
    print(f"\nConfiguration backup: {backup}")
    (backup / "proposed-printer.cfg").write_bytes(proposed)
    (backup / "supplied-macro.sha256").write_text(
        hashlib.sha256(macro).hexdigest() + f"  {MACRO_FILENAME}\n", encoding="utf-8"
    )

    client.upload_config(MACRO_FILENAME, macro)
    if changed:
        client.upload_config("printer.cfg", proposed)
    client.post_json("/printer/firmware_restart")
    ready, message = wait_for_ready(client)
    if ready:
        print("Klipper restarted successfully and reports ready.")
        print("Test the new macro from Mainsail before relying on the touchscreen shortcut.")
        return

    print(f"Klipper failed to become ready: {message}", file=sys.stderr)
    print("Restoring the original printer.cfg automatically...", file=sys.stderr)
    client.upload_config("printer.cfg", original)
    try:
        client.post_json("/printer/firmware_restart")
    except T300Error:
        time.sleep(2)
        client.post_json("/printer/firmware_restart")
    restored, restore_message = wait_for_ready(client)
    if restored:
        raise T300Error(
            "The macro configuration failed, but printer.cfg was restored and Klipper is ready again"
        )
    raise T300Error(
        "Automatic rollback was uploaded, but Klipper is not ready. "
        f"Open Mainsail and restore {backup / 'config-root' / 'printer.cfg'}. "
        f"Last response: {restore_message}"
    )


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

    install_parser = subparsers.add_parser(
        "install-gergo", help="safely stage or install a user-supplied GerGo v3 macro"
    )
    add_host_args(install_parser)
    install_parser.add_argument("--source", type=Path, required=True, help="purchased ZIP or CFG")
    install_parser.add_argument("--output", type=Path, help="backup destination used with --apply")
    install_parser.add_argument(
        "--apply", action="store_true", help="perform uploads and restart after backing up"
    )
    return parser


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

        client = Moonraker(args.host, api_key=api_key_from_args(args))
        if args.command == "check":
            print_preflight(client)
        elif args.command == "backup":
            destination = make_backup(client, args.output)
            print(f"Configuration backup written to {destination}")
        elif args.command == "install-gergo":
            install_macro(client, args.source, args.apply, args.output)
        return 0
    except (T300Error, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

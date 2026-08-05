"""Bounded, key-only configuration-bundle transport for a T300 candidate."""

from __future__ import annotations

import argparse
import base64
import binascii
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO, Iterable


MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_HEADER_BYTES = 4096
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
ALLOWED_KEY_TYPES = {"ssh-ed25519", "sk-ssh-ed25519@openssh.com"}
INCOMING_DIRECTORY = Path("/var/lib/t300/incoming")
INCOMING_BUNDLE = "config-bundle.tar"
DEPLOY_USER = "t300-deploy"


class TransferError(RuntimeError):
    pass


def _decode_ssh_string(blob: bytes, offset: int = 0) -> tuple[bytes, int]:
    if len(blob) - offset < 4:
        raise TransferError("SSH public key blob is truncated")
    length = struct.unpack(">I", blob[offset : offset + 4])[0]
    offset += 4
    if length > len(blob) - offset:
        raise TransferError("SSH public key field is truncated")
    return blob[offset : offset + length], offset + length


def validate_public_key(path: Path) -> dict[str, str]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise TransferError("deployment public key must be one small regular file")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TransferError("deployment public key is not readable ASCII") from exc
    if len(lines) != 1 or not lines[0].strip():
        raise TransferError("deployment public key must contain exactly one key")
    fields = lines[0].split()
    if len(fields) < 2 or fields[0] not in ALLOWED_KEY_TYPES:
        raise TransferError("deployment key must be one Ed25519 OpenSSH public key")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransferError("deployment public key payload is invalid") from exc
    encoded_type, offset = _decode_ssh_string(blob)
    try:
        embedded_type = encoded_type.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TransferError("deployment public key type is invalid") from exc
    key_bytes, offset = _decode_ssh_string(blob, offset)
    if embedded_type != fields[0] or len(key_bytes) != 32:
        raise TransferError("deployment public key blob does not match its type")
    if embedded_type == "sk-ssh-ed25519@openssh.com":
        _application, offset = _decode_ssh_string(blob, offset)
    if offset != len(blob):
        raise TransferError("deployment public key blob has trailing fields")
    canonical = "%s %s t300-deploy" % (fields[0], fields[1])
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()
    ).decode("ascii").rstrip("=")
    return {"key": canonical, "fingerprint": fingerprint}


def _validate_incoming_directory(path: Path, strict_owner: bool) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise TransferError("incoming quarantine is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TransferError("incoming quarantine is not a real directory")
    if stat.S_IMODE(info.st_mode) != 0o730:
        raise TransferError("incoming quarantine must have mode 0730")
    if strict_owner:
        expected_group = grp.getgrnam(DEPLOY_USER).gr_gid
        if info.st_uid != 0 or info.st_gid != expected_group:
            raise TransferError("incoming quarantine ownership is unsafe")


def receive_bundle(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    incoming: Path = INCOMING_DIRECTORY,
    *,
    strict_owner: bool = True,
    original_command: str | None = None,
) -> dict[str, Any]:
    if original_command:
        raise TransferError("remote commands are not accepted")
    _validate_incoming_directory(incoming, strict_owner)
    header_line = input_stream.readline(MAX_HEADER_BYTES + 1)
    if not header_line or len(header_line) > MAX_HEADER_BYTES or not header_line.endswith(b"\n"):
        raise TransferError("upload header is missing or too large")
    try:
        header = json.loads(header_line.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferError("upload header is malformed") from exc
    if not isinstance(header, dict) or set(header) != {
        "schema_version",
        "operation",
        "sha256",
        "size",
    }:
        raise TransferError("upload header fields are not exact")
    size = header["size"]
    expected_sha256 = header["sha256"]
    if (
        header["schema_version"] != 1
        or header["operation"] != "upload-config"
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > MAX_BUNDLE_BYTES
        or not isinstance(expected_sha256, str)
        or SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise TransferError("upload request is outside policy")

    target = incoming / INCOMING_BUNDLE
    descriptor = -1
    written = 0
    digest = hashlib.sha256()
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        while written < size:
            block = input_stream.read(min(1024 * 1024, size - written))
            if not block:
                raise TransferError("upload ended before its declared size")
            digest.update(block)
            view = memoryview(block)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise TransferError("quarantine write made no progress")
                view = view[count:]
            written += len(block)
        if input_stream.read(1):
            raise TransferError("upload contains bytes beyond its declared size")
        os.fsync(descriptor)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise TransferError("uploaded bundle hash does not match its header")
        os.close(descriptor)
        descriptor = -1
        response = {
            "schema_version": 1,
            "state": "quarantined",
            "path": str(target),
            "size": written,
            "sha256": actual,
            "services_started": [],
        }
        output_stream.write(
            (json.dumps(response, sort_keys=True) + "\n").encode("ascii")
        )
        output_stream.flush()
        return response
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise


def _host_key_lines(host: str, port: int) -> list[str]:
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-T", "5", "-p", str(port), "-t", "ed25519", host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransferError("could not inspect the candidate SSH host key") from exc
    lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if result.returncode or not lines:
        raise TransferError("candidate did not provide an Ed25519 SSH host key")
    return lines


def _line_fingerprint(line: str) -> str:
    fields = line.split()
    if len(fields) < 3 or fields[1] != "ssh-ed25519":
        raise TransferError("candidate host key response is malformed")
    try:
        blob = base64.b64decode(fields[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransferError("candidate host key payload is malformed") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode(
        "ascii"
    ).rstrip("=")


def send_bundle(
    host: str,
    port: int,
    private_key: Path,
    expected_host_fingerprint: str,
    bundle: Path,
) -> dict[str, Any]:
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise TransferError("candidate host must be a literal IP address") from exc
    if port <= 0 or port > 65535:
        raise TransferError("SSH port is invalid")
    if HOST_FINGERPRINT_RE.fullmatch(expected_host_fingerprint) is None:
        raise TransferError("candidate host fingerprint is malformed")
    private_key = private_key.expanduser()
    if private_key.is_symlink() or not private_key.is_file():
        raise TransferError("deployment private key must be one regular file")
    if private_key.stat().st_mode & 0o077:
        raise TransferError("deployment private key must not be accessible to other users")
    if bundle.is_symlink() or not bundle.is_file():
        raise TransferError("configuration bundle must be one regular file")
    payload = bundle.read_bytes()
    if not payload or len(payload) > MAX_BUNDLE_BYTES:
        raise TransferError("configuration bundle is outside its transport size limit")
    digest = hashlib.sha256(payload).hexdigest()
    lines = _host_key_lines(host, port)
    accepted = [line for line in lines if _line_fingerprint(line) == expected_host_fingerprint]
    if len(accepted) != 1:
        raise TransferError("candidate SSH host key does not match the reviewed fingerprint")
    header = (
        json.dumps(
            {
                "schema_version": 1,
                "operation": "upload-config",
                "sha256": digest,
                "size": len(payload),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    with tempfile.TemporaryDirectory(prefix="t300-known-host-") as directory:
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text(accepted[0] + "\n", encoding="ascii")
        known_hosts.chmod(0o600)
        command = [
            "ssh",
            "-T",
            "-p",
            str(port),
            "-i",
            str(private_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=%s" % known_hosts,
            "-o",
            "ConnectTimeout=10",
            "%s@%s" % (DEPLOY_USER, host),
        ]
        try:
            result = subprocess.run(
                command,
                input=header + payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransferError("restricted SSH upload failed to execute") from exc
    if result.returncode:
        raise TransferError(
            "restricted SSH upload failed: %s"
            % result.stderr.decode("utf-8", "replace")[-1000:].strip()
        )
    try:
        response = json.loads(result.stdout.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferError("candidate upload response is malformed") from exc
    if (
        not isinstance(response, dict)
        or response.get("state") != "quarantined"
        or response.get("sha256") != digest
        or response.get("size") != len(payload)
    ):
        raise TransferError("candidate did not verify the uploaded bundle")
    return response


def receive_main() -> int:
    try:
        if os.geteuid() == 0 or pwd.getpwuid(os.geteuid()).pw_name != DEPLOY_USER:
            raise TransferError("receiver must run only as the deployment account")
        result = receive_bundle(
            sys.stdin.buffer,
            sys.stdout.buffer,
            original_command=os.environ.get("SSH_ORIGINAL_COMMAND"),
        )
    except (OSError, TransferError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    return 0 if result["state"] == "quarantined" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--host-key-fingerprint", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = send_bundle(
            args.host,
            args.port,
            args.private_key,
            args.host_key_fingerprint,
            args.bundle,
        )
    except (OSError, TransferError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

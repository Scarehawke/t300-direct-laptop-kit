"""Validation helpers for owner-supplied, non-redistributable configuration."""

from __future__ import annotations

import io
from pathlib import Path, PurePosixPath
import re
import zipfile

from .lockfile import sha256_file


GERGO_OUTER_SHA256 = (
    "c4af725ece0ccb4cc2757fbe8d76150a5018a0c0a6a9c7715c5461b8ad5ab64e"
)
GERGO_MACRO_FILENAME = "macro_z_tilt_via_knob.cfg"
GERGO_NESTED_FILENAME = "macro_v3(extract!).zip"
MAX_OUTER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MACRO_BYTES = 2 * 1024 * 1024
FORBIDDEN_PRIVATE_COMMANDS = (
    "ACTIVATE_EXTRUDER",
    "FIRMWARE_RESTART",
    "FORCE_MOVE",
    "M112",
    "RESTART",
    "RUN_SHELL_COMMAND",
    "SAVE_CONFIG",
    "SET_DIGIPOT",
    "SET_HEATER_TEMPERATURE",
    "SET_KINEMATIC_POSITION",
    "SET_PIN",
    "SET_TMC_CURRENT",
    "SET_TMC_FIELD",
    "SHUTDOWN",
    "UPDATE_GIT_REPO",
)


class PrivateConfigError(RuntimeError):
    pass


def _validate_zip(archive: zipfile.ZipFile, description: str) -> None:
    members = archive.infolist()
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise PrivateConfigError("%s has an invalid member count" % description)
    expanded = 0
    seen: set[str] = set()
    for member in members:
        path = PurePosixPath(member.filename)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise PrivateConfigError("%s contains an unsafe path" % description)
        normalized = path.as_posix()
        if normalized in seen:
            raise PrivateConfigError("%s contains a duplicate path" % description)
        seen.add(normalized)
        if member.flag_bits & 0x1:
            raise PrivateConfigError("%s contains encrypted material" % description)
        expanded += member.file_size
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PrivateConfigError("%s expands beyond its safety limit" % description)


def _one_named_file(
    archive: zipfile.ZipFile, filename: str, description: str
) -> zipfile.ZipInfo:
    matches = [
        member
        for member in archive.infolist()
        if not member.is_dir()
        and PurePosixPath(member.filename).name.lower() == filename.lower()
    ]
    if len(matches) != 1:
        raise PrivateConfigError(
            "%s must contain exactly one required private file" % description
        )
    return matches[0]


def _validate_macro(content: bytes) -> bytes:
    if len(content) > MAX_MACRO_BYTES or b"\x00" in content:
        raise PrivateConfigError("private macro is not a bounded text file")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrivateConfigError("private macro is not valid UTF-8") from exc
    sections = re.findall(r"(?m)^\s*\[([^]\r\n]+)\]\s*(?:#.*)?$", text)
    if len(sections) != 3 or any(
        re.fullmatch(r"gcode_macro\s+\S+", section, re.IGNORECASE) is None
        for section in sections
    ):
        raise PrivateConfigError(
            "private macro does not have the reviewed three-macro structure"
        )
    if len({section.lower() for section in sections}) != len(sections):
        raise PrivateConfigError("private macro repeats a section")
    if re.search(r"(?mi)^\s*rename_existing\s*:", text):
        raise PrivateConfigError("private macro may not replace an existing command")
    for command in FORBIDDEN_PRIVATE_COMMANDS:
        if re.search(r"(?mi)^\s*%s\b" % re.escape(command), text):
            raise PrivateConfigError(
                "private macro contains a command outside the maintenance policy"
            )
    stepper_lines = re.findall(r"(?mi)^\s*SET_STEPPER_ENABLE\b[^\r\n]*", text)
    if any(
        " ".join(line.split()).upper()
        != "SET_STEPPER_ENABLE STEPPER=STEPPER_Z ENABLE=0"
        for line in stepper_lines
    ):
        raise PrivateConfigError(
            "private macro may only release the reviewed Z stepper in maintenance mode"
        )
    return content


def load_purchased_gergo(source: Path) -> bytes:
    source = source.expanduser()
    if source.is_symlink() or not source.is_file():
        raise PrivateConfigError("private GerGo source must be one regular file")
    source = source.resolve(strict=True)
    if source.suffix.lower() != ".zip":
        raise PrivateConfigError("mainline staging requires the untouched outer ZIP")
    if source.stat().st_size > MAX_OUTER_BYTES:
        raise PrivateConfigError("private GerGo archive exceeds its size limit")
    if sha256_file(source) != GERGO_OUTER_SHA256:
        raise PrivateConfigError("private GerGo archive hash is not the purchased package")
    try:
        with zipfile.ZipFile(source) as outer:
            _validate_zip(outer, "private GerGo outer archive")
            nested = _one_named_file(
                outer, GERGO_NESTED_FILENAME, "private GerGo outer archive"
            )
            if nested.file_size > MAX_MACRO_BYTES:
                raise PrivateConfigError("private GerGo nested archive is too large")
            nested_bytes = outer.read(nested)
        with zipfile.ZipFile(io.BytesIO(nested_bytes)) as inner:
            _validate_zip(inner, "private GerGo nested archive")
            macro = _one_named_file(
                inner, GERGO_MACRO_FILENAME, "private GerGo nested archive"
            )
            if macro.file_size > MAX_MACRO_BYTES:
                raise PrivateConfigError("private GerGo macro is too large")
            return _validate_macro(inner.read(macro))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, PrivateConfigError):
            raise
        raise PrivateConfigError("private GerGo package is unreadable") from exc

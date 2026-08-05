"""Minimal interactive serial console for the Klipad50 service connection."""

from __future__ import annotations

import glob
import os
from pathlib import Path
import select
import stat
import termios
import tty
from typing import BinaryIO


SERIAL_BAUD = 1500000
DIRECT_DEVICE_PATTERNS = (r"/dev/ttyACM[0-9]+", r"/dev/ttyUSB[0-9]+")


class SerialConsoleError(RuntimeError):
    pass


def list_serial_devices() -> list[str]:
    candidates = set(glob.glob("/dev/serial/by-id/*"))
    candidates.update(glob.glob("/dev/ttyACM[0-9]*"))
    candidates.update(glob.glob("/dev/ttyUSB[0-9]*"))
    return sorted(path for path in candidates if _allowed_device_name(path))


def serial_device_access(path: str) -> str:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError:
        return "unavailable"
    if not stat.S_ISCHR(resolved.stat().st_mode):
        return "not-a-character-device"
    if os.access(resolved, os.R_OK | os.W_OK):
        return "read-write"
    return "permission-denied"


def _allowed_device_name(path: str) -> bool:
    import re

    if re.fullmatch(r"/dev/serial/by-id/[A-Za-z0-9_.:+-]+", path):
        return True
    return any(re.fullmatch(pattern, path) for pattern in DIRECT_DEVICE_PATTERNS)


def validate_serial_device(value: str) -> Path:
    if not _allowed_device_name(value):
        raise SerialConsoleError(
            "device must be one /dev/serial/by-id, /dev/ttyACM, or /dev/ttyUSB path"
        )
    requested = Path(value)
    try:
        resolved = requested.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise SerialConsoleError("serial device is unavailable: %s" % exc) from exc
    if not stat.S_ISCHR(info.st_mode):
        raise SerialConsoleError("serial device does not resolve to a character device")
    if not os.access(resolved, os.R_OK | os.W_OK):
        raise SerialConsoleError(
            "serial device is not readable and writable by this user; inspect its udev ACL"
        )
    return resolved


def _serial_attributes(current: list[object]) -> list[object]:
    if not hasattr(termios, "B1500000"):
        raise SerialConsoleError("this operating system lacks the required 1500000 baud rate")
    attributes = list(current)
    attributes[0] = 0
    attributes[1] = 0
    attributes[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attributes[3] = 0
    attributes[4] = termios.B1500000
    attributes[5] = termios.B1500000
    control = list(attributes[6])
    control[termios.VMIN] = 1
    control[termios.VTIME] = 0
    attributes[6] = control
    return attributes


def interactive_console(device: Path, stdin: BinaryIO, stdout: BinaryIO) -> None:
    if not os.isatty(stdin.fileno()):
        raise SerialConsoleError("interactive console requires a terminal on standard input")
    descriptor = os.open(
        device,
        os.O_RDWR | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0),
    )
    saved_stdin = termios.tcgetattr(stdin.fileno())
    try:
        current = termios.tcgetattr(descriptor)
        termios.tcsetattr(descriptor, termios.TCSANOW, _serial_attributes(current))
        tty.setraw(stdin.fileno())
        while True:
            readable, _, _ = select.select([stdin.fileno(), descriptor], [], [])
            if descriptor in readable:
                block = os.read(descriptor, 4096)
                if not block:
                    raise SerialConsoleError("serial device disconnected")
                stdout.write(block)
                stdout.flush()
            if stdin.fileno() in readable:
                block = os.read(stdin.fileno(), 4096)
                if b"\x1d" in block:
                    before, _separator, _after = block.partition(b"\x1d")
                    if before:
                        os.write(descriptor, before)
                    return
                if block:
                    os.write(descriptor, block)
    finally:
        termios.tcsetattr(stdin.fileno(), termios.TCSANOW, saved_stdin)
        os.close(descriptor)

#!/usr/bin/env python3
"""Forced-command gate for the marked T300 recovery USB."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
import sys
from typing import NoReturn


AGENT = "/usr/local/sbin/t300-recovery-agent"
ALLOWED_COMMANDS = {"inspect", "stream", "hash", "write"}


def fail(message: str) -> NoReturn:
    print("Error: %s" % (message,), file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not original or len(original) > 1024 or "\0" in original or "\n" in original:
        fail("recovery command is missing or malformed")
    try:
        arguments = shlex.split(original, posix=True)
    except ValueError:
        fail("recovery command quoting is malformed")
    if (
        len(arguments) < 2
        or len(arguments) > 16
        or arguments[0] != AGENT
        or arguments[1] not in ALLOWED_COMMANDS
    ):
        fail("only the fixed T300 recovery agent is available")
    if any(
        not argument
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in argument)
        for argument in arguments
    ):
        fail("recovery command contains unsupported characters")
    agent = Path(AGENT)
    try:
        info = agent.lstat()
    except OSError:
        fail("recovery agent is unavailable")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        fail("recovery agent ownership or permissions are unsafe")
    os.execve(
        AGENT,
        arguments,
        {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

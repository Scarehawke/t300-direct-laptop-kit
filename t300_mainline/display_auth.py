"""Create the short-lived X11 credential used by the local T300 display.

The X server needs direct console access on the Klipad, but KlipperScreen does
not.  Keeping the cookie in a root-created runtime directory lets the UI stay
unprivileged without opening the X server to every local process.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile


AUTHORITY = Path("/run/t300/xorg/Xauthority")
XAUTH = Path("/usr/bin/xauth")
DISPLAY = ":0"


class DisplayAuthError(RuntimeError):
    pass


def _regular_root_binary(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DisplayAuthError("xauth is unavailable: %s" % exc) from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise DisplayAuthError("xauth must be one regular, non-symlink file")
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise DisplayAuthError("xauth must be root-owned and not broadly writable")


def create_authority(
    authority: Path = AUTHORITY,
    xauth: Path = XAUTH,
    display: str = DISPLAY,
) -> Path:
    if not authority.is_absolute() or authority.name != "Xauthority":
        raise DisplayAuthError("the X authority path is not the fixed runtime file")
    if display != DISPLAY:
        raise DisplayAuthError("the local display is fixed at :0")
    parent = authority.parent
    try:
        parent_info = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise DisplayAuthError("X runtime directory is unavailable: %s" % exc) from exc
    if parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
        raise DisplayAuthError("X runtime path must be one real directory")
    if parent_info.st_uid != 0 or parent_info.st_mode & stat.S_IWOTH:
        raise DisplayAuthError("X runtime directory ownership or mode is unsafe")
    _regular_root_binary(xauth)

    cookie = secrets.token_hex(16)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".Xauthority.", dir=str(parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o640)
        os.close(descriptor)
        descriptor = -1
        result = subprocess.run(
            [str(xauth), "-f", str(temporary), "add", display,
             "MIT-MAGIC-COOKIE-1", cookie],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            cwd="/",
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DisplayAuthError("xauth failed: %s" % (detail or "unknown error"))
        os.chmod(temporary, 0o640)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, authority)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return authority


def main() -> int:
    try:
        create_authority()
    except DisplayAuthError as exc:
        print("Display credential error: %s" % exc, file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

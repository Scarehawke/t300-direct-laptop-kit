"""Validate the owner-private runtime for the official T300 serial UI bridge."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath
import stat
import zipfile


BRIDGE_SHA256 = "f6895da83aa078656c3af6d1f29d0d4a6a45f94303cb55403d662f8bda7f3c10"
BOOST_SHA256 = "6008f34c163c425cac1c434bf9c1918892c13641b88b7837ad1fd0db9127daef"
WPA_CLIENT_SHA256 = "298ae20e1ad3d72429f8fa866c1ce224e21ca2825538bfdf16100f3631bf5de5"
THUMBNAIL_HELPER_SHA256 = "aa46027e32666d7b71e006ef429e8e9680c4f83431e1e016c9b30783fe2f3691"
EXPECTED_FILES = {
    "zhongchuang_klipper": BRIDGE_SHA256,
    "gene5.py": THUMBNAIL_HELPER_SHA256,
    "lib/libboost_system.so.1.67.0": BOOST_SHA256,
    "lib/libwpa_client.so": WPA_CLIENT_SHA256,
}
MAX_RUNTIME_BYTES = 64 * 1024 * 1024


class PrivateTouchscreenError(ValueError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_touchscreen_runtime(path: Path) -> dict[str, bytes]:
    """Return exact reviewed files from one owner-private ZIP runtime bundle."""
    try:
        raw = path.expanduser().resolve(strict=True).read_bytes()
    except OSError as exc:
        raise PrivateTouchscreenError("could not read private touchscreen runtime") from exc
    if len(raw) > MAX_RUNTIME_BYTES:
        raise PrivateTouchscreenError("private touchscreen runtime exceeds 64 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
            members = archive.infolist()
            if len(members) != len(EXPECTED_FILES):
                raise PrivateTouchscreenError("private touchscreen runtime has unexpected files")
            result: dict[str, bytes] = {}
            for member in members:
                name = PurePosixPath(member.filename)
                if (
                    name.is_absolute()
                    or any(part in ("", ".", "..") for part in name.parts)
                    or member.is_dir()
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise PrivateTouchscreenError("private touchscreen runtime has an unsafe member")
                canonical = name.as_posix()
                expected = EXPECTED_FILES.get(canonical)
                if expected is None or canonical in result:
                    raise PrivateTouchscreenError("private touchscreen runtime has unexpected files")
                value = archive.read(member)
                if _sha256(value) != expected:
                    raise PrivateTouchscreenError(
                        "private touchscreen file differs from the reviewed official firmware: %s"
                        % canonical
                    )
                result[canonical] = value
    except zipfile.BadZipFile as exc:
        raise PrivateTouchscreenError("private touchscreen runtime is not a valid ZIP") from exc
    if set(result) != set(EXPECTED_FILES):
        raise PrivateTouchscreenError("private touchscreen runtime is incomplete")
    return result

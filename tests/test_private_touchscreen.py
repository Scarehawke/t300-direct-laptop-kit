from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from t300_mainline.private_touchscreen import (
    EXPECTED_FILES,
    PrivateTouchscreenError,
    load_touchscreen_runtime,
)


class PrivateTouchscreenTests(unittest.TestCase):
    def fixture(self, root: Path, values: dict[str, bytes]) -> Path:
        path = root / "touchscreen-runtime.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, value in values.items():
                archive.writestr(name, value)
        return path

    def test_accepts_only_exact_reviewed_private_runtime(self):
        values = {
            "zhongchuang_klipper": b"official bridge fixture",
            "lib/libboost_system.so.1.67.0": b"official boost fixture",
            "lib/libwpa_client.so": b"official wpa fixture",
        }
        expected = {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "t300_mainline.private_touchscreen.EXPECTED_FILES", expected
        ):
            root = Path(directory)
            path = self.fixture(root, values)
            self.assertEqual(load_touchscreen_runtime(path), values)

    def test_rejects_missing_extra_changed_and_traversal_members(self):
        values = {name: name.encode("ascii") for name in EXPECTED_FILES}
        expected = {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}
        cases = {
            "missing": dict(list(values.items())[:-1]),
            "extra": {**values, "unexpected": b"x"},
            "changed": {**values, "zhongchuang_klipper": b"changed"},
            "traversal": {**values, "../escape": b"x"},
        }
        with mock.patch("t300_mainline.private_touchscreen.EXPECTED_FILES", expected):
            for label, members in cases.items():
                with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                    path = self.fixture(Path(directory), members)
                    with self.assertRaises(PrivateTouchscreenError):
                        load_touchscreen_runtime(path)


if __name__ == "__main__":
    unittest.main()

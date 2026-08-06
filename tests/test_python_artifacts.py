from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from t300_mainline.python_artifacts import (
    PythonArtifactError,
    create_lock,
    load_artifact_lock,
    lock_environment,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def report(path: Path, name: str = "Example_Package", version: str = "1.2.3") -> None:
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "install": [
                    {
                        "requested": True,
                        "metadata": {"name": name, "version": version},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def opener_for(filename: str, payload: bytes, *, yanked: bool = False):
    digest = hashlib.sha256(payload).hexdigest()

    def opener(request, timeout):
        self_url = request.full_url
        if self_url != "https://pypi.org/pypi/Example_Package/1.2.3/json":
            raise AssertionError(self_url)
        if timeout != 30:
            raise AssertionError(timeout)
        metadata = {
            "info": {
                "name": "example-package",
                "version": "1.2.3",
                "license_expression": "MIT",
                "requires_python": ">=3.10",
            },
            "urls": [
                {
                    "filename": filename,
                    "digests": {"sha256": digest},
                    "url": "https://files.pythonhosted.org/packages/aa/%s" % filename,
                    "packagetype": "bdist_wheel",
                    "yanked": yanked,
                }
            ],
        }
        return FakeResponse(json.dumps(metadata).encode("utf-8"))

    return opener


def wheel_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            "example_package-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: Example-Package\n"
            "Version: 1.2.3\n"
            "License-Expression: Apache-2.0\n\n",
        )
    return output.getvalue()


class PythonArtifactTests(unittest.TestCase):
    def test_environment_locks_official_cached_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheels"
            wheelhouse.mkdir()
            filename = "example_package-1.2.3-py3-none-any.whl"
            payload = wheel_payload()
            (wheelhouse / filename).write_bytes(payload)
            report_path = root / "report.json"
            report(report_path)
            requirements = root / "requirements.txt"
            requirements.write_text("example-package==1.2.3\n", encoding="ascii")

            result = lock_environment(
                "example",
                report_path,
                wheelhouse,
                requirements,
                "/opt/t300/src/example/requirements.txt",
                opener=opener_for(filename, payload),
            )

            self.assertEqual(result["requirements_path"], "/opt/t300/src/example/requirements.txt")
            self.assertEqual(result["artifacts"][0]["license"], "Apache-2.0")
            self.assertEqual(
                result["artifacts"][0]["license_source"],
                "artifact-metadata:License-Expression",
            )
            self.assertFalse(result["artifacts"][0]["yanked"])

    def test_yanked_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheels"
            wheelhouse.mkdir()
            filename = "example_package-1.2.3-py3-none-any.whl"
            payload = wheel_payload()
            (wheelhouse / filename).write_bytes(payload)
            report_path = root / "report.json"
            report(report_path)

            with self.assertRaisesRegex(PythonArtifactError, "yanked"):
                lock_environment(
                    "example",
                    report_path,
                    wheelhouse,
                    opener=opener_for(filename, payload, yanked=True),
                )

    def test_unlocked_extra_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheels"
            wheelhouse.mkdir()
            filename = "example_package-1.2.3-py3-none-any.whl"
            payload = wheel_payload()
            (wheelhouse / filename).write_bytes(payload)
            (wheelhouse / "surprise.whl").write_bytes(b"unexpected")
            report_path = root / "report.json"
            report(report_path)

            with self.assertRaisesRegex(PythonArtifactError, "unlocked artifacts"):
                lock_environment(
                    "example",
                    report_path,
                    wheelhouse,
                    opener=opener_for(filename, payload),
                )

    def test_logical_requirements_path_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheels"
            wheelhouse.mkdir()
            (wheelhouse / "unused.whl").write_bytes(b"unused")
            report_path = root / "report.json"
            report(report_path)
            requirements = root / "requirements.txt"
            requirements.write_bytes(b"")

            with self.assertRaisesRegex(PythonArtifactError, "below /opt/t300/src"):
                lock_environment(
                    "example",
                    report_path,
                    wheelhouse,
                    requirements,
                    "/tmp/requirements.txt",
                )

    def test_lock_target_matches_signed_base_runtime(self):
        result = create_lock([{"name": "example", "artifacts": []}], "2026-08-04")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["bootstrap"]["pip"], "25.1.1")
        self.assertEqual(result["target"]["python"], "3.13.5")
        self.assertEqual(result["target"]["abi"], "cp313")
        self.assertEqual(result["target"]["architecture"], "aarch64")

    def test_repository_artifact_lock_is_valid(self):
        root = Path(__file__).resolve().parents[1]
        result = load_artifact_lock(root / "mainline/build/python-artifacts.lock.json")
        self.assertEqual({item["name"] for item in result["environments"]}, {
            "build", "klipper", "moonraker"
        })
        self.assertEqual(
            sum(len(item["artifacts"]) for item in result["environments"]), 49
        )
        self.assertEqual(result["bootstrap"]["pip"], "25.1.1")


if __name__ == "__main__":
    unittest.main()

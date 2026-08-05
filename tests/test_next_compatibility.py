from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NextCompatibilityTests(unittest.TestCase):
    def test_next_job_is_reporting_only_and_uses_locked_fixture_inputs(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        commits = {
            item["name"]: item["commit"]
            for item in lock["components"]
        }

        self.assertIn("klipper-next:", workflow)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("--mode next", workflow)
        self.assertIn("--report project/.cache/klipper-next-report.json", workflow)
        self.assertIn("ref: %s" % commits["mainsail-config"], workflow)
        self.assertIn("ref: %s" % commits["kamp"], workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("t300-provision", workflow)
        self.assertNotIn("image write", workflow)

    def test_next_mode_refuses_a_supplied_deployable_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "next-report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin/test-klipper-v013-mainline.py"),
                    "--mode",
                    "next",
                    "--stage",
                    str(Path(directory) / "stage"),
                    "--stage-manifest-sha256",
                    "0" * 64,
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["deployable"])
            self.assertFalse(payload["passed"])
            self.assertIn("cannot consume a deployable stage", payload["error"])

    def test_lock_marks_next_as_non_deployable(self):
        lock = json.loads((ROOT / "stack.lock.json").read_text(encoding="utf-8"))
        self.assertIs(lock["next"]["deployable"], False)
        self.assertEqual(lock["next"]["klipper_ref"], "master")


if __name__ == "__main__":
    unittest.main()

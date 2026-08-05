from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "prepare-runtime-proposal.py"
SPEC = importlib.util.spec_from_file_location("prepare_runtime_proposal", MODULE_PATH)
assert SPEC and SPEC.loader
proposal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proposal)


class IniPatchTests(unittest.TestCase):
    def test_patches_one_option_and_preserves_comment_and_crlf(self):
        original = "[timelapse]\r\nsaveframes: True  # retained\r\n"
        actual, changed = proposal.patch_ini_option(
            original, "timelapse", "saveframes", "True", "False"
        )
        self.assertTrue(changed)
        self.assertEqual(
            actual, "[timelapse]\r\nsaveframes: False  # retained\r\n"
        )

    def test_is_idempotent_at_reviewed_replacement(self):
        original = "[crowsnest]\ndelete_log: false\n"
        actual, changed = proposal.patch_ini_option(
            original, "crowsnest", "delete_log", "true", "false"
        )
        self.assertFalse(changed)
        self.assertEqual(actual, original)

    def test_refuses_unreviewed_value(self):
        with self.assertRaisesRegex(proposal.ProposalError, "expected 'True'"):
            proposal.patch_ini_option(
                "[timelapse]\nsaveframes: sometimes\n",
                "timelapse",
                "saveframes",
                "True",
                "False",
            )

    def test_refuses_duplicate_option(self):
        with self.assertRaisesRegex(proposal.ProposalError, "found 2"):
            proposal.patch_ini_option(
                "[timelapse]\nsaveframes: True\nsaveframes: True\n",
                "timelapse",
                "saveframes",
                "True",
                "False",
            )

    def test_service_review_changes_only_two_reviewed_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "moonraker.conf").write_text(
                "[server]\nport: 7125\n[timelapse]\nsaveframes: True\n",
                encoding="utf-8",
            )
            (root / "crowsnest.conf").write_text(
                "[crowsnest]\ndelete_log: true\n[cam 1]\nmax_fps: 15\n",
                encoding="utf-8",
            )
            patch, review = proposal.build_service_review(root)
        self.assertIn("-saveframes: True", patch)
        self.assertIn("+saveframes: False", patch)
        self.assertIn("-delete_log: true", patch)
        self.assertIn("+delete_log: false", patch)
        self.assertNotIn("+max_fps", patch)
        self.assertNotIn("-max_fps", patch)
        self.assertEqual(set(review), {"moonraker.conf", "crowsnest.conf"})
        self.assertEqual(review["moonraker.conf"]["approval"], "recommended")
        self.assertEqual(review["crowsnest.conf"]["approval"], "conditional")
        self.assertIn("rotation", review["crowsnest.conf"]["precondition"])


if __name__ == "__main__":
    unittest.main()

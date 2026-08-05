import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "record-t300-camera.sh"


class CameraRecorderTests(unittest.TestCase):
    def test_help_does_not_require_a_printer(self):
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--host HOST", result.stdout)

    def test_host_is_required_and_old_address_is_absent(self):
        env = dict(os.environ, T300_HOST="")
        result = subprocess.run(
            [str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Supply --host", result.stderr)
        self.assertNotIn("192.168.178.54", SCRIPT.read_text(encoding="utf-8"))

    def test_builds_reconnecting_low_storage_ffmpeg_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            argument_log = root / "ffmpeg.args"
            output = root / "recording.mkv"
            self._write_executable(
                fake_bin / "ffmpeg",
                """
                #!/bin/sh
                : > "$FAKE_FFMPEG_ARGS"
                for argument do
                  printf '%s\\n' "$argument" >> "$FAKE_FFMPEG_ARGS"
                  last="$argument"
                done
                printf 'fake-video' > "$last"
                """,
            )
            self._write_executable(
                fake_bin / "ffprobe",
                """
                #!/bin/sh
                printf 'width=640\\nheight=480\\nduration=1.0\\nsize=10\\n'
                """,
            )
            env = dict(
                os.environ,
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                FAKE_FFMPEG_ARGS=str(argument_log),
            )
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--host",
                    "10.42.42.2",
                    "--duration",
                    "1",
                    "--output",
                    str(output),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = argument_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-reconnect_streamed", arguments)
            self.assertIn("-rw_timeout", arguments)
            self.assertIn("fps=10", arguments)
            self.assertIn("matroska", arguments)
            self.assertIn("http://10.42.42.2/webcam/?action=stream", arguments)
            self.assertTrue(output.is_file())

    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()

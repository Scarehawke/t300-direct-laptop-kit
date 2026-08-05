from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_timelapse_module():
    package_name = "_t300_timelapse_test_components"
    package = types.ModuleType(package_name)
    package.__path__ = []
    upstream = types.ModuleType(package_name + ".timelapse_upstream")

    class UpstreamTimelapse:
        pass

    upstream.Timelapse = UpstreamTimelapse
    sys.modules[package_name] = package
    sys.modules[upstream.__name__] = upstream
    name = package_name + ".timelapse"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "mainline/moonraker/t300_timelapse.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TIMELAPSE = load_timelapse_module()


def jpeg_bytes() -> bytes:
    return b"\xff\xd8" + (b"J" * 1020) + b"\xff\xd9"


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200, error: bool = False):
        self.content = content
        self.status_code = status
        self._error = error

    def has_error(self):
        return self._error


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeKlippyApis:
    def __init__(self, filename="models/frieren.gcode"):
        self.filename = filename
        self.gcodes = []

    async def query_objects(self, objects):
        return {"print_stats": {"filename": self.filename}}

    async def run_gcode(self, command):
        self.gcodes.append(command)


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""

    def kill(self):
        self.returncode = -9


class TimelapsePolicyTests(unittest.TestCase):
    def make_capture(self, directory: Path, response: FakeResponse):
        instance = object.__new__(TIMELAPSE.T300Timelapse)
        instance.temp_dir = str(directory) + "/"
        instance.config = {
            "snapshoturl": "http://127.0.0.1:8080/?action=snapshot",
            "enabled": True,
            "autorender": True,
        }
        instance.http_client = FakeHttpClient(response)
        instance.maximum_frame_bytes = 16 * 1024 * 1024
        instance.maximum_frames = 10000
        instance.framecount = 0
        instance.lastframefile = ""
        instance.takingframe = True
        instance._storage_failure_logged = False
        instance._storage_ready = lambda additional_bytes=0: True
        events = []
        instance.notify_event = events.append

        async def get_webcam_config():
            return None

        instance.getWebcamConfig = get_webcam_config
        return instance, events

    def make_renderer(self, root: Path):
        frames = root / "frames"
        videos = root / "videos"
        frames.mkdir()
        videos.mkdir()
        for index in range(1, 4):
            (frames / ("frame%06d.jpg" % index)).write_bytes(jpeg_bytes())
        instance = object.__new__(TIMELAPSE.T300Timelapse)
        instance.temp_dir = str(frames) + "/"
        instance.out_dir = str(videos) + "/"
        instance.config = {
            "variable_fps": True,
            "targetlength": 30,
            "variable_fps_min": 5,
            "variable_fps_max": 30,
            "output_framerate": 30,
            "constant_rate_factor": 26,
            "previewimage": True,
        }
        instance.maximum_frame_bytes = 16 * 1024 * 1024
        instance.maximum_frames = 10000
        instance.maximum_video_bytes = 1024 * 1024 * 1024
        instance.minimum_free_bytes = 1024
        instance.render_timeout_seconds = 30
        instance.ffmpeg_binary_path = "/usr/bin/ffmpeg"
        instance.ffprobe_binary_path = "/usr/bin/ffprobe"
        instance.renderisrunning = False
        instance.printing = False
        instance.takingframe = False
        instance.framecount = 0
        instance.byrendermacro = False
        instance.klippy_apis = FakeKlippyApis()
        instance._storage_ready = lambda additional_bytes=0: True
        instance._render_budget = lambda: 1024 * 1024 * 1024
        instance._storage_failure_logged = False
        events = []
        instance.notify_event = events.append
        return instance, frames, videos, events

    def test_capture_uses_bounded_http_and_atomic_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance, events = self.make_capture(root, FakeResponse(jpeg_bytes()))
            asyncio.run(instance.newframe())
            self.assertEqual((root / "frame000001.jpg").read_bytes(), jpeg_bytes())
            self.assertEqual(instance.framecount, 1)
            self.assertFalse(instance.takingframe)
            self.assertEqual(events[-1]["status"], "success")
            _url, options = instance.http_client.calls[0]
            self.assertEqual(options["attempts"], 1)
            self.assertFalse(options["enable_cache"])
            self.assertEqual(options["request_timeout"], 3.0)

    def test_verified_video_survives_partial_frame_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, frames, videos, _events = self.make_renderer(Path(directory))

            async def create_process(*args, **kwargs):
                Path(args[-1]).write_bytes(b"M" * 4096)
                return FakeProcess()

            async def verify_video(path):
                return {"codec_name": "h264", "width": 640, "height": 480}

            original_unlink = Path.unlink

            def flaky_unlink(path, *args, **kwargs):
                if path.name == "frame000002.jpg":
                    raise OSError("simulated USB cleanup failure")
                return original_unlink(path, *args, **kwargs)

            instance._verify_video = verify_video
            with mock.patch.object(
                TIMELAPSE.asyncio, "create_subprocess_exec", side_effect=create_process
            ), mock.patch.object(Path, "unlink", new=flaky_unlink), self.assertLogs(
                level="WARNING"
            ):
                result = asyncio.run(instance.render())
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["verified"])
            self.assertEqual(result["frames_removed"], 2)
            self.assertEqual(result["frames_retained"], 1)
            self.assertEqual(len(list(videos.glob("*.mp4"))), 1)
            self.assertEqual(
                [path.name for path in frames.glob("frame*.jpg")],
                ["frame000002.jpg"],
            )

    def test_render_rejects_a_frame_set_over_the_fixed_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, frames, videos, _events = self.make_renderer(Path(directory))
            instance.maximum_frames = 2
            with self.assertLogs(level="ERROR"):
                result = asyncio.run(instance.render())
            self.assertEqual(result["status"], "error")
            self.assertIn("fixed frame limit", result["msg"])
            self.assertEqual(len(list(frames.glob("frame*.jpg"))), 3)
            self.assertEqual(list(videos.iterdir()), [])

    def test_capture_rejects_non_jpeg_without_advancing_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance, events = self.make_capture(root, FakeResponse(b"x" * 2048))
            asyncio.run(instance.newframe())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(instance.framecount, 0)
            self.assertEqual(events[-1]["status"], "error")

    def test_capture_rechecks_exact_frame_size_against_usb_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = jpeg_bytes()
            instance, events = self.make_capture(root, FakeResponse(content))
            checks = []

            def storage_ready(additional_bytes=0):
                checks.append(additional_bytes)
                return additional_bytes != len(content)

            instance._storage_ready = storage_ready
            asyncio.run(instance.newframe())
            self.assertEqual(list(root.iterdir()), [])
            self.assertIn(len(content), checks)
            self.assertFalse(instance.config["enabled"])
            self.assertEqual(events[-1]["status"], "error")

    def test_capture_stops_at_the_fixed_frame_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, events = self.make_capture(
                Path(directory), FakeResponse(jpeg_bytes())
            )
            instance.maximum_frames = 3
            instance.framecount = 3
            asyncio.run(instance.newframe())
            self.assertEqual(instance.http_client.calls, [])
            self.assertFalse(instance.config["enabled"])
            self.assertEqual(events[-1]["msg"], "timelapse frame limit reached")

    def test_settings_post_is_rejected(self):
        instance = object.__new__(TIMELAPSE.T300Timelapse)
        instance.server = types.SimpleNamespace(error=RuntimeError)
        instance.config = {}
        request = types.SimpleNamespace(get_action=lambda: "POST")
        with self.assertRaises(RuntimeError):
            asyncio.run(instance.webrequest_settings(request))

    def test_frame_zip_export_is_unconditionally_rejected(self):
        instance = object.__new__(TIMELAPSE.T300Timelapse)
        instance.server = types.SimpleNamespace(error=RuntimeError)
        with self.assertRaises(RuntimeError):
            asyncio.run(instance.saveFramesZip())

    def test_verified_render_uses_exec_arguments_and_removes_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, frames, videos, events = self.make_renderer(Path(directory))
            invocations = []

            async def create_process(*args, **kwargs):
                invocations.append((args, kwargs))
                Path(args[-1]).write_bytes(b"M" * 4096)
                return FakeProcess()

            async def verify_video(path):
                return {"codec_name": "h264", "width": 640, "height": 480}

            instance._verify_video = verify_video
            with mock.patch.object(
                TIMELAPSE.asyncio, "create_subprocess_exec", side_effect=create_process
            ):
                result = asyncio.run(instance.render())
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["verified"])
            self.assertEqual(list(frames.glob("frame*.jpg")), [])
            self.assertEqual(len(list(videos.glob("*.mp4"))), 1)
            self.assertEqual(len(list(videos.glob("*.jpg"))), 1)
            self.assertEqual(invocations[0][0][0], "/usr/bin/ffmpeg")
            self.assertIn("-fs", invocations[0][0])
            budget_index = invocations[0][0].index("-fs") + 1
            self.assertEqual(invocations[0][0][budget_index], str(1024 * 1024 * 1024))
            self.assertNotIn("shell_command", " ".join(invocations[0][0]))
            self.assertEqual(events[0]["status"], "started")
            self.assertEqual(events[-1]["status"], "success")

    def test_failed_video_verification_retains_source_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, frames, videos, _events = self.make_renderer(Path(directory))

            async def create_process(*args, **kwargs):
                Path(args[-1]).write_bytes(b"M" * 4096)
                return FakeProcess()

            async def reject_video(path):
                return None

            instance._verify_video = reject_video
            with mock.patch.object(
                TIMELAPSE.asyncio, "create_subprocess_exec", side_effect=create_process
            ), self.assertLogs(level="ERROR"):
                result = asyncio.run(instance.render())
            self.assertEqual(result["status"], "error")
            self.assertEqual(len(list(frames.glob("frame*.jpg"))), 3)
            self.assertEqual(list(videos.iterdir()), [])

    def test_render_is_refused_while_printing(self):
        with tempfile.TemporaryDirectory() as directory:
            instance, frames, videos, events = self.make_renderer(Path(directory))
            instance.printing = True
            result = asyncio.run(instance.render())
            self.assertEqual(result["status"], "error")
            self.assertEqual(len(list(frames.glob("frame*.jpg"))), 3)
            self.assertEqual(list(videos.iterdir()), [])
            self.assertEqual(events[-1]["status"], "error")

    def test_symlinked_storage_child_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "mount"
            outside = Path(directory) / "outside"
            mount.mkdir()
            outside.mkdir()
            (mount / "frames").symlink_to(outside, target_is_directory=True)
            self.assertFalse(TIMELAPSE.T300Timelapse._real_directory_tree(
                mount / "frames", mount
            ))

    def test_wrapper_contains_no_shell_execution_path(self):
        source = (ROOT / "mainline/moonraker/t300_timelapse.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("shell_command", source)
        self.assertNotIn("create_subprocess_shell", source)


if __name__ == "__main__":
    unittest.main()

"""Fail-soft removable-storage policy for pinned moonraker-timelapse.

The upstream component remains pinned and is staged beside this module as
``timelapse_upstream.py``. This wrapper keeps its API and event integration,
but replaces shell-based capture and rendering with bounded direct calls. It
does not park the head or issue motion/heater commands.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import asyncio
import datetime as dt
import glob
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any
from urllib.parse import urlsplit

from .timelapse_upstream import Timelapse as UpstreamTimelapse


FRAME_RE = re.compile(r"^frame([0-9]{6})\.jpg$")
SAFE_PRINT_RE = re.compile(r"[^A-Za-z0-9._-]+")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class T300Timelapse(UpstreamTimelapse):
    def __init__(self, confighelper):
        self.storage_mount = Path(
            confighelper.get("storage_mount_path", "/mnt/t300-data")
        )
        self.minimum_free_bytes = confighelper.getint(
            "minimum_free_bytes", 1073741824, minval=268435456
        )
        self.maximum_frame_bytes = confighelper.getint(
            "maximum_frame_bytes", 16777216, minval=1048576, maxval=67108864
        )
        self.maximum_frames = confighelper.getint(
            "maximum_frames", 10000, minval=1, maxval=100000
        )
        self.maximum_video_bytes = confighelper.getint(
            "maximum_video_bytes", 1073741824,
            minval=16777216, maxval=2147483648,
        )
        self.render_timeout_seconds = confighelper.getint(
            "render_timeout_seconds", 1800, minval=60, maxval=7200
        )
        self.ffprobe_binary_path = confighelper.get(
            "ffprobe_binary_path", "/usr/bin/ffprobe"
        )
        configured_frames = Path(os.path.abspath(os.path.expanduser(
            confighelper.get("frame_path", "/mnt/t300-data/timelapse/frames")
        )))
        configured_videos = Path(os.path.abspath(os.path.expanduser(
            confighelper.get("output_path", "/mnt/t300-data/timelapse/videos")
        )))
        self.retained_dir = self.storage_mount / "timelapse/retained"
        expected_paths = (
            (configured_frames, self.storage_mount / "timelapse/frames"),
            (configured_videos, self.storage_mount / "timelapse/videos"),
        )
        if any(actual != expected for actual, expected in expected_paths):
            raise self._config_error(
                confighelper, "frame and video paths must use the fixed USB layout"
            )
        if (
            not os.path.ismount(self.storage_mount)
            or not all(
                self._real_directory_tree(path, self.storage_mount)
                for path in (configured_frames, configured_videos, self.retained_dir)
            )
        ):
            raise self._config_error(
                confighelper,
                "the mounted USB frame, video, and retained directories must already exist",
            )
        self._storage_failure_logged = False
        super().__init__(confighelper)
        self.http_client = self.server.lookup_component("http_client")
        self._validate_fixed_policy(confighelper)
        if not self._storage_ready():
            self._disable_storage("removable timelapse USB is unavailable")

    async def handle_klippy_ready(self) -> None:
        # The production Klipper config has one immutable, non-moving frame
        # trigger. Upstream's mutable parking/hyperlapse setup is intentionally
        # not installed and therefore must not be synchronized into Klipper.
        return None

    def _config_error(self, confighelper, message: str) -> Exception:
        return confighelper.error("T300 timelapse policy: %s" % message)

    def _validate_fixed_policy(self, confighelper) -> None:
        parsed = urlsplit(self.config["snapshoturl"])
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port != 8080
        ):
            raise self._config_error(
                confighelper, "snapshoturl must be the local Crowsnest port"
            )
        if self.config["mode"] != "layermacro":
            raise self._config_error(confighelper, "only one frame per layer is allowed")
        if self.config["parkhead"]:
            raise self._config_error(confighelper, "head parking must remain disabled")
        if self.config["saveframes"]:
            raise self._config_error(confighelper, "ZIP frame export must remain disabled")
        if self.config["extraoutputparams"]:
            raise self._config_error(confighelper, "extra FFmpeg parameters are forbidden")
        if self.config["pixelformat"] != "yuv420p":
            raise self._config_error(confighelper, "pixel format must be yuv420p")
        if self.config["rotation"] != 0 or self.config["flip_x"] or self.config["flip_y"]:
            raise self._config_error(confighelper, "runtime image transforms are disabled")
        if self.config["duplicatelastframe"] != 0:
            raise self._config_error(confighelper, "duplicate frame generation is disabled")
        if not 18 <= int(self.config["constant_rate_factor"]) <= 35:
            raise self._config_error(confighelper, "CRF is outside 18..35")
        if not 1 <= int(self.config["output_framerate"]) <= 60:
            raise self._config_error(confighelper, "output framerate is outside 1..60")
        if not 5 <= int(self.config["variable_fps_min"]) <= 60:
            raise self._config_error(confighelper, "minimum variable FPS is outside 5..60")
        if not 5 <= int(self.config["variable_fps_max"]) <= 60:
            raise self._config_error(confighelper, "maximum variable FPS is outside 5..60")
        if self.config["variable_fps_min"] > self.config["variable_fps_max"]:
            raise self._config_error(confighelper, "variable FPS bounds are reversed")
        if not 5 <= int(self.config["targetlength"]) <= 600:
            raise self._config_error(confighelper, "target length is outside 5..600 seconds")

    @staticmethod
    def _real_directory_tree(path: Path, mount: Path) -> bool:
        path = Path(os.path.abspath(path))
        mount = Path(os.path.abspath(mount))
        try:
            path.relative_to(mount)
        except ValueError:
            return False
        current = Path(path.anchor)
        try:
            for part in path.parts[1:]:
                current /= part
                mode = os.lstat(current).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return False
        except OSError:
            return False
        return True

    def _paths_are_beneath_mount(self) -> bool:
        try:
            mount = Path(os.path.abspath(self.storage_mount))
            if not self._real_directory_tree(mount, mount) or not os.path.ismount(mount):
                return False
            return all(
                self._real_directory_tree(Path(value), mount)
                for value in (self.temp_dir, self.out_dir, self.retained_dir)
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _storage_ready(self, additional_bytes: int = 0) -> bool:
        if additional_bytes < 0 or not self._paths_are_beneath_mount():
            return False
        try:
            free = shutil.disk_usage(self.storage_mount).free
        except OSError:
            return False
        return free >= self.minimum_free_bytes + additional_bytes

    def _disable_storage(self, reason: str) -> None:
        self.config["enabled"] = False
        self.config["autorender"] = False
        self.takingframe = False
        if not self._storage_failure_logged:
            logging.warning("timelapse disabled without affecting print: %s", reason)
            self._storage_failure_logged = True

    def call_newframe(self, macropark=False, hyperlapse=False):
        del macropark, hyperlapse
        if not self.config["enabled"]:
            return
        if self.takingframe:
            logging.info("previous timelapse capture is still running; frame skipped")
            return
        if self.framecount >= self.maximum_frames:
            self._disable_storage("the configured frame-count limit was reached")
            return
        if not self._storage_ready(self.maximum_frame_bytes):
            self._disable_storage("USB missing or free-space reserve reached")
            return
        self.takingframe = True
        delay = max(0.0, float(self.config["stream_delay_compensation"]))
        loop = asyncio.get_running_loop()
        loop.call_later(delay, lambda: loop.create_task(self.newframe()))

    @staticmethod
    def _valid_jpeg_bytes(content: bytes, maximum: int) -> bool:
        return (
            1024 <= len(content) <= maximum
            and content.startswith(b"\xff\xd8")
            and content.endswith(b"\xff\xd9")
        )

    def _valid_jpeg_file(self, path: Path) -> bool:
        try:
            size = path.stat().st_size
            if path.is_symlink() or not 1024 <= size <= self.maximum_frame_bytes:
                return False
            with path.open("rb") as handle:
                if handle.read(2) != b"\xff\xd8":
                    return False
                handle.seek(-2, os.SEEK_END)
                return handle.read(2) == b"\xff\xd9"
        except OSError:
            return False

    @staticmethod
    def _write_atomic(path: Path, content: bytes, mode: int = 0o640) -> None:
        if path.exists() or path.is_symlink():
            raise OSError("refusing to replace an existing timelapse file")
        descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    async def newframe(self):
        result = {"action": "newframe", "status": "error"}
        try:
            if self.framecount >= self.maximum_frames:
                self._disable_storage("the configured frame-count limit was reached")
                result["msg"] = "timelapse frame limit reached"
                return
            if not self._storage_ready(self.maximum_frame_bytes):
                self._disable_storage("USB disappeared before frame capture")
                result["msg"] = "removable storage unavailable"
                return
            response = await self.http_client.get(
                self.config["snapshoturl"],
                connect_timeout=1.0,
                request_timeout=3.0,
                attempts=1,
                enable_cache=False,
            )
            if response.has_error() or response.status_code != 200:
                result["msg"] = "camera snapshot request failed"
                return
            content = response.content
            if not self._valid_jpeg_bytes(content, self.maximum_frame_bytes):
                result["msg"] = "camera returned an invalid or oversized JPEG"
                return
            if not self._storage_ready(len(content)):
                self._disable_storage("USB reserve reached before frame write")
                result["msg"] = "removable storage reserve reached"
                return
            next_frame = self.framecount + 1
            framefile = "frame%06d.jpg" % next_frame
            destination = Path(self.temp_dir) / framefile
            self._write_atomic(destination, content)
            self.framecount = next_frame
            self.lastframefile = framefile
            result.update(
                {"frame": str(next_frame), "framefile": framefile, "status": "success"}
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("bounded timelapse frame capture failed")
            result["msg"] = "frame capture failed"
        finally:
            self.takingframe = False
            self.notify_event(result)

    def cleanup(self):
        """Retain prior frames instead of deleting evidence before a new job."""
        filelist = sorted(glob.glob(self.temp_dir + "frame*.jpg"))
        if filelist and self._storage_ready():
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            retained = self.retained_dir / stamp
            suffix = 0
            while retained.exists():
                suffix += 1
                retained = retained.with_name("%s-%d" % (stamp, suffix))
            try:
                retained.mkdir()
                for filename in filelist:
                    source = Path(filename)
                    if source.is_symlink() or not source.is_file():
                        raise OSError("unsafe prior frame")
                    shutil.move(source, retained / source.name)
                logging.warning("retained unverified timelapse frames at %s", retained)
            except OSError as exc:
                self._disable_storage("could not retain old frames: %s" % exc)
                return
        self.framecount = 0
        self.lastframefile = ""

    async def webrequest_settings(self, webrequest):
        if webrequest.get_action() != "GET":
            raise self.server.error(
                "T300 timelapse settings are immutable; deploy a reviewed config change",
                403,
            )
        return dict(self.config)

    def call_saveFramesZip(self):
        logging.warning("T300 timelapse frame ZIP export is disabled")

    async def saveFramesZip(self, webrequest=None):
        del webrequest
        raise self.server.error(
            "T300 timelapse frame ZIP export is disabled by storage policy", 403
        )

    def _render_budget(self) -> int:
        if not self._paths_are_beneath_mount():
            return 0
        try:
            free = shutil.disk_usage(self.storage_mount).free
        except OSError:
            return 0
        # Keep room for the configured free-space reserve, one maximum-size
        # preview frame, and FFmpeg/container allocation overhead.
        overhead = self.maximum_frame_bytes + 64 * 1024 * 1024
        available = free - self.minimum_free_bytes - overhead
        return max(0, min(self.maximum_video_bytes, available))

    async def _verify_video(self, path: Path) -> dict[str, Any] | None:
        process = None
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size < 1024:
                return None
            process = await asyncio.create_subprocess_exec(
                self.ffprobe_binary_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,duration",
                "-of",
                "json",
                str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode:
                return None
            value = json.loads(stdout.decode("utf-8"))
            streams = value.get("streams") if isinstance(value, dict) else None
            if not isinstance(streams, list) or len(streams) != 1:
                return None
            stream = streams[0]
            if (
                not isinstance(stream, dict)
                or not stream.get("codec_name")
                or int(stream.get("width", 0)) <= 0
                or int(stream.get("height", 0)) <= 0
            ):
                return None
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            return stream
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
                await process.communicate()
            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _safe_print_name(filename: str) -> str:
        name = Path(filename).name
        name = SAFE_PRINT_RE.sub("_", name).strip("._-")
        return (name or "print")[:64]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _render_frames(self) -> list[Path]:
        frames: list[Path] = []
        with os.scandir(self.temp_dir) as entries:
            for entry in entries:
                if not entry.name.startswith("frame") or not entry.name.endswith(".jpg"):
                    continue
                frames.append(Path(entry.path))
                if len(frames) > self.maximum_frames:
                    raise OSError("timelapse frame set exceeds the fixed frame limit")
        frames.sort()
        for index, frame in enumerate(frames, 1):
            match = FRAME_RE.fullmatch(frame.name)
            if (
                match is None
                or int(match.group(1)) != index
                or frame.is_symlink()
                or not frame.is_file()
                or not self._valid_jpeg_file(frame)
            ):
                raise OSError("timelapse frame set is incomplete or unsafe")
        return frames

    def _render_fps(self, frame_count: int) -> int:
        if self.config["variable_fps"]:
            fps = int(frame_count / self.config["targetlength"])
            return max(
                min(fps, self.config["variable_fps_max"]),
                self.config["variable_fps_min"],
            )
        return self.config["output_framerate"]

    async def _finish_render_macro(self) -> None:
        # No printer-side render macro exists in the production surface. A
        # remote render request may still set this upstream compatibility flag;
        # clear it locally without sending mutable G-code back to Klipper.
        self.byrendermacro = False

    async def render(self, webrequest=None):
        del webrequest
        result: dict[str, Any] = {"action": "render"}
        if self.renderisrunning:
            result.update({"status": "running", "msg": "render is already running"})
            self.notify_event(result)
            return result
        if self.printing or self.takingframe:
            result.update(
                {
                    "status": "error",
                    "msg": "render refused while a print or frame capture is active",
                }
            )
            self.notify_event(result)
            return result
        partial: Path | None = None
        final: Path | None = None
        final_committed = False
        try:
            frames = self._render_frames()
            self.framecount = len(frames)
            if not frames:
                result.update({"status": "skipped", "msg": "no frames to render"})
                return result
            output_budget = self._render_budget()
            if output_budget < 16 * 1024 * 1024:
                self._disable_storage("not enough reserved USB space to render")
                result.update(
                    {
                        "status": "error",
                        "msg": "render skipped; removable storage is unavailable or full",
                    }
                )
                return result

            self.renderisrunning = True
            query = await self.klippy_apis.query_objects({"print_stats": None})
            printfile = query.get("print_stats", {}).get("filename", "")
            basename = self._safe_print_name(printfile)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            final = Path(self.out_dir) / ("timelapse_%s_%s.mp4" % (basename, stamp))
            suffix = 0
            while final.exists() or final.is_symlink():
                suffix += 1
                final = final.with_name("timelapse_%s_%s-%d.mp4" % (basename, stamp, suffix))

            handle = tempfile.NamedTemporaryFile(
                prefix=".render-", suffix=".mp4", dir=self.temp_dir, delete=False
            )
            partial = Path(handle.name)
            handle.close()
            os.chmod(partial, 0o640)
            fps = self._render_fps(len(frames))
            result.update(
                {
                    "status": "started",
                    "framecount": str(len(frames)),
                    "settings": {
                        "framerate": fps,
                        "crf": self.config["constant_rate_factor"],
                        "pixelformat": "yuv420p",
                    },
                }
            )
            self.notify_event(result)
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg_binary_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-start_number",
                "1",
                "-i",
                str(Path(self.temp_dir) / "frame%06d.jpg"),
                "-threads",
                "2",
                "-g",
                "5",
                "-crf",
                str(self.config["constant_rate_factor"]),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-fs",
                str(output_budget),
                "-movflags",
                "+faststart",
                "-y",
                str(partial),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.render_timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise OSError("FFmpeg render timed out")
            if process.returncode:
                detail = stderr.decode("utf-8", "replace")[-1000:]
                raise OSError("FFmpeg render failed: %s" % detail)
            if partial.stat().st_size > output_budget:
                raise OSError("rendered file exceeded its fixed output budget")
            stream = await self._verify_video(partial)
            if stream is None:
                raise OSError("rendered file failed ffprobe verification")
            os.replace(partial, final)
            partial = None
            stream = await self._verify_video(final)
            if stream is None:
                raise OSError("final file failed post-move verification")
            final_committed = True

            digest = self._sha256(final)
            preview = None
            warnings: list[str] = []
            if self.config["previewimage"]:
                preview_path = final.with_suffix(".jpg")
                try:
                    self._write_atomic(preview_path, frames[-1].read_bytes())
                    preview = preview_path.name
                except OSError as exc:
                    logging.warning("verified timelapse kept without preview: %s", exc)
                    warnings.append("preview image could not be written")
            removed_frames = 0
            for frame in frames:
                try:
                    frame.unlink()
                    removed_frames += 1
                except OSError as exc:
                    logging.warning("verified timelapse frame cleanup failed: %s", exc)
            retained_frames = len(frames) - removed_frames
            if retained_frames:
                warnings.append(
                    "%d source frame(s) could not be removed" % retained_frames
                )
            result = {
                "action": "render",
                "status": "success",
                "msg": (
                    "timelapse rendered and verified"
                    if not warnings
                    else "timelapse rendered and verified; " + "; ".join(warnings)
                ),
                "filename": final.name,
                "printfile": printfile,
                "verified": True,
                "sha256": digest,
                "video_stream": stream,
                "frames_removed": removed_frames,
                "frames_retained": retained_frames,
            }
            if preview is not None:
                result["previewimage"] = preview
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("timelapse render failed; source frames retained")
            if final is not None and not final_committed:
                final.unlink(missing_ok=True)
            if final_committed and final is not None:
                result = {
                    "action": "render",
                    "status": "error",
                    "msg": (
                        "%s; the verified video and source frames were retained" % exc
                    ),
                    "filename": final.name,
                    "verified": True,
                }
            else:
                result = {
                    "action": "render",
                    "status": "error",
                    "msg": "%s; source frames were retained" % exc,
                }
            return result
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
            self.renderisrunning = False
            self.notify_event(result)
            await self._finish_render_macro()


def load_component(config):
    return T300Timelapse(config)

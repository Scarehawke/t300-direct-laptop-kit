"""Polling, fail-closed admission worker for T300 virtual-SD G-code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
from typing import Iterable

from .gcode_policy import (
    GCodePolicy,
    PolicyError,
    admit_gcode,
)


GCODE_SUFFIXES = {".gcode", ".g", ".gco"}


class AdmissionResourceError(PolicyError):
    pass


class AdmissionDaemon:
    def __init__(
        self,
        gcode_root: Path,
        approval_dir: Path,
        spool_dir: Path,
        rejected_dir: Path,
        policy_path: Path,
        interval: float = 2.0,
    ) -> None:
        self.gcode_root = self._real_directory(gcode_root, "G-code root")
        self.approval_dir = self._real_directory(approval_dir, "approval directory")
        self.spool_dir = self._real_directory(spool_dir, "protected G-code directory")
        self.rejected_dir = self._real_directory(rejected_dir, "rejection directory")
        self._validate_directory_layout()
        self.policy_path = self._real_file(policy_path, "policy file")
        self.policy_sha256 = self._policy_digest()
        self.policy = GCodePolicy.from_json(self.policy_path)
        self.interval = interval
        self.running = True
        self.observed: dict[Path, tuple[int, int, int]] = {}
        self.processed: dict[Path, tuple[int, int, int]] = {}

    @staticmethod
    def _real_directory(path: Path, description: str) -> Path:
        requested = Path(os.path.abspath(path.expanduser()))
        if requested.is_symlink() or not requested.is_dir():
            raise PolicyError("%s must be one real directory" % description)
        return requested.resolve(strict=True)

    @staticmethod
    def _real_file(path: Path, description: str) -> Path:
        requested = Path(os.path.abspath(path.expanduser()))
        try:
            info = requested.lstat()
        except OSError as exc:
            raise PolicyError("%s is unavailable: %s" % (description, exc)) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PolicyError("%s must be one real regular file" % description)
        return requested.resolve(strict=True)

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        try:
            first.relative_to(second)
            return True
        except ValueError:
            pass
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False

    def _validate_directory_layout(self) -> None:
        directories = {
            "G-code root": self.gcode_root,
            "approval directory": self.approval_dir,
            "protected G-code directory": self.spool_dir,
            "rejection directory": self.rejected_dir,
        }
        items = list(directories.items())
        for index, (first_name, first_path) in enumerate(items):
            for second_name, second_path in items[index + 1 :]:
                if self._paths_overlap(first_path, second_path):
                    raise PolicyError(
                        "%s and %s must be distinct, non-overlapping directories"
                        % (first_name, second_name)
                    )

    def _policy_digest(self) -> str:
        try:
            info = self.policy_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise AdmissionResourceError("production policy is not a regular file")
            digest = hashlib.sha256()
            with self.policy_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError as exc:
            raise AdmissionResourceError(
                "production policy cannot be read: %s" % exc
            ) from exc

    def _verify_policy_unchanged(self) -> None:
        if self._policy_digest() != self.policy_sha256:
            raise AdmissionResourceError(
                "production policy changed while admission was running; restart required"
            )

    def _fail_closed_policy_change(self, error: Exception) -> tuple[int, int]:
        self._revoke_all()
        self.observed.clear()
        self.processed.clear()
        self._prune_unreferenced_spool()
        self._record_global_policy_error(error)
        self._prune_rejections()
        return 0, 1

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False

    def _candidate_files(self) -> list[Path]:
        candidates: list[Path] = []
        for root, directories, filenames in os.walk(self.gcode_root, followlinks=False):
            root_path = Path(root)
            directories[:] = [
                name
                for name in directories
                if not name.startswith(".")
                and not (root_path / name).is_symlink()
            ]
            for filename in filenames:
                path = root_path / filename
                if path.suffix.lower() in GCODE_SUFFIXES and not filename.startswith("."):
                    candidates.append(path)
                    if len(candidates) > self.policy.max_candidate_files:
                        raise AdmissionResourceError(
                            "G-code directory exceeds the production file-count limit"
                        )
        return sorted(candidates)

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, int] | None:
        try:
            info = path.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return info.st_size, info.st_mtime_ns, info.st_ino

    def _write_rejection(self, payload: dict[str, object], target: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".rejection-", dir=self.rejected_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o640)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(self.rejected_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _record_rejection(self, report: object) -> None:
        self._write_rejection(
            report.to_json(), self.rejected_dir / (report.sha256 + ".json")
        )

    def _record_policy_error(
        self, path: Path, fingerprint: tuple[int, int, int], error: Exception
    ) -> None:
        relative = self._relative_name(path)
        identity = "%s\0%d\0%d\0%d" % (relative, *fingerprint)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self._write_rejection(
            {
                "accepted": False,
                "path": relative,
                "fingerprint": list(fingerprint),
                "policy_error": str(error)[:1000],
            },
            self.rejected_dir / (digest + ".json"),
        )

    def _record_global_policy_error(self, error: Exception) -> None:
        message = str(error)[:1000]
        digest = hashlib.sha256(("global\0" + message).encode("utf-8")).hexdigest()
        self._write_rejection(
            {
                "accepted": False,
                "scope": "gcode-root",
                "policy_error": message,
            },
            self.rejected_dir / (digest + ".json"),
        )

    def _relative_name(self, path: Path) -> str:
        return path.relative_to(self.gcode_root).as_posix()

    def _revoke_all(self) -> None:
        for record_path in self.approval_dir.glob("*.json"):
            try:
                info = record_path.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                record_path.unlink(missing_ok=True)

    def _revoke_relative(self, relative_name: str, keep: Path | None = None) -> None:
        for record_path in self.approval_dir.glob("*.json"):
            if keep is not None and record_path == keep:
                continue
            try:
                info = record_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if record.get("relative_path") == relative_name:
                record_path.unlink(missing_ok=True)

    def _prune_unreferenced_spool(self) -> None:
        referenced: set[str] = set()
        for record_path in self.approval_dir.glob("*.json"):
            try:
                info = record_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            spool_file = record.get("spool_file")
            if isinstance(spool_file, str):
                referenced.add(spool_file)
        for path in self.spool_dir.glob("*.gcode"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if (
                path.name not in referenced
                and not stat.S_ISLNK(info.st_mode)
                and stat.S_ISREG(info.st_mode)
            ):
                path.unlink(missing_ok=True)
        for partial in self.spool_dir.glob(".gcode-snapshot-*"):
            try:
                info = partial.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                partial.unlink(missing_ok=True)
        for partial in self.approval_dir.glob(".approval-*"):
            try:
                info = partial.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                partial.unlink(missing_ok=True)

    def _prune_rejections(self) -> None:
        records: list[tuple[int, str, Path]] = []
        for path in self.rejected_dir.glob("*.json"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                records.append((info.st_mtime_ns, path.name, path))
        excess = len(records) - self.policy.max_rejection_records
        for _mtime, _name, path in sorted(records)[: max(0, excess)]:
            path.unlink(missing_ok=True)

    def startup_preflight(self) -> tuple[int, int]:
        # Never carry a prior process's authorization across a scanner restart.
        self._revoke_all()
        self.observed.clear()
        self.processed.clear()
        self._prune_unreferenced_spool()
        try:
            self._verify_policy_unchanged()
            self._candidate_files()
        except AdmissionResourceError as exc:
            return self._fail_closed_policy_change(exc)
        self._prune_rejections()
        return 0, 0

    def scan_once(self) -> tuple[int, int]:
        admitted = rejected = 0
        try:
            self._verify_policy_unchanged()
        except AdmissionResourceError as exc:
            return self._fail_closed_policy_change(exc)
        self._prune_unreferenced_spool()
        try:
            current_paths = set(self._candidate_files())
        except AdmissionResourceError as exc:
            self._revoke_all()
            self._prune_unreferenced_spool()
            self._record_global_policy_error(exc)
            self._prune_rejections()
            return 0, 1
        for stale in set(self.observed) - current_paths:
            self._revoke_relative(self._relative_name(stale))
            del self.observed[stale]
            self.processed.pop(stale, None)
        for path in current_paths:
            fingerprint = self._fingerprint(path)
            if fingerprint is None:
                self._revoke_relative(self._relative_name(path))
                self.processed.pop(path, None)
                continue
            if self.observed.get(path) != fingerprint:
                self.observed[path] = fingerprint
                self.processed.pop(path, None)
                continue
            if self.processed.get(path) == fingerprint:
                continue
            try:
                report, approval = admit_gcode(
                    path,
                    self.policy,
                    self.policy_path,
                    self.approval_dir,
                    self.gcode_root,
                    self.spool_dir,
                )
            except (OSError, PolicyError) as exc:
                self._revoke_relative(self._relative_name(path))
                self._record_policy_error(path, fingerprint, exc)
                self.processed[path] = fingerprint
                rejected += 1
                continue
            if self._fingerprint(path) != fingerprint:
                self.observed.pop(path, None)
                continue
            if report.accepted:
                if approval is None:
                    raise RuntimeError("accepted G-code has no approval record")
                self._revoke_relative(self._relative_name(path), keep=approval)
                admitted += 1
            else:
                self._revoke_relative(self._relative_name(path))
                self._record_rejection(report)
                rejected += 1
            self.processed[path] = fingerprint
        self._prune_unreferenced_spool()
        self._prune_rejections()
        return admitted, rejected

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self.running:
            self.scan_once()
            time.sleep(self.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcode-root", type=Path, required=True)
    parser.add_argument("--approval-dir", type=Path, required=True)
    parser.add_argument("--spool-dir", type=Path, required=True)
    parser.add_argument("--rejected-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--startup-preflight", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        daemon = AdmissionDaemon(
            args.gcode_root,
            args.approval_dir,
            args.spool_dir,
            args.rejected_dir,
            args.policy,
            interval=args.interval,
        )
        if args.once and args.startup_preflight:
            raise PolicyError("choose either --once or --startup-preflight")
        if args.startup_preflight:
            admitted, rejected = daemon.startup_preflight()
            print(json.dumps({"admitted": admitted, "rejected": rejected}))
        elif args.once:
            admitted, rejected = daemon.scan_once()
            print(json.dumps({"admitted": admitted, "rejected": rejected}))
        else:
            daemon.run()
        return 0
    except (OSError, PolicyError, ValueError) as exc:
        print("Error: %s" % (exc,), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

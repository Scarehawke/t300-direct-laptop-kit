#!/usr/bin/env python3
"""Prepare an isolated, offline-capable T300 analysis environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "third_party" / "laptop-tools-python314.lock.txt"
DEFAULT_VENV = REPO_ROOT / ".cache" / "python-tools"
DEFAULT_WHEELHOUSE = REPO_ROOT / ".cache" / "python-wheelhouse"
SHAKETUNE = REPO_ROOT / ".cache" / "community-sources" / "shaketune"


class ToolPreparationError(RuntimeError):
    pass


def run(command: list[str], env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.strip() or exc.stdout.strip() or "no error detail"
        else:
            detail = str(exc)
        raise ToolPreparationError(f"Command failed: {' '.join(command)}\n{detail}") from exc
    return result.stdout.strip()


def python_path(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def prepare(args: argparse.Namespace) -> None:
    if sys.version_info[:2] != (3, 14):
        raise ToolPreparationError(
            "The checked-in laptop dependency lock targets Python 3.14; "
            f"this interpreter is {sys.version_info.major}.{sys.version_info.minor}"
        )
    requirements = args.requirements.expanduser().resolve()
    if not requirements.is_file():
        raise ToolPreparationError(f"Requirements lock is missing: {requirements}")
    venv_dir = args.venv.expanduser().resolve()
    wheelhouse = args.wheelhouse.expanduser().resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if not python_path(venv_dir).is_file():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = str(python_path(venv_dir))
    pip_cache = REPO_ROOT / ".cache" / "pip"
    pip_cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PIP_CACHE_DIR"] = str(pip_cache)

    if not args.offline:
        run(
            [
                python,
                "-m",
                "pip",
                "download",
                "--dest",
                str(wheelhouse),
                "--requirement",
                str(requirements),
            ],
            env,
        )
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--requirement",
            str(requirements),
        ],
        env,
    )

    if not SHAKETUNE.is_dir():
        raise ToolPreparationError(
            "ShakeTune source is missing; run bin/prepare-community.py first"
        )
    smoke_env = env.copy()
    smoke_env["PYTHONPATH"] = str(SHAKETUNE)
    smoke_env["MPLCONFIGDIR"] = str(REPO_ROOT / ".cache" / "matplotlib")
    help_text = run([python, "-m", "shaketune.cli", "--help"], smoke_env)
    if "input_shaper" not in help_text:
        raise ToolPreparationError("ShakeTune CLI smoke test returned unexpected output")

    print(f"Python tools ready: {venv_dir}")
    print(f"Offline wheelhouse: {wheelhouse}")
    print("ShakeTune CLI smoke test: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--wheelhouse", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="install and verify using only the prepared wheelhouse",
    )
    return parser


def main() -> int:
    try:
        prepare(build_parser().parse_args())
        return 0
    except ToolPreparationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

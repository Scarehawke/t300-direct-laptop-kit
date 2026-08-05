#!/usr/bin/env python3
"""Compile and run the T300 macro smoke test on exact Klipper v0.12.0."""

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
KLIPPER = REPO_ROOT / ".cache" / "community-sources" / "klipper-v0.12.0"
PYTHON = REPO_ROOT / ".cache" / "python-tools" / "bin" / "python"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "klipper-v012"
EXPECTED_REVISION = "0d67d9c45d2dc39f8b4be7d1bb54b94b2698a2b6"
T300CTL = REPO_ROOT / "bin" / "t300ctl.py"


class HarnessError(RuntimeError):
    pass


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stdout.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise HarnessError(f"Command failed: {' '.join(command)}\n{detail}") from exc
    return result.stdout.strip()


def prepare_runtime_fixtures() -> list[Path]:
    spec = importlib.util.spec_from_file_location("t300ctl_klipper_test", T300CTL)
    if spec is None or spec.loader is None:
        raise HarnessError(f"Could not load {T300CTL}")
    t300ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t300ctl)

    base_path = FIXTURE_DIR / "t300-core.cfg"
    base = base_path.read_text(encoding="utf-8")
    legacy_include = "[include ../../../macros/t300_core.cfg]\n"
    if base.count(legacy_include) != 1:
        raise HarnessError("Synthetic T300 base fixture no longer matches its reviewed layout")
    base = base.replace(legacy_include, "")
    mainsail = t300ctl.read_mainsail_client().decode("utf-8").replace(
        "path: ~/gcode_files", "path: /tmp"
    )
    kamp = t300ctl.read_kamp_macro().decode("utf-8")
    runtime = t300ctl.read_runtime_macro().decode("utf-8")

    generated = REPO_ROOT / ".cache" / "klipper-runtime-fixtures"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "t300-runtime.cfg").write_text(
        base.rstrip() + "\n\n" + kamp + "\n" + mainsail + "\n" + runtime,
        encoding="utf-8",
    )
    cases = {
        "runtime-smoke.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg

END_PRINT
PAUSE
CANCEL_PRINT
G4 P250
T_RELEASE_MOTORS
""",
        "runtime-reject-missing-start-temps.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg
SHOULD_FAIL

START_PRINT
""",
        "runtime-reject-power-loss-resume.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg
SHOULD_FAIL

RESUME_INTERRUPTED
""",
        "runtime-unpaused-resume-cold.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg

RESUME
""",
        "runtime-reject-motor-release-while-paused.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg
SHOULD_FAIL

PAUSE
T_RELEASE_MOTORS
""",
        "runtime-reject-bed-overtemp.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg
SHOULD_FAIL

START_PRINT BED_TEMP=200 EXTRUDER_TEMP=215
""",
        "runtime-reject-nozzle-overtemp.test": """DICTIONARY linuxprocess.dict
CONFIG t300-runtime.cfg
SHOULD_FAIL

START_PRINT BED_TEMP=60 EXTRUDER_TEMP=500
""",
    }
    paths: list[Path] = []
    for filename, content in cases.items():
        path = generated / filename
        path.write_text(content, encoding="ascii")
        paths.append(path)
    return paths


def main() -> int:
    try:
        if not (KLIPPER / ".git").is_dir():
            raise HarnessError("Klipper source is missing; run bin/prepare-community.py")
        if not PYTHON.is_file():
            raise HarnessError("Python tools are missing; run bin/prepare-laptop-tools.py")
        revision = run(["git", "rev-parse", "HEAD"], KLIPPER)
        if revision != EXPECTED_REVISION:
            raise HarnessError(f"Expected Klipper {EXPECTED_REVISION}, found {revision}")

        config = KLIPPER / ".config"
        linux_config = KLIPPER / "test" / "configs" / "linuxprocess.config"
        if not config.is_file() or config.read_bytes() != linux_config.read_bytes():
            shutil.copyfile(linux_config, config)
            run(["make", "olddefconfig"], KLIPPER)
        dictionary = KLIPPER / "out" / "klipper.dict"
        if not dictionary.is_file():
            run(["make", "-j2"], KLIPPER)

        dict_dir = REPO_ROOT / ".cache" / "klipper-test-dicts"
        temp_dir = REPO_ROOT / ".cache" / "klipper-test-run"
        dict_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        dictionary_data = json.loads(dictionary.read_text(encoding="utf-8"))
        # Keep defensive defaults in the disposable dictionary only.
        dictionary_data["config"].setdefault("ADC_MAX", 4095)
        dictionary_data["config"].setdefault("PWM_MAX", 255)
        (dict_dir / "linuxprocess.dict").write_text(
            json.dumps(dictionary_data, separators=(",", ":")), encoding="utf-8"
        )
        for stale in (KLIPPER / "_test_.log", KLIPPER / "_test_output"):
            if stale.exists():
                stale.unlink()
        env = os.environ.copy()
        env["MPLCONFIGDIR"] = str(REPO_ROOT / ".cache" / "matplotlib")
        fixtures = sorted(FIXTURE_DIR.glob("*.test")) + prepare_runtime_fixtures()
        if not fixtures:
            raise HarnessError(f"No Klipper tests found below {FIXTURE_DIR}")
        env["PYTHONPATH"] = str(FIXTURE_DIR)
        output = run(
            [
                str(PYTHON),
                "scripts/test_klippy.py",
                "-d",
                str(dict_dir),
                "-t",
                str(temp_dir),
                *(str(path) for path in fixtures),
            ],
            KLIPPER,
            env,
        )
        print(output)
        print(
            f"PASS: {len(fixtures)} T300 core/runtime cases ran on Klipper {revision[:12]}"
        )
        return 0
    except HarnessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

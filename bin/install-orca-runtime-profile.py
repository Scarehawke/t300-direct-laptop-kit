#!/usr/bin/env python3
"""Install the reviewed T300 Orca machine preset for new projects."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys


PROFILE_NAME = "T300 AUDITED Runtime 0.4 - REVIEW ONLY"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "orcaslicer" / f"{PROFILE_NAME}.json"


class InstallError(RuntimeError):
    pass


def default_config_root() -> Path:
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "OrcaSlicer"
        return Path.home() / "AppData" / "Roaming" / "OrcaSlicer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OrcaSlicer"
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "OrcaSlicer"
    return Path.home() / ".config" / "OrcaSlicer"


def load_profile(path: Path) -> dict[str, object]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not read Orca profile: {path}") from exc
    if profile.get("type") != "machine":
        raise InstallError("runtime profile must be an Orca machine preset")
    if profile.get("name") != PROFILE_NAME:
        raise InstallError(f"runtime profile name must be {PROFILE_NAME!r}")
    required = {
        "gcode_flavor": "klipper",
        "enable_power_loss_recovery": "printer_configuration",
        "print_sequence": "by layer",
        "gcode_label_objects": "1",
        "exclude_object": "1",
        "machine_start_gcode": (
            "START_PRINT BED_TEMP=[bed_temperature_initial_layer_single] "
            "EXTRUDER_TEMP=[nozzle_temperature_initial_layer]\n"
        ),
        "machine_end_gcode": "END_PRINT\n",
    }
    for key, expected in required.items():
        if profile.get(key) != expected:
            raise InstallError(f"runtime profile has unexpected {key!r}")
    return profile


def install_profile(
    *,
    config_root: Path,
    profile_path: Path,
    apply: bool,
) -> list[str]:
    profile = load_profile(profile_path)
    machine_dir = config_root / "user" / "default" / "machine"
    target = machine_dir / f"{PROFILE_NAME}.json"
    config_file = config_root / "OrcaSlicer.conf"
    actions = [
        f"install machine preset: {target}",
        f"set default Orca machine preset to: {PROFILE_NAME}",
    ]
    if not apply:
        return actions

    if not config_file.is_file():
        raise InstallError(f"Orca config file does not exist: {config_file}")
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not read Orca config: {config_file}") from exc
    if not isinstance(config, dict):
        raise InstallError("Orca config root is not a JSON object")

    machine_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(config_file, config_file.with_name(f"{config_file.name}.bak-{stamp}"))
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    presets = config.setdefault("presets", {})
    if not isinstance(presets, dict):
        raise InstallError("Orca config presets field is not a JSON object")
    presets["machine"] = PROFILE_NAME
    config_file.write_text(json.dumps(config, indent="\t", ensure_ascii=False) + "\n", encoding="utf-8")
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=default_config_root())
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        actions = install_profile(
            config_root=args.config_root,
            profile_path=args.profile,
            apply=args.apply,
        )
    except InstallError as exc:
        parser.error(str(exc))
    mode = "apply" if args.apply else "dry run"
    print(f"mode: {mode}")
    for action in actions:
        print(action)
    if not args.apply:
        print("no files changed; rerun with --apply to install")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

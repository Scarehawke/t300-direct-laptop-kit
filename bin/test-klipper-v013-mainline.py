#!/usr/bin/env python3
"""Test the production config on pinned Klipper or reporting-only master."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t300_mainline.lockfile import load_lock  # noqa: E402
from t300_mainline.gcode_policy import approval_id  # noqa: E402
from t300_mainline.provision import ProvisionError, verify_stage  # noqa: E402
from t300_mainline.staging import (  # noqa: E402
    StagingError,
    apply_locked_patches,
    component_archive_name,
    extract_source,
    stage_rootfs,
    verify_cache,
)


STABLE_PYTHON = ROOT / ".cache/python-tools/bin/python"
CACHE = ROOT / ".cache/mainline/downloads"
LOCK_PATH = ROOT / "stack.lock.json"
STABLE_SOURCE = ROOT / ".cache/mainline/klipper-test-v013"
STABLE_COMMIT = "61c0c8d2ef40340781835dd53fb04cc7a454e37a"
T300_TEST_PINS = (
    "PA0",
    "PA3",
    "PA4",
    "PA7",
    "PA15",
    "PB3",
    "PB4",
    "PB5",
    "PB6",
    "PB7",
    "PB8",
    "PB9",
    "PB10",
    "PC0",
    "PC1",
    "PC2",
    "PC3",
    "PC4",
    "PC5",
    "PC7",
    "PC8",
    "PC10",
    "PC11",
    "PC13",
    "PC14",
    "PD2",
)


class HarnessError(RuntimeError):
    pass


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode:
        raise HarnessError(
            "command failed: %s\n%s" % (" ".join(command), result.stdout.strip())
        )
    return result.stdout.strip()


def _git_commit(source: Path, expected: str | None = None) -> str:
    if source.is_symlink() or not source.is_dir():
        raise HarnessError("component checkout is missing or unsafe: %s" % (source,))
    commit = run(["git", "rev-parse", "HEAD"], source)
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise HarnessError("component checkout did not resolve to a full commit")
    if expected is not None and commit != expected:
        raise HarnessError(
            "component checkout commit mismatch: expected %s, got %s"
            % (expected, commit)
        )
    return commit


def _require_clean_paths(source: Path, paths: tuple[str, ...]) -> None:
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *paths],
        cwd=source,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError("reviewed component files differ from their checkout")


def _copy_checkout(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns(
            ".git", ".config", "out", "_test_.log", "_test_output"
        ),
    )


def prepare_stable_source() -> tuple[Path, Path, str]:
    lock = load_lock(LOCK_PATH)
    component = next(item for item in lock["components"] if item["name"] == "klipper")
    if component["commit"] != STABLE_COMMIT:
        raise HarnessError("stack lock no longer names the reviewed Klipper commit")
    verify_cache(LOCK_PATH, CACHE, include_base=False)
    if not STABLE_SOURCE.exists():
        extract_source(CACHE / component_archive_name(component), STABLE_SOURCE)
    shutil.copy2(
        ROOT / "mainline/klippy/extras/t300_safety.py",
        STABLE_SOURCE / "klippy/extras/t300_safety.py",
    )
    config = STABLE_SOURCE / ".config"
    linux_config = STABLE_SOURCE / "test/configs/linuxprocess.config"
    if not config.exists() or config.read_bytes() != linux_config.read_bytes():
        shutil.copy2(linux_config, config)
        run(["make", "olddefconfig"], STABLE_SOURCE)
    dictionary = STABLE_SOURCE / "out/klipper.dict"
    if not dictionary.exists():
        run(["make", "-j2"], STABLE_SOURCE)
    return STABLE_SOURCE, dictionary, STABLE_COMMIT


def prepare_next_source(source: Path, temporary: Path) -> tuple[Path, Path, str]:
    commit = _git_commit(source)
    destination = temporary / "klipper-next"
    _copy_checkout(source, destination)
    safety_destination = destination / "klippy/extras/t300_safety.py"
    if safety_destination.exists():
        raise HarnessError("upstream Klipper already contains a t300_safety extra")
    shutil.copy2(ROOT / "mainline/klippy/extras/t300_safety.py", safety_destination)
    config = destination / ".config"
    linux_config = destination / "test/configs/linuxprocess.config"
    if not linux_config.is_file():
        raise HarnessError("next Klipper checkout has no Linux-process test config")
    shutil.copy2(linux_config, config)
    run(["make", "olddefconfig"], destination)
    run(["make", "-j2"], destination)
    dictionary = destination / "out/klipper.dict"
    if not dictionary.is_file():
        raise HarnessError("next Klipper build produced no protocol dictionary")
    return destination, dictionary, commit


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise HarnessError("expected one reviewed replacement in %s: %s" % (path, old))
    path.write_text(text.replace(old, new), encoding="utf-8")


def _locked_component(lock: dict[str, object], name: str) -> dict[str, object]:
    components = lock.get("components")
    if not isinstance(components, list):
        raise HarnessError("stack lock has no component list")
    for component in components:
        if isinstance(component, dict) and component.get("name") == name:
            return component
    raise HarnessError("stack lock has no %s component" % (name,))


def prepare_minimal_stage(
    directory: Path, mainsail_source: Path, kamp_source: Path
) -> Path:
    """Build a config-only fixture; it is intentionally not a deployable stage."""
    lock = load_lock(LOCK_PATH)
    mainsail = _locked_component(lock, "mainsail-config")
    kamp = _locked_component(lock, "kamp")
    _git_commit(mainsail_source, str(mainsail["commit"]))
    _git_commit(kamp_source, str(kamp["commit"]))
    _require_clean_paths(mainsail_source, ("client.cfg",))
    _require_clean_paths(
        kamp_source,
        (
            "Configuration/KAMP_Settings.cfg",
            "Configuration/Line_Purge.cfg",
            "Configuration/Smart_Park.cfg",
        ),
    )

    output = directory / "stage"
    config = output / "etc/t300/klipper"
    shutil.copytree(ROOT / "mainline/config/production", config)
    local = config / "local"
    local.mkdir()
    (local / "identity.cfg").write_text(
        "# Disposable Klipper compatibility fixture.\n"
        "[mcu]\n"
        "serial: /dev/serial/by-id/usb-Klipper_stm32f401xx_TEST-if00\n",
        encoding="ascii",
    )
    shutil.copy2(
        ROOT / "mainline/config/templates/calibration-bootstrap.cfg",
        local / "calibration.cfg",
    )
    shutil.copy2(
        ROOT / "mainline/policy/gcode-policy.json",
        output / "etc/t300/gcode-policy.json",
    )

    mainsail_text = (mainsail_source / "client.cfg").read_text(encoding="utf-8")
    old_virtual_sd = "path: ~/printer_data/gcodes"
    if mainsail_text.count(old_virtual_sd) != 1:
        raise HarnessError("pinned Mainsail virtual-SD path changed unexpectedly")
    mainsail_destination = config / "vendor/mainsail/client.cfg"
    mainsail_destination.parent.mkdir(parents=True)
    mainsail_destination.write_text(
        mainsail_text.replace(
            old_virtual_sd, "path: /var/lib/t300/moonraker-data/gcodes"
        ),
        encoding="utf-8",
    )

    kamp_work = directory / "kamp-reviewed"
    _copy_checkout(kamp_source, kamp_work)
    kamp_patches = [
        item
        for item in lock["compatibility_patches"]
        if item.get("component") == "kamp"
    ]
    if len(kamp_patches) != 1:
        raise HarnessError("stack lock must contain one reviewed KAMP patch")
    apply_locked_patches(
        {"compatibility_patches": kamp_patches},
        ROOT,
        {"kamp": kamp_work},
        output,
    )
    kamp_destination = config / "vendor/kamp"
    kamp_destination.mkdir()
    for filename in ("KAMP_Settings.cfg", "Line_Purge.cfg", "Smart_Park.cfg"):
        shutil.copy2(kamp_work / "Configuration" / filename, kamp_destination / filename)
    return output


def prepare_stage(
    directory: Path,
    supplied_stage: Path | None = None,
    supplied_manifest_sha256: str | None = None,
    fixture_components: tuple[Path, Path] | None = None,
) -> tuple[Path, bool, bool, Path | None]:
    output = directory / "stage"
    private_gergo = False
    if fixture_components is not None:
        if supplied_stage is not None:
            raise HarnessError("a minimal fixture cannot use a supplied stage")
        output = prepare_minimal_stage(directory, *fixture_components)
    elif supplied_stage is None:
        stage_rootfs(
            ROOT,
            LOCK_PATH,
            CACHE,
            output,
            "/dev/serial/by-id/usb-Klipper_stm32f401xx_TEST-if00",
            "10.42.42.0/24",
        )
    else:
        if supplied_manifest_sha256 is None:
            raise HarnessError("supplied stage requires its external manifest SHA-256")
        verified = verify_stage(supplied_stage, supplied_manifest_sha256)
        source = verified["root"] / "etc/t300"
        shutil.copytree(source, output / "etc/t300")
        private_gergo = verified["manifest"]["metadata"].get(
            "private_gergo_present"
        ) is True
    config = output / "etc/t300/klipper"
    policy = output / "etc/t300/gcode-policy.json"
    approvals = directory / "approvals"
    spool = directory / "spool"
    gcodes = directory / "gcodes"
    approvals.mkdir()
    spool.mkdir()
    gcodes.mkdir()
    replace_once(
        config / "safety.cfg",
        "policy_path: /etc/t300/gcode-policy.json",
        "policy_path: %s" % (policy,),
    )
    replace_once(
        config / "safety.cfg",
        "approval_dir: /var/lib/t300/gcode-approvals",
        "approval_dir: %s" % (approvals,),
    )
    replace_once(
        config / "safety.cfg",
        "spool_dir: /var/lib/t300/approved-gcodes",
        "spool_dir: %s" % (spool,),
    )
    replace_once(
        config / "machine.cfg",
        "path: /var/lib/t300/moonraker-data/gcodes",
        "path: %s" % (gcodes,),
    )
    replace_once(
        config / "vendor/mainsail/client.cfg",
        "path: /var/lib/t300/moonraker-data/gcodes",
        "path: %s" % (gcodes,),
    )
    os.chmod(policy, 0o444)
    safety = (config / "safety.cfg").read_text(encoding="utf-8")
    commissioning_lock = re.search(
        r"(?m)^commissioning_lock:\s*True\s*$", safety
    ) is not None
    maintenance_cfg = None
    if private_gergo:
        source = output / "etc/t300/maintenance/printer.cfg"
        maintenance_cfg = source.with_name("printer-harness.cfg")
        text = source.read_text(encoding="utf-8")
        resonance_include = "[include resonance.cfg]\n"
        if text.count(resonance_include) != 1:
            raise HarnessError("maintenance configuration resonance include changed")
        maintenance_cfg.write_text(
            text.replace(
                resonance_include,
                "# ADXL host MCU is deliberately absent from this syntax harness.\n",
            ),
            encoding="utf-8",
        )
    return config / "printer.cfg", commissioning_lock, private_gergo, maintenance_cfg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stable", "next"), default="stable")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--stage-manifest-sha256")
    parser.add_argument("--klipper-source", type=Path)
    parser.add_argument("--mainsail-config-source", type=Path)
    parser.add_argument("--kamp-source", type=Path)
    parser.add_argument("--test-python", type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def _write_next_report(path: Path, payload: dict[str, object]) -> None:
    path = path.expanduser()
    if path.is_symlink():
        raise HarnessError("next compatibility report path may not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % (path.name,), dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    next_report: dict[str, object] = {
        "schema_version": 1,
        "mode": args.mode,
        "deployable": False,
        "stable_commit": STABLE_COMMIT,
        "klipper_commit": None,
        "tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "passed": False,
    }
    try:
        if (args.stage is None) != (args.stage_manifest_sha256 is None):
            raise HarnessError("--stage and --stage-manifest-sha256 must be supplied together")
        next_sources = (
            args.klipper_source,
            args.mainsail_config_source,
            args.kamp_source,
        )
        if args.mode == "stable":
            if any(item is not None for item in next_sources) or args.report is not None:
                raise HarnessError("next-mode source and report options require --mode next")
            if args.test_python is not None:
                raise HarnessError("stable mode always uses the locked test Python")
            test_python = STABLE_PYTHON
            if not test_python.is_file():
                raise HarnessError("cached Klipper test Python is missing")
        else:
            if args.stage is not None:
                raise HarnessError("next mode cannot consume a deployable stage")
            if any(item is None for item in next_sources):
                raise HarnessError(
                    "next mode requires Klipper, Mainsail-config, and KAMP checkouts"
                )
            if args.report is None:
                raise HarnessError("next mode requires a non-deployable report path")
            test_python = (args.test_python or Path(sys.executable)).expanduser().absolute()
            try:
                resolved_test_python = test_python.resolve(strict=True)
            except OSError as exc:
                raise HarnessError("next test Python is missing or unsafe") from exc
            if not resolved_test_python.is_file():
                raise HarnessError("next test Python is missing or unsafe")

        with tempfile.TemporaryDirectory(prefix="t300-klipper-v013-") as temporary:
            temp = Path(temporary)
            if args.mode == "stable":
                source_dir, dictionary, tested_commit = prepare_stable_source()
                for stale in (source_dir / "_test_.log", source_dir / "_test_output"):
                    if stale.is_file():
                        stale.unlink()
                fixture_components = None
            else:
                assert args.klipper_source is not None
                assert args.mainsail_config_source is not None
                assert args.kamp_source is not None
                source_dir, dictionary, tested_commit = prepare_next_source(
                    args.klipper_source, temp
                )
                fixture_components = (
                    args.mainsail_config_source,
                    args.kamp_source,
                )
            next_report["klipper_commit"] = tested_commit
            printer_cfg, commissioning_lock, private_gergo, maintenance_cfg = prepare_stage(
                temp,
                args.stage,
                args.stage_manifest_sha256,
                fixture_components,
            )
            dictionary_data = json.loads(dictionary.read_text(encoding="utf-8"))
            dictionary_data["config"].setdefault("ADC_MAX", 4095)
            dictionary_data["config"].setdefault("PWM_MAX", 255)
            # The test MCU is Linux-based, but the production config retains
            # the exact live STM32 pin names.  Add only that reviewed pin set
            # to the disposable protocol dictionary so typos still fail.
            pin_enum = dictionary_data["enumerations"]["pin"]
            for index, pin in enumerate(T300_TEST_PINS, start=32):
                pin_enum[pin] = [index, 1]
            dict_dir = temp / "dict"
            dict_dir.mkdir()
            (dict_dir / "linuxprocess.dict").write_text(
                json.dumps(dictionary_data, separators=(",", ":")), encoding="utf-8"
            )
            cases = {
                "smoke.test": "STATUS\n",
                "operator-status.test": "T_STATUS\n",
                "reject-unarmed-home.test": "SHOULD_FAIL\n\nG28\n",
                "reject-velocity.test": "SHOULD_FAIL\n\nSET_VELOCITY_LIMIT VELOCITY=601\n",
                "reject-accel.test": "SHOULD_FAIL\n\nM204 S12001\n",
                "reject-current.test": "SHOULD_FAIL\n\nSET_TMC_CURRENT STEPPER=stepper_x CURRENT=1.2\n",
                "reject-debug.test": "SHOULD_FAIL\n\nSET_TMC_FIELD STEPPER=stepper_x FIELD=toff VALUE=0\n",
                "reject-motor-release.test": "SHOULD_FAIL\n\nM18\n",
                "reject-config-write.test": "SHOULD_FAIL\n\nSAVE_CONFIG\n",
                "reject-manual-probe.test": "SHOULD_FAIL\n\nMANUAL_PROBE\n",
                "reject-direct-probe.test": "SHOULD_FAIL\n\nPROBE\n",
                "reject-raw-heater.test": "SHOULD_FAIL\n\nSET_HEATER_TEMPERATURE HEATER=extruder TARGET=200\n",
                "reject-coordinate-reset.test": "SHOULD_FAIL\n\nG92 Z=100\n",
                "reject-runtime-offset.test": "SHOULD_FAIL\n\nSET_GCODE_OFFSET Z_ADJUST=-1 MOVE=1\n",
                "reject-rotation-distance.test": (
                    "SHOULD_FAIL\n\nSET_EXTRUDER_ROTATION_DISTANCE EXTRUDER=extruder DISTANCE=1\n"
                ),
                "reject-speed-factor.test": "SHOULD_FAIL\n\nM220 S101\n",
                "reject-flow-factor.test": "SHOULD_FAIL\n\nM221 S101\n",
                "reject-kamp-variable.test": (
                    "SHOULD_FAIL\n\n"
                    "SET_GCODE_VARIABLE MACRO=_KAMP_Settings "
                    "VARIABLE=purge_amount VALUE=300\n"
                ),
                "accept-reviewed-variable.test": (
                    "SET_GCODE_VARIABLE MACRO=RESUME "
                    "VARIABLE=idle_state VALUE=False\n"
                ),
                "reject-zero-accel-ratio.test": (
                    "SHOULD_FAIL\n\n"
                    "SET_VELOCITY_LIMIT ACCEL=0 ACCEL_TO_DECEL=1\n"
                ),
                "reject-legacy-sd-resume.test": "SHOULD_FAIL\n\nM24\n",
            }
            if commissioning_lock:
                cases["reject-bootstrap-arm.test"] = (
                    "SHOULD_FAIL\n\nT_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                )
            else:
                cases["accept-release-arm.test"] = (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                )
            fixtures: list[Path] = []
            for name, commands in cases.items():
                fixture = temp / name
                fixture.write_text(
                    "DICTIONARY linuxprocess.dict\nCONFIG %s\n%s"
                    % (printer_cfg, commands),
                    encoding="ascii",
                )
                fixtures.append(fixture)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "tests/fixtures/klipper-v012")
            env["MPLCONFIGDIR"] = str(ROOT / ".cache/matplotlib")
            run_dir = temp / "run"
            run_dir.mkdir()
            if maintenance_cfg is not None:
                maintenance_fixture = temp / "maintenance-load.test"
                maintenance_fixture.write_text(
                    "DICTIONARY linuxprocess.dict\nCONFIG %s\nSTATUS\n"
                    % (maintenance_cfg,),
                    encoding="ascii",
                )
                maintenance_output = run(
                    [
                        str(test_python),
                        "scripts/test_klippy.py",
                        "-d",
                        str(dict_dir),
                        "-t",
                        str(run_dir),
                        str(maintenance_fixture),
                    ],
                    source_dir,
                    env,
                )
                print(maintenance_output)
                print("PASS: exact private maintenance configuration loads")
            output = run(
                [
                    str(test_python),
                    "scripts/test_klippy.py",
                    "-d",
                    str(dict_dir),
                    "-t",
                    str(run_dir),
                    *(str(path) for path in fixtures),
                ],
                source_dir,
                env,
            )
            print(output)
            policy = temp / "stage/etc/t300/gcode-policy.json"
            writable_policy_fixture = temp / "reject-writable-policy.test"
            writable_policy_fixture.write_text(
                "DICTIONARY linuxprocess.dict\nCONFIG %s\nSHOULD_FAIL\n\nSTATUS\n"
                % (printer_cfg,),
                encoding="ascii",
            )
            os.chmod(policy, 0o644)
            try:
                writable_policy_output = run(
                    [
                        str(test_python),
                        "scripts/test_klippy.py",
                        "-d",
                        str(dict_dir),
                        "-t",
                        str(run_dir),
                        str(writable_policy_fixture),
                    ],
                    source_dir,
                    env,
                )
            finally:
                os.chmod(policy, 0o444)
            print(writable_policy_output)
            original_policy = policy.read_bytes()
            malformed_policy = json.loads(original_policy.decode("utf-8"))
            malformed_policy["max_velocity"] = True
            os.chmod(policy, 0o644)
            policy.write_text(
                json.dumps(malformed_policy, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(policy, 0o444)
            malformed_policy_fixture = temp / "reject-malformed-policy.test"
            malformed_policy_fixture.write_text(
                "DICTIONARY linuxprocess.dict\nCONFIG %s\nSHOULD_FAIL\n\nSTATUS\n"
                % (printer_cfg,),
                encoding="ascii",
            )
            try:
                malformed_policy_output = run(
                    [
                        str(test_python),
                        "scripts/test_klippy.py",
                        "-d",
                        str(dict_dir),
                        "-t",
                        str(run_dir),
                        str(malformed_policy_fixture),
                    ],
                    source_dir,
                    env,
                )
            finally:
                os.chmod(policy, 0o644)
                policy.write_bytes(original_policy)
                os.chmod(policy, 0o444)
            print(malformed_policy_output)
            if commissioning_lock:
                replace_once(
                    printer_cfg.parent / "safety.cfg",
                    "commissioning_lock: True",
                    "commissioning_lock: False",
                )
            gcodes = temp / "gcodes"
            approvals = temp / "approvals"
            spool = temp / "spool"
            snapshot_sources = {
                "snapshot.gcode": (
                    "SET_PRESSURE_ADVANCE ADVANCE=0.2\n"
                    "SET_PRESSURE_ADVANCE ADVANCE=0\n"
                    "M117 SNAPSHOT_LOADER_OK\n"
                ),
                "reject-pressure-over.gcode": (
                    "SET_PRESSURE_ADVANCE ADVANCE=0.20001\n"
                ),
                "reject-pressure-smooth.gcode": (
                    "SET_PRESSURE_ADVANCE ADVANCE=0.02 SMOOTH_TIME=0.04\n"
                ),
                "reject-pressure-extruder.gcode": (
                    "SET_PRESSURE_ADVANCE ADVANCE=0.02 EXTRUDER=extruder\n"
                ),
            }
            for relative, content in snapshot_sources.items():
                source = gcodes / relative
                source.write_text(content, encoding="ascii")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                protected = spool / (digest + ".gcode")
                shutil.copy2(source, protected)
                os.chmod(protected, 0o440)
                approval = approvals / (approval_id(relative, digest) + ".json")
                approval.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "relative_path": relative,
                            "sha256": digest,
                            "size": source.stat().st_size,
                            "policy_sha256": hashlib.sha256(
                                policy.read_bytes()
                            ).hexdigest(),
                            "spool_file": protected.name,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="ascii",
                )
                os.chmod(approval, 0o440)
            unapproved = gcodes / "unapproved.gcode"
            unapproved.write_text("M117 MUST_NOT_LOAD\n", encoding="ascii")
            loader_cases = {
                "accept-protected-snapshot.test": (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                    "SDCARD_PRINT_FILE FILENAME=snapshot.gcode\n"
                ),
                "reject-unapproved-snapshot.test": (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                    "SHOULD_FAIL\n\nSDCARD_PRINT_FILE FILENAME=unapproved.gcode\n"
                ),
                "reject-pressure-over.test": (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                    "SHOULD_FAIL\n\n"
                    "SDCARD_PRINT_FILE FILENAME=reject-pressure-over.gcode\n"
                ),
                "reject-pressure-smooth.test": (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                    "SHOULD_FAIL\n\n"
                    "SDCARD_PRINT_FILE FILENAME=reject-pressure-smooth.gcode\n"
                ),
                "reject-pressure-extruder.test": (
                    "T_CONFIRM_STEEL_SHEET CONFIRM=YES\n"
                    "SHOULD_FAIL\n\n"
                    "SDCARD_PRINT_FILE FILENAME=reject-pressure-extruder.gcode\n"
                ),
            }
            loader_fixtures = []
            for name, commands in loader_cases.items():
                fixture = temp / name
                fixture.write_text(
                    "DICTIONARY linuxprocess.dict\nCONFIG %s\n%s"
                    % (printer_cfg, commands),
                    encoding="ascii",
                )
                loader_fixtures.append(fixture)
            loader_output = run(
                [
                    str(test_python),
                    "scripts/test_klippy.py",
                    "-d",
                    str(dict_dir),
                    "-t",
                    str(run_dir),
                    *(str(path) for path in loader_fixtures),
                ],
                source_dir,
                env,
            )
            print(loader_output)
            total_cases = len(fixtures) + len(loader_fixtures) + 2
            print(
                "PASS: %d production cases on Klipper %s (%s)"
                % (total_cases, tested_commit[:12], args.mode)
            )
            if args.stage is not None:
                print(
                    "PASS: exact supplied stage %s (private GerGo: %s)"
                    % (args.stage_manifest_sha256[:12], "present" if private_gergo else "absent")
                )
            if args.mode == "next":
                next_report["passed"] = True
                next_report["production_stage_created"] = False
                assert args.report is not None
                _write_next_report(args.report, next_report)
        return 0
    except (HarnessError, OSError, ProvisionError, StagingError, ValueError) as exc:
        if args.mode == "next" and args.report is not None:
            next_report["error"] = str(exc)
            try:
                _write_next_report(args.report, next_report)
            except (HarnessError, OSError) as report_exc:
                print("Report error: %s" % (report_exc,), file=sys.stderr)
        print("Error: %s" % (exc,), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

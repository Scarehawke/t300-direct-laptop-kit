#!/usr/bin/env python3
"""Build an offline, review-only T300 core installation proposal."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
T300CTL_PATH = REPO_ROOT / "bin" / "t300ctl.py"
DEFAULT_OUTPUT = REPO_ROOT / ".cache" / "prepared-core"


class ProposalError(RuntimeError):
    pass


def load_t300ctl():
    spec = importlib.util.spec_from_file_location("t300ctl_proposal", T300CTL_PATH)
    if spec is None or spec.loader is None:
        raise ProposalError(f"Could not load {T300CTL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def config_root(value: Path) -> Path:
    supplied = value.expanduser().resolve()
    if (supplied / "printer.cfg").is_file():
        return supplied
    nested = supplied / "config-root"
    if (nested / "printer.cfg").is_file():
        return nested
    raise ProposalError(f"No printer.cfg found below {supplied}")


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProposalError(f"Could not read {label} at {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root",
        type=Path,
        required=True,
        help="saved config root or backup directory containing config-root/",
    )
    parser.add_argument(
        "--printer-cfg",
        type=Path,
        help="optional printer.cfg proposal to validate instead of config-root/printer.cfg",
    )
    parser.add_argument("--klipper-version", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        t300ctl = load_t300ctl()
        root = config_root(args.config_root)
        printer_path = (
            args.printer_cfg.expanduser().resolve()
            if args.printer_cfg is not None
            else root / "printer.cfg"
        )
        printer_cfg = read_text(printer_path, "printer.cfg")
        factory_macros = read_text(root / "Macro.cfg", "Macro.cfg")
        plr_cfg = read_text(root / "plr.cfg", "plr.cfg")
        t300ctl.validate_core_compatibility(
            printer_cfg,
            factory_macros,
            plr_cfg,
            args.klipper_version,
        )
        proposed_cfg, changed = t300ctl.patch_core_printer_cfg(printer_cfg)
        core = t300ctl.read_core_macro()

        output = args.output.expanduser().resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ProposalError(f"Output must be an absent or empty directory: {output}")
        output.mkdir(parents=True, exist_ok=True)

        core_path = output / t300ctl.CORE_MACRO_FILENAME
        core_path.write_bytes(core)
        diff = "\n".join(
            difflib.unified_diff(
                printer_cfg.splitlines(),
                proposed_cfg.splitlines(),
                fromfile="printer.cfg (saved input)",
                tofile="printer.cfg (include proposal only)",
                lineterm="",
            )
        )
        patch_path = output / "printer-include.patch"
        patch_path.write_text(diff + ("\n" if diff else ""), encoding="utf-8")

        manifest = {
            "schema": 1,
            "purpose": "review-only offline T300 core proposal",
            "klipper_version": args.klipper_version,
            "include_change_required": changed,
            "input_hashes": {
                "printer.cfg": sha256(printer_cfg.encode("utf-8")),
                "Macro.cfg": sha256(factory_macros.encode("utf-8")),
                "plr.cfg": sha256(plr_cfg.encode("utf-8")),
            },
            "payload": {
                t300ctl.CORE_MACRO_FILENAME: sha256(core),
            },
        }
        manifest_path = output / "proposal.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        readme_path = output / "README.txt"
        readme_path.write_text(
            "T300 core offline review bundle\n"
            "\n"
            "This is not a complete printer configuration. Do not upload the patch or\n"
            "replace printer.cfg from this directory. The live installer downloads the\n"
            "current printer.cfg, verifies it again, adds one include, backs up every\n"
            "config file, and rolls back automatically if Klipper does not restart.\n",
            encoding="utf-8",
        )

        artifacts = [core_path, patch_path, manifest_path, readme_path]
        sums = "".join(f"{sha256(path.read_bytes())}  {path.name}\n" for path in artifacts)
        (output / "SHA256SUMS").write_text(sums, encoding="utf-8")
        print("Saved factory compatibility contract: PASS")
        print(f"Review bundle: {output}")
        print(f"Core SHA-256: {sha256(core)}")
        print("No complete printer.cfg or purchased GerGo content was written.")
        return 0
    except (ProposalError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

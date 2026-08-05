#!/usr/bin/env python3
"""Build a complete, offline, review-only T300 runtime proposal."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
T300CTL_PATH = REPO_ROOT / "bin" / "t300ctl.py"
AUDITOR_PATH = REPO_ROOT / "bin" / "audit-t300-config.py"
DEFAULT_OUTPUT = REPO_ROOT / ".cache" / "prepared-runtime-20260804"


class ProposalError(RuntimeError):
    pass


def load_t300ctl():
    spec = importlib.util.spec_from_file_location("t300ctl_runtime_proposal", T300CTL_PATH)
    if spec is None or spec.loader is None:
        raise ProposalError(f"Could not load {T300CTL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_auditor():
    spec = importlib.util.spec_from_file_location("t300_runtime_auditor", AUDITOR_PATH)
    if spec is None or spec.loader is None:
        raise ProposalError(f"Could not load {AUDITOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def find_config_root(value: Path) -> Path:
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


def patch_ini_option(
    text: str,
    section: str,
    option: str,
    expected: str,
    replacement: str,
) -> tuple[str, bool]:
    """Patch one reviewed INI option while preserving layout and comments."""
    lines = text.splitlines(keepends=True)
    section_pattern = re.compile(r"^\s*\[\s*([^]]+?)\s*\]\s*(?:[#;].*)?$")
    option_pattern = re.compile(
        rf"^(\s*{re.escape(option)}\s*[:=]\s*)([^#;]*?)(\s*(?:[#;].*)?)$",
        re.IGNORECASE,
    )
    current_section = ""
    section_count = 0
    matches: list[tuple[int, re.Match[str], str]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        header = section_pattern.match(body)
        if header:
            current_section = header.group(1).strip().casefold()
            if current_section == section.casefold():
                section_count += 1
            continue
        if current_section == section.casefold():
            match = option_pattern.match(body)
            if match:
                matches.append((index, match, ending))

    if section_count != 1:
        raise ProposalError(
            f"Expected exactly one [{section}] section, found {section_count}"
        )
    if len(matches) != 1:
        raise ProposalError(
            f"Expected exactly one active [{section}] {option} option, found {len(matches)}"
        )

    index, match, ending = matches[0]
    current = match.group(2).strip()
    if current.casefold() == replacement.casefold():
        return text, False
    if current.casefold() != expected.casefold():
        raise ProposalError(
            f"[{section}] {option} is {current!r}, expected {expected!r} before patching"
        )
    lines[index] = match.group(1) + replacement + match.group(3) + ending
    return "".join(lines), True


def build_service_review(source_root: Path) -> tuple[str, dict[str, object]]:
    specs = (
        (
            "moonraker.conf",
            "timelapse",
            "saveframes",
            "True",
            "False",
            "Moonraker restart",
            "Do not retain a second ZIP copy of rendered timelapse frames",
            "recommended",
            "Confirm the existing rendered video is retained after a short test",
        ),
        (
            "crowsnest.conf",
            "crowsnest",
            "delete_log",
            "true",
            "false",
            "crowsnest restart",
            "Preserve the previous camera log for diagnostics",
            "conditional",
            "Confirm log rotation or define a size cap before applying",
        ),
    )
    patches: list[str] = []
    files: dict[str, object] = {}
    for (
        filename,
        section,
        option,
        expected,
        replacement,
        restart,
        reason,
        approval,
        precondition,
    ) in specs:
        path = source_root / filename
        original = read_text(path, filename)
        proposed, changed = patch_ini_option(
            original, section, option, expected, replacement
        )
        if changed:
            patches.extend(
                difflib.unified_diff(
                    original.splitlines(),
                    proposed.splitlines(),
                    fromfile=f"a/{filename}",
                    tofile=f"b/{filename}",
                    lineterm="",
                )
            )
            patches.append("")
        files[filename] = {
            "section": section,
            "option": option,
            "from": expected,
            "to": replacement,
            "changed": changed,
            "reason": reason,
            "approval": approval,
            "precondition": precondition,
            "requires": restart,
            "input_sha256": sha256(original.encode("utf-8")),
            "proposed_sha256": sha256(proposed.encode("utf-8")),
        }
    return "\n".join(patches), files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root",
        type=Path,
        required=True,
        help="saved config root or backup directory containing config-root/",
    )
    parser.add_argument("--klipper-version", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        t300ctl = load_t300ctl()
        auditor = load_auditor()
        source_root = find_config_root(args.config_root)
        supplied = args.config_root.expanduser().resolve()
        backup_root = supplied if (supplied / "config-root") == source_root else source_root.parent
        backup_verified = False
        if (backup_root / "SHA256SUMS").is_file() and (backup_root / "config-root") == source_root:
            checked = t300ctl.verify_backup(backup_root)
            backup_verified = True
            print(f"Source backup checksum verification: PASS ({checked} files)")
        printer_cfg = read_text(source_root / "printer.cfg", "printer.cfg")
        factory_macros = read_text(source_root / "Macro.cfg", "Macro.cfg")
        plr_cfg = read_text(source_root / "plr.cfg", "plr.cfg")
        service_patch, service_review = build_service_review(source_root)

        kamp = t300ctl.read_kamp_macro()
        mainsail = t300ctl.read_mainsail_client()
        runtime = t300ctl.read_runtime_macro()
        t300ctl.validate_runtime_compatibility(
            printer_cfg,
            factory_macros,
            plr_cfg,
            kamp.decode("utf-8"),
            args.klipper_version,
        )

        private_path = source_root / t300ctl.GERGO_MACRO_FILENAME
        if not private_path.is_file():
            raise ProposalError("Saved config is missing the privately supplied GerGo macro")
        private_names = set(t300ctl.macro_names(private_path.read_bytes()))
        proposed_names = (
            set(t300ctl.macro_names(kamp))
            | set(t300ctl.macro_names(mainsail))
            | set(t300ctl.macro_names(runtime))
        )
        if private_names & proposed_names:
            raise ProposalError(
                "The private GerGo package collides with a proposed macro owner; "
                "its source was not copied into the public proposal"
            )

        proposed_cfg, changed = t300ctl.patch_runtime_printer_cfg(printer_cfg)
        output = args.output.expanduser().resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ProposalError(f"Output must be an absent or empty directory: {output}")
        output.mkdir(parents=True, exist_ok=True)

        payload = output / "payload"
        payload.mkdir()
        payload_files = {
            t300ctl.KAMP_MACRO_FILENAME: kamp,
            t300ctl.MAINSAIL_CLIENT_FILENAME: mainsail,
            t300ctl.RUNTIME_MACRO_FILENAME: runtime,
        }
        for filename, content in payload_files.items():
            (payload / filename).write_bytes(content)

        diff = "\n".join(
            difflib.unified_diff(
                printer_cfg.splitlines(),
                proposed_cfg.splitlines(),
                fromfile="printer.cfg (saved input)",
                tofile="printer.cfg (proposed include order)",
                lineterm="",
            )
        )
        (output / "printer-include.patch").write_text(
            diff + ("\n" if diff else ""), encoding="utf-8"
        )
        (output / "service-settings.patch").write_text(
            service_patch, encoding="utf-8"
        )
        (output / "service-proposal.json").write_text(
            json.dumps(
                {
                    "status": "separate review-only service changes",
                    "printer_contacted": False,
                    "files": service_review,
                    "deferred": [
                        "camera resolution and max_fps pending a fresh v4l2 format capture",
                        "Moonraker authorization pending a fresh network and service capture",
                        "crowsnest delete_log change is blocked until log retention is bounded",
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        staged = output / "staged-config-private"
        shutil.copytree(source_root, staged)
        (staged / "printer.cfg").write_text(proposed_cfg, encoding="utf-8")
        for filename, content in payload_files.items():
            (staged / filename).write_bytes(content)

        tree = auditor.load_tree(staged)
        findings = auditor.audit(tree)
        (output / "audit.txt").write_text(
            auditor.format_text(tree, findings), encoding="utf-8"
        )
        (output / "audit.json").write_text(
            json.dumps(
                {
                    "root": "staged-config-private",
                    "files": [str(path) for path in tree.files],
                    "findings": [auditor.asdict(item) for item in findings],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        p0_findings = [item for item in findings if item.severity == "P0"]

        manifest = {
            "schema": 1,
            "status": "review-only; live installation is quarantined",
            "klipper_version": args.klipper_version,
            "mainsail_revision": t300ctl.MAINSAIL_CLIENT_REVISION,
            "kamp_revision": t300ctl.KAMP_REVISION,
            "include_change_required": changed,
            "source_backup_verified": backup_verified,
            "audit_p0_count": len(p0_findings),
            "service_review": service_review,
            "inputs": {
                "printer.cfg": sha256(printer_cfg.encode("utf-8")),
                "Macro.cfg": sha256(factory_macros.encode("utf-8")),
                "plr.cfg": sha256(plr_cfg.encode("utf-8")),
            },
            "payload": {name: sha256(content) for name, content in payload_files.items()},
            "private_source_copied_to_public_payload": False,
        }
        (output / "proposal.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (output / "README.txt").write_text(
            "T300 preliminary runtime proposal\n\n"
            "Nothing here was sent to the printer. The payload and include patch are\n"
            "review artifacts only. staged-config-private contains the owner's saved\n"
            "private macro solely so the complete include graph can be tested locally;\n"
            "the entire output directory is gitignored and must never be published.\n\n"
            "service-settings.patch is a separate proposal for Moonraker and crowsnest.\n"
            "It must not be bundled into a Klipper firmware-restart transaction. The\n"
            "crowsnest change is conditional on verified log rotation or a size cap. Verify\n"
            "all public artifact hashes with: sha256sum -c SHA256SUMS\n",
            encoding="utf-8",
        )

        public_artifacts = [
            *(payload / name for name in payload_files),
            output / "printer-include.patch",
            output / "service-settings.patch",
            output / "service-proposal.json",
            output / "proposal.json",
            output / "README.txt",
            output / "audit.txt",
            output / "audit.json",
        ]
        (output / "SHA256SUMS").write_text(
            "".join(
                f"{sha256(path.read_bytes())}  {path.relative_to(output)}\n"
                for path in public_artifacts
            ),
            encoding="utf-8",
        )

        if p0_findings:
            raise ProposalError(
                f"Staged configuration has {len(p0_findings)} P0 finding(s); review audit.txt"
            )
        print("Saved T300 runtime compatibility contract: PASS")
        print(f"Staged configuration audit: PASS ({len(findings)} findings, 0 P0)")
        print(f"Review bundle: {output}")
        print(f"Staged private config: {staged}")
        print("Live installation remains quarantined; no printer connection was used.")
        return 0
    except (ProposalError, RuntimeError, OSError, UnicodeDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

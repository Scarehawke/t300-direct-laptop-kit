#!/usr/bin/env python3
"""Download and verify pinned community sources used by the T300 audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "third_party" / "community-sources.lock.json"
DEFAULT_CACHE = REPO_ROOT / ".cache" / "community-sources"


class PreparationError(RuntimeError):
    pass


def run(command: list[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PreparationError(f"Required command is missing: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or "no error detail"
        raise PreparationError(f"Command failed: {' '.join(command)}\n{detail}") from exc
    return completed.stdout.strip()


def read_manifest(path: Path) -> list[dict[str, str]]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"Could not read source manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise PreparationError("Unsupported or malformed source manifest")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise PreparationError("Source manifest contains no sources")
    required = {"id", "url", "revision", "license", "purpose", "decision"}
    seen: set[str] = set()
    validated: list[dict[str, str]] = []
    for item in sources:
        if not isinstance(item, dict) or not required.issubset(item):
            raise PreparationError("Source manifest contains an incomplete entry")
        normalized = {key: str(item[key]) for key in required}
        source_id = normalized["id"]
        if source_id in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source_id):
            raise PreparationError(f"Unsafe or duplicate source id: {source_id!r}")
        revision = normalized["revision"]
        if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
            raise PreparationError(f"Source {source_id} has an invalid Git revision")
        if not normalized["url"].startswith("https://github.com/"):
            raise PreparationError(f"Source {source_id} is not an official HTTPS GitHub URL")
        seen.add(source_id)
        validated.append(normalized)
    return validated


def prepare_source(source: dict[str, str], cache: Path, offline: bool) -> str:
    destination = cache / source["id"]
    if not destination.exists():
        if offline:
            raise PreparationError(f"Missing offline source cache: {destination}")
        run(
            [
                "git",
                "-c",
                "advice.detachedHead=false",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                source["url"],
                str(destination),
            ]
        )
    if not (destination / ".git").is_dir():
        raise PreparationError(f"Cache path is not a Git checkout: {destination}")
    actual_url = run(["git", "remote", "get-url", "origin"], cwd=destination)
    if actual_url.rstrip("/") != source["url"].rstrip("/"):
        raise PreparationError(
            f"Source URL mismatch for {source['id']}: {actual_url} != {source['url']}"
        )
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=destination)
    if dirty:
        raise PreparationError(
            f"Source cache has local changes and will not be overwritten: {destination}"
        )
    revision = source["revision"]
    if not offline:
        run(["git", "fetch", "--filter=blob:none", "origin", revision], cwd=destination)
    try:
        run(["git", "cat-file", "-e", revision + "^{commit}"], cwd=destination)
    except PreparationError as exc:
        raise PreparationError(
            f"Pinned revision for {source['id']} is absent; rerun without --offline"
        ) from exc
    run(["git", "checkout", "--detach", "--force", revision], cwd=destination)
    head = run(["git", "rev-parse", "HEAD"], cwd=destination)
    if head != revision:
        raise PreparationError(f"Revision verification failed for {source['id']}")
    return head


def write_inventory(sources: list[dict[str, str]], cache: Path) -> Path:
    lines = ["# Prepared T300 community source inventory", ""]
    for source in sources:
        destination = cache / source["id"]
        head = run(["git", "rev-parse", "HEAD"], cwd=destination)
        lines.extend(
            [
                f"- {source['id']}: `{head}`",
                f"  - role: {source['decision']}",
                f"  - license: {source['license']}",
            ]
        )
    inventory = cache / "INVENTORY.md"
    inventory.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="verify an existing cache without using the network",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if shutil.which("git") is None:
            raise PreparationError("Git is required to prepare community sources")
        sources = read_manifest(args.manifest.expanduser().resolve())
        cache = args.cache_dir.expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        for source in sources:
            revision = prepare_source(source, cache, args.offline)
            print(f"OK  {source['id']:<27} {revision[:12]}  {source['decision']}")
        inventory = write_inventory(sources, cache)
        print(f"\nVerified {len(sources)} pinned sources. Inventory: {inventory}")
        return 0
    except PreparationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

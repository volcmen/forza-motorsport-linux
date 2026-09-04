# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .coordinator import (
    PrepareContext,
    build_prepare_plan,
    inspect_prepare,
    prepare,
    resolve_supported_host,
)
from .manifest import load_bootstrap_manifest
from .model import BootstrapError, canonical_json, plan_digest
from .snapshot import (
    ChangeRule,
    Snapshot,
    compare_snapshots,
    output_snapshot,
    read_snapshot,
    render_comparison,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the public bootstrap command surface without performing work."""
    parser = argparse.ArgumentParser(prog="forza-bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap = commands.add_parser("bootstrap")
    source = bootstrap.add_mutually_exclusive_group()
    source.add_argument("--bundle")
    source.add_argument("--build-from-source", action="store_true")
    bootstrap.add_argument("--threading-dll")
    mode = bootstrap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--rollback", action="store_true")

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output")

    compare = commands.add_parser("compare")
    compare.add_argument("--before")
    compare.add_argument("--after")

    return parser.parse_args(argv)


def _unavailable(_: argparse.Namespace) -> None:
    raise BootstrapError("command is unavailable in this build")


def _capture_current_snapshot() -> Snapshot:
    """Defer current-scope resolution to the bootstrap coordinator task."""
    raise BootstrapError("command is unavailable in this build")


def _prepare_context(arguments: argparse.Namespace) -> PrepareContext:
    repository = Path(__file__).resolve().parents[1]
    manifest = load_bootstrap_manifest(repository / "manifests/bootstrap-v1.toml")
    host = resolve_supported_host(os.environ)
    mode = (
        "source"
        if arguments.build_from_source
        else "local"
        if arguments.bundle is not None
        else "download"
    )
    return PrepareContext(
        manifest=manifest,
        host=host,
        repository_root=repository,
        acquisition_mode=mode,
        bundle_path=Path(arguments.bundle) if arguments.bundle is not None else None,
        output=sys.stdout,
    )


def _confirm_prepare(expected: str) -> str:
    return input(f"Type the complete PLAN_SHA256 {expected} to prepare: ")


def _run_bootstrap(arguments: argparse.Namespace) -> None:
    if arguments.rollback:
        raise BootstrapError("bootstrap rollback is unavailable in this build")
    context = _prepare_context(arguments)
    if arguments.check:
        inspection = inspect_prepare(context)
        plan = build_prepare_plan(context, inspection)
        print(canonical_json(plan).decode("ascii"))
        print(f"PLAN_SHA256={plan_digest(plan)}")
        return
    prepare(context, _confirm_prepare)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a parsed command, without claiming an unavailable command succeeded."""
    arguments = parse_args(argv)
    try:
        if arguments.command == "snapshot":
            output_snapshot(_capture_current_snapshot(), arguments.output, sys.stdout)
            return 0
        if arguments.command == "compare" and arguments.before and arguments.after:
            before = read_snapshot(arguments.before)
            after = read_snapshot(arguments.after)
            rules = tuple(
                ChangeRule(item.logical_path, "unchanged", item.sha256, item.mode)
                for item in before.records
            )
            comparison = compare_snapshots(before, after, rules)
            print(render_comparison(comparison), end="")
            return 0 if comparison.ok else 1
        if arguments.command == "bootstrap":
            _run_bootstrap(arguments)
            return 0
        _unavailable(arguments)
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0

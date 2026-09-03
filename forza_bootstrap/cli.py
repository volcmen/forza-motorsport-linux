# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .model import BootstrapError
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
        _unavailable(arguments)
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0

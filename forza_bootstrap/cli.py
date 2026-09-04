# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .coordinator import (
    FinishContext,
    PrepareContext,
    RecoveryContext,
    build_prepare_plan,
    capture_managed_snapshot,
    compare_transaction_snapshots,
    finish,
    finish_context_for_recovery,
    finish_context_from_prepare,
    inspect_bootstrap,
    inspect_prepare,
    prepare,
    resolve_supported_host,
    resume,
    rollback,
)
from .manifest import load_bootstrap_manifest
from .model import BootstrapError, canonical_json, plan_digest
from .snapshot import (
    ChangeRule,
    Comparison,
    Snapshot,
    compare_snapshots,
    output_snapshot,
    read_snapshot,
    render_comparison,
)
from .state import (
    BootstrapPhase,
    BootstrapState,
    load_latest_transaction_read_only,
    load_unfinished_transaction,
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
    """Resolve and capture the latest transaction's finite supported-host scope."""
    arguments = argparse.Namespace(bundle=None, build_from_source=False)
    context = _prepare_context(arguments)
    state = load_latest_transaction_read_only(context.host.state_root)
    if state is None:
        raise BootstrapError("no bootstrap transaction exists for snapshot identity")
    return capture_managed_snapshot(context, state)


def _compare_latest_transaction() -> Comparison:
    arguments = argparse.Namespace(bundle=None, build_from_source=False)
    context = _prepare_context(arguments)
    state = load_latest_transaction_read_only(context.host.state_root)
    if state is None:
        raise BootstrapError("no bootstrap transaction exists to compare")
    return compare_transaction_snapshots(context, state)


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


def _confirm_finish(expected: str) -> str:
    return input(f"Type the complete PLAN_SHA256 {expected} to finish: ")


def _finish_context(
    context: PrepareContext,
    arguments: argparse.Namespace,
    state: BootstrapState,
) -> FinishContext:
    threading = Path(arguments.threading_dll) if arguments.threading_dll is not None else None
    return finish_context_from_prepare(context, state, threading)


def _finish_context_for_recovery(
    context: PrepareContext,
    arguments: argparse.Namespace,
    state: BootstrapState,
) -> FinishContext:
    threading = Path(arguments.threading_dll) if arguments.threading_dll is not None else None
    return finish_context_for_recovery(context, state, threading)


def _recovery_context(
    context: PrepareContext,
    arguments: argparse.Namespace,
    state: BootstrapState,
) -> RecoveryContext:
    finish_context = None
    finish_phases = {
        BootstrapPhase.PLANNED_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
    }
    if (
        state.phase in finish_phases
        or (
            arguments.rollback
            and getattr(state, "finish_plan_sha256", None) is not None
        )
    ):
        finish_context = _finish_context_for_recovery(context, arguments, state)
    return RecoveryContext(
        state=state,
        state_root=context.host.state_root,
        output=sys.stdout,
        prepare_context=context,
        finish_context=finish_context,
    )


def _confirm_rollback() -> str:
    return input("Type ROLLBACK to roll back the composed bootstrap transaction: ")


def _run_bootstrap(arguments: argparse.Namespace) -> None:
    context = _prepare_context(arguments)
    if arguments.check:
        state = load_latest_transaction_read_only(context.host.state_root)
        if state is not None:
            print(f"PHASE={state.phase.value}")
            report = inspect_bootstrap(context, state)
            print(f"STATUS={report.status}")
            print(f"CACHE={report.cache}")
            print(f"PREFIX={report.prefix}")
            print(f"MANAGED_RECORDS={report.managed_records}")
            print(f"NEXT={report.next_action}")
            return
        print("PHASE=NOT_STARTED")
        inspection = inspect_prepare(context)
        plan = build_prepare_plan(context, inspection)
        print(canonical_json(plan).decode("ascii"))
        print(f"PLAN_SHA256={plan_digest(plan)}")
        print("NEXT=./setup bootstrap")
        return
    state = load_unfinished_transaction(context.host.state_root)
    if arguments.rollback:
        if state is None:
            raise BootstrapError("no unfinished bootstrap transaction exists")
        confirmation = _confirm_rollback()
        rollback(_recovery_context(context, arguments, state), confirmation)
        if confirmation != "ROLLBACK":
            print("No changes made.")
        return
    if state is not None:
        if state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX:
            finish(_finish_context(context, arguments, state), _confirm_finish)
            return
        if state.phase in {
            BootstrapPhase.RECOVERY_REQUIRED,
            BootstrapPhase.ROLLING_BACK,
        }:
            raise BootstrapError(
                "bootstrap transaction requires './setup bootstrap --rollback' "
                f"from {state.phase.value}"
            )
        resume(_recovery_context(context, arguments, state))
        return
    prepare(context, _confirm_prepare)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a parsed command, without claiming an unavailable command succeeded."""
    arguments = parse_args(argv)
    try:
        if arguments.command == "snapshot":
            output_snapshot(_capture_current_snapshot(), arguments.output, sys.stdout)
            return 0
        if arguments.command == "compare":
            if bool(arguments.before) != bool(arguments.after):
                raise BootstrapError(
                    "compare requires both --before and --after, or neither"
                )
            if arguments.before and arguments.after:
                before = read_snapshot(arguments.before)
                after = read_snapshot(arguments.after)
                rules = tuple(
                    ChangeRule(item.logical_path, "unchanged", item.sha256, item.mode)
                    for item in before.records
                )
                comparison = compare_snapshots(before, after, rules)
            else:
                comparison = _compare_latest_transaction()
            print(render_comparison(comparison), end="")
            return 0 if comparison.ok else 1
        if arguments.command == "bootstrap":
            _run_bootstrap(arguments)
            return 0
        _unavailable(arguments)
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt) as error:
        print(
            "\nStopped. Run './setup bootstrap --check' before resuming or rolling back. "
            "Keep the installation journals and backups.",
            file=sys.stderr,
        )
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    return 0

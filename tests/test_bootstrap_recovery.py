# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_bootstrap_finish import PATCH_LOGICAL_PATHS, FinishFixture
from test_bootstrap_prepare import PrepareFixture

import forza_bootstrap.coordinator as coordinator_module
from forza_bootstrap.coordinator import (
    PatchInspection,
    PrefixInspection,
    RecoveryContext,
    RecoveryOperations,
    default_recovery_operations,
    resume,
    rollback,
)
from forza_bootstrap.model import BootstrapError, canonical_json, plan_digest
from forza_bootstrap.proton import ToolDisposition
from forza_bootstrap.state import (
    BootstrapPhase,
    BootstrapState,
    create_transaction,
    load_transaction,
    load_unfinished_transaction,
    transition,
)


def state_at(root: Path, phase: BootstrapPhase) -> BootstrapState:
    state = create_transaction(root, "a" * 64)
    route = (
        BootstrapPhase.PREPARING,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        BootstrapPhase.PLANNED_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.READY_TO_ATTEMPT,
    )
    current = BootstrapPhase.NEW
    for target in route:
        state = transition(state, current, target)
        current = target
        if target is phase:
            return state
    if phase is BootstrapPhase.ACCEPTED:
        return transition(state, current, BootstrapPhase.ACCEPTED)
    raise AssertionError(f"unsupported fixture phase: {phase}")


class RecoveryFixture:
    def __init__(self, tmp_path: Path, phase: BootstrapPhase) -> None:
        self.root = tmp_path / "state"
        self.state = state_at(self.root, phase)
        self.output = io.StringIO()
        self.actions: list[str] = []
        self.patch = "applied"
        self.runtime = "installed"
        self.threading = "installed"
        self.tool = "created"

        def resume_prepare(_context: RecoveryContext, state: BootstrapState):
            self.actions.append("resume-prepare-inspect")
            assert self.tool in {"created", "adopted"}
            return transition(
                state,
                BootstrapPhase.PREPARING,
                BootstrapPhase.AWAITING_STEAM_PREFIX,
                compatibility_tool_disposition=self.tool,
            )

        def resume_finish(_context: RecoveryContext, state: BootstrapState):
            self.actions.append("resume-finish-inspect")
            assert (self.patch, self.runtime, self.threading) == (
                "applied",
                "installed",
                "installed",
            )
            if state.phase is BootstrapPhase.PLANNED_FINISH:
                state = transition(
                    state,
                    BootstrapPhase.PLANNED_FINISH,
                    BootstrapPhase.INSTALLING_FINISH,
                )
            return transition(
                state,
                BootstrapPhase.INSTALLING_FINISH,
                BootstrapPhase.READY_TO_ATTEMPT,
            )

        def restore_patches(_context: RecoveryContext, _state: BootstrapState):
            self.actions.append("patch-restore")
            if self.patch == "changed":
                raise BootstrapError("patch target changed")
            self.patch = "original"

        def rollback_runtime(_context: RecoveryContext, _state: BootstrapState):
            self.actions.append("runtime-rollback")
            if self.runtime == "recovery_required":
                raise BootstrapError("runtime child requires recovery")
            self.runtime = "rolled_back"

        def rollback_threading(_context: RecoveryContext, _state: BootstrapState):
            self.actions.append("threading-rollback")
            if self.threading == "changed":
                raise BootstrapError("licensed destination changed")
            self.threading = "rolled_back"

        def rollback_tool(_context: RecoveryContext, state: BootstrapState):
            self.actions.append("compat-tool-rollback")
            if state.compatibility_tool_disposition == "adopted":
                return
            if self.tool == "changed":
                raise BootstrapError("compatibility tool changed")
            self.tool = "absent"

        def status(attribute: str):
            def inspect(_context: RecoveryContext, _state: BootstrapState) -> str:
                value = getattr(self, attribute)
                if value in {"original", "rolled_back", "absent"}:
                    return "rolled_back"
                if value == "adopted":
                    return "not_applicable"
                return "pending"

            return inspect

        def inspect_tool(_context: RecoveryContext, state: BootstrapState) -> str:
            if state.compatibility_tool_disposition == "adopted":
                return "not_applicable"
            return status("tool")(_context, state)

        self.operations = RecoveryOperations(
            resume_prepare=resume_prepare,
            resume_finish=resume_finish,
            inspect_patches=status("patch"),
            restore_patches=restore_patches,
            inspect_runtime=status("runtime"),
            rollback_runtime=rollback_runtime,
            inspect_threading=status("threading"),
            rollback_threading=rollback_threading,
            inspect_tool=inspect_tool,
            rollback_tool=rollback_tool,
            event=self.actions.append,
        )
        self.context = RecoveryContext(
            state=self.state,
            state_root=self.root,
            output=self.output,
            operations=self.operations,
        )


@pytest.mark.parametrize(
    ("phase", "expected", "inspection"),
    (
        (
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            "resume-prepare-inspect",
        ),
        (
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            None,
        ),
        (
            BootstrapPhase.PLANNED_FINISH,
            BootstrapPhase.READY_TO_ATTEMPT,
            "resume-finish-inspect",
        ),
        (
            BootstrapPhase.INSTALLING_FINISH,
            BootstrapPhase.READY_TO_ATTEMPT,
            "resume-finish-inspect",
        ),
        (BootstrapPhase.READY_TO_ATTEMPT, BootstrapPhase.READY_TO_ATTEMPT, None),
    ),
)
def test_resume_routes_every_durable_phase_through_real_inspection(
    tmp_path: Path,
    phase: BootstrapPhase,
    expected: BootstrapPhase,
    inspection: str | None,
) -> None:
    fixture = RecoveryFixture(tmp_path, phase)

    result = resume(fixture.context)

    assert result.phase is expected
    assert fixture.actions == ([] if inspection is None else [inspection])
    assert load_transaction(fixture.root, result.transaction_id) == result


def test_resume_ambiguity_enters_recovery_required_with_private_journal(
    tmp_path: Path,
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.PREPARING)
    fixture.operations = replace(
        fixture.operations,
        resume_prepare=lambda *_args: (_ for _ in ()).throw(
            BootstrapError("Prepare identity is ambiguous")
        ),
    )
    fixture.context = replace(fixture.context, operations=fixture.operations)

    with pytest.raises(BootstrapError, match="identity is ambiguous"):
        resume(fixture.context)

    durable = load_unfinished_transaction(fixture.root)
    assert durable is not None and durable.phase is BootstrapPhase.RECOVERY_REQUIRED
    assert str(fixture.root / durable.transaction_id) in fixture.output.getvalue()


def test_resume_marks_latest_owner_checkpoint_without_masking_original_error(
    tmp_path: Path,
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.PREPARING)

    def checkpoint_then_fail(
        _context: RecoveryContext, state: BootstrapState
    ) -> BootstrapState:
        transition(
            state,
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
        )
        raise BootstrapError("original owner recovery failure")

    fixture.operations = replace(
        fixture.operations,
        resume_prepare=checkpoint_then_fail,
    )
    fixture.context = replace(fixture.context, operations=fixture.operations)

    with pytest.raises(BootstrapError, match="original owner recovery failure"):
        resume(fixture.context)

    durable = load_unfinished_transaction(fixture.root)
    assert durable is not None and durable.phase is BootstrapPhase.RECOVERY_REQUIRED
    assert str(fixture.root / durable.transaction_id) in fixture.output.getvalue()


def test_finish_plan_replacement_window_keeps_prepare_plan_reconstructable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    state = state_at(root, BootstrapPhase.AWAITING_STEAM_PREFIX)
    transaction = root / state.transaction_id
    prepare_plan = {
        "schema": 1,
        "phase": "prepare",
        "manifest_sha256": state.manifest_sha256,
    }
    finish_plan = {
        "schema": 1,
        "phase": "finish",
        "manifest_sha256": state.manifest_sha256,
    }
    for name, value in (
        ("prepare-plan.json", prepare_plan),
        ("plan.json", finish_plan),
    ):
        path = transaction / name
        path.write_text(json.dumps(value), encoding="ascii")
        path.chmod(0o600)

    recovered = resume(
        RecoveryContext(
            state=state,
            state_root=root,
            output=io.StringIO(),
        )
    )

    assert recovered.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert json.loads((transaction / "prepare-plan.json").read_text()) == prepare_plan


def test_prepare_preserves_plan_and_exact_tool_publication_record(
    tmp_path: Path,
) -> None:
    fixture = PrepareFixture(tmp_path)

    state = fixture.run()

    transaction = fixture.state / state.transaction_id
    assert (transaction / "prepare-plan.json").read_bytes() == (
        transaction / "plan.json"
    ).read_bytes()
    record = json.loads((transaction / "compatibility-tool.json").read_text())
    assert record == {
        "schema": 1,
        "destination": {
            "root": "steam",
            "relative": "compatibilitytools.d/GE-Proton11-3-FM",
        },
        "disposition": "created",
        "identity": {
            "name": "GE-Proton11-3-FM",
            "base_release": "GE-Proton11-3",
            "vdf_sha256": "c" * 64,
            "managed_tree_sha256": "d" * 64,
            "marker_sha256": "e" * 64,
        },
        "root_device": 1,
        "root_inode": 100,
        "published_tree_sha256": "d" * 64,
    }


def test_default_resume_completes_interrupted_prepare_without_republishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path)
    crashed = False
    publications = 0
    original_publish = fixture.context.operations.publish_fm

    def publish(*args: object):
        nonlocal publications
        publications += 1
        record = original_publish(*args)  # type: ignore[arg-type]
        record.destination.mkdir(exist_ok=True)
        info = record.destination.stat()
        return replace(record, root_device=info.st_dev, root_inode=info.st_ino)

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "publish-fm-tool" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=publish,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None and durable.phase is BootstrapPhase.PREPARING
    assert publications == 1
    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "d" * 64,
            },
        )(),
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=fixture.context,
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert publications == 1


def test_prepare_resume_refuses_same_content_tool_at_a_replaced_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path)
    original_publish = fixture.context.operations.publish_fm

    def publish(*args: object):
        record = original_publish(*args)  # type: ignore[arg-type]
        record.destination.mkdir()
        info = record.destination.stat()
        return replace(record, root_device=info.st_dev, root_inode=info.st_ino)

    def interrupt(boundary: str) -> None:
        if boundary == "publish-fm-tool":
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=publish,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None
    destination = fixture.compatibility / "GE-Proton11-3-FM"
    destination.rename(fixture.compatibility / "displaced")
    destination.mkdir()
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "d" * 64,
            },
        )(),
    )
    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=fixture.context,
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(BootstrapError, match="compatibility tool changed"):
        resume(context)


@pytest.mark.parametrize("damage", ("missing", "malformed"))
def test_prepare_resume_never_recreates_baseline_after_tool_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    original_publish = fixture.context.operations.publish_fm

    def publish(*args: object):
        record = original_publish(*args)  # type: ignore[arg-type]
        record.destination.mkdir()
        info = record.destination.stat()
        return replace(record, root_device=info.st_dev, root_inode=info.st_ino)

    def interrupt(boundary: str) -> None:
        if boundary == "publish-fm-tool":
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=publish,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None
    before_path = fixture.state / durable.transaction_id / "before.json"
    if damage == "missing":
        before_path.unlink()
        damaged = None
    else:
        before_path.write_text("{}", encoding="ascii")
        before_path.chmod(0o600)
        damaged = before_path.read_bytes()
    captures = 0

    def capture(*args: object):
        nonlocal captures
        captures += 1
        return fixture.context.operations.capture_before(*args)

    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "d" * 64,
            },
        )(),
    )
    resume_operations = replace(
        fixture.context.operations,
        capture_before=capture,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=replace(fixture.context, operations=resume_operations),
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(BootstrapError, match="before snapshot"):
        resume(context)

    assert captures == 0
    assert before_path.exists() is (damage == "malformed")
    if damaged is not None:
        assert before_path.read_bytes() == damaged


def test_prepare_resume_refuses_planned_absent_tool_without_ownership_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path)

    def interrupt(boundary: str) -> None:
        if boundary == "acquire-bundle":
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None
    destination = fixture.compatibility / "GE-Proton11-3-FM"
    destination.mkdir()
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "d" * 64,
            },
        )(),
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=replace(
            fixture.context,
            operations=replace(fixture.context.operations, boundary=lambda _name: None),
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(
        BootstrapError, match="compatibility-tool recovery is ambiguous"
    ):
        resume(context)

    assert not (
        fixture.state / durable.transaction_id / "compatibility-tool.json"
    ).exists()


def test_default_rollback_from_interrupted_prepare_reaches_created_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path)
    original_publish = fixture.context.operations.publish_fm
    present = True

    def publish(*args: object):
        record = original_publish(*args)  # type: ignore[arg-type]
        record.destination.mkdir()
        info = record.destination.stat()
        return replace(record, root_device=info.st_dev, root_inode=info.st_ino)

    def interrupt(boundary: str) -> None:
        if boundary == "publish-fm-tool":
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=publish,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None and durable.phase is BootstrapPhase.PREPARING

    def inspect(*_args: object):
        return type(
            "Inspection",
            (),
            {
                "disposition": (
                    ToolDisposition.ADOPTED if present else ToolDisposition.ABSENT
                ),
                "identity": fixture.identity if present else None,
                "tree_sha256": "d" * 64 if present else None,
            },
        )()

    def remove(_record: object) -> ToolDisposition:
        nonlocal present
        present = False
        return ToolDisposition.ABSENT

    monkeypatch.setattr(coordinator_module, "inspect_fm", inspect)
    monkeypatch.setattr(coordinator_module, "rollback_published_fm", remove)
    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=fixture.context,
    )

    result = rollback(context, confirm="ROLLBACK")

    assert result.phase is BootstrapPhase.ROLLED_BACK
    assert present is False


def test_default_rollback_preserves_changed_adopted_tool_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PrepareFixture(tmp_path, disposition=ToolDisposition.ADOPTED)
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "0" * 64,
            },
        )(),
    )
    state = fixture.run()
    removed = False

    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": replace(fixture.identity, vdf_sha256="9" * 64),
                "tree_sha256": "9" * 64,
            },
        )(),
    )

    def remove(_record: object) -> ToolDisposition:
        nonlocal removed
        removed = True
        return ToolDisposition.ABSENT

    monkeypatch.setattr(coordinator_module, "rollback_published_fm", remove)
    context = RecoveryContext(
        state,
        fixture.state,
        fixture.stdout,
        prepare_context=replace(
            fixture.context,
            operations=replace(fixture.context.operations, boundary=lambda _name: None),
        ),
    )

    with pytest.raises(BootstrapError, match="compatibility tool changed"):
        rollback(context, confirm="ROLLBACK")

    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None and durable.phase is BootstrapPhase.RECOVERY_REQUIRED
    assert removed is False


@pytest.mark.parametrize(
    "boundary",
    (
        "write-before-snapshot",
        "acquire-ge",
        "acquire-bundle",
        "publish-fm-tool",
        "checkpoint-awaiting-prefix",
    ),
)
def test_default_resume_after_every_prepare_boundary_has_one_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    fixture = PrepareFixture(tmp_path)
    crashed = False
    publications = 0
    original_publish = fixture.context.operations.publish_fm

    def publish(*args: object):
        nonlocal publications
        publications += 1
        record = original_publish(*args)  # type: ignore[arg-type]
        record.destination.mkdir(exist_ok=True)
        info = record.destination.stat()
        return replace(record, root_device=info.st_dev, root_inode=info.st_ino)

    def interrupt(current: str) -> None:
        nonlocal crashed
        if current == boundary and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.context.operations,
            publish_fm=publish,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state)
    assert durable is not None

    def inspect(*_args: object):
        record = fixture.state / durable.transaction_id / "compatibility-tool.json"
        if not record.exists():
            return type(
                "Inspection",
                (),
                {
                    "disposition": ToolDisposition.ABSENT,
                    "identity": None,
                    "tree_sha256": None,
                },
            )()
        return type(
            "Inspection",
            (),
            {
                "disposition": ToolDisposition.ADOPTED,
                "identity": fixture.identity,
                "tree_sha256": "d" * 64,
            },
        )()

    monkeypatch.setattr(coordinator_module, "inspect_fm", inspect)
    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    context = RecoveryContext(
        durable,
        fixture.state,
        fixture.stdout,
        prepare_context=fixture.context,
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert publications == 1


def test_default_resume_completes_interrupted_finish_from_actual_records(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "runtime-status" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None and durable.phase is BootstrapPhase.INSTALLING_FINISH
    fixture.context = replace(
        fixture.context,
        state=durable,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=fixture.context,
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert fixture.actions.count("runtime-install") == 1


def test_finish_persists_confirmed_plan_digest_write_once(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    expected = fixture.finish_plan.sha256

    state = fixture.run()

    assert state.finish_plan_sha256 == expected
    with pytest.raises(BootstrapError, match="write-once"):
        transition(
            state,
            BootstrapPhase.READY_TO_ATTEMPT,
            BootstrapPhase.ACCEPTED,
            finish_plan_sha256="7" * 64,
        )


def test_resume_rejects_invalid_nested_finish_plan_before_any_owner(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "runtime-status" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    plan_path = fixture.state_root / durable.transaction_id / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["patch"]["disposition"] = 17
    plan_path.write_text(json.dumps(plan), encoding="ascii")
    plan_path.chmod(0o600)
    owner_calls: list[str] = []

    def plan_threading(_context: object):
        owner_calls.append("licensed")
        return fixture.licensed_action

    operations = replace(
        fixture.context.operations,
        plan_threading=plan_threading,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(
            fixture.context,
            state=durable,
            operations=operations,
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(BootstrapError, match="Finish recovery plan is invalid"):
        resume(context)

    assert owner_calls == []


def _crashed_finish_for_plan_validation(
    fixture: FinishFixture,
) -> BootstrapState:
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "runtime-status" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    return durable


def _republish_validly_digested_finish_plan(
    fixture: FinishFixture,
    state: BootstrapState,
    document: dict[str, object],
) -> BootstrapState:
    transaction = fixture.state_root / state.transaction_id
    digest = plan_digest(document)
    plan_path = transaction / "plan.json"
    plan_path.write_bytes(canonical_json(document))
    plan_path.chmod(0o600)
    state_path = transaction / "state.json"
    state_value = json.loads(state_path.read_text())
    state_value["finish_plan_sha256"] = digest
    state_path.write_bytes(canonical_json(state_value))
    state_path.chmod(0o600)
    return load_transaction(fixture.state_root, state.transaction_id)


def _resume_rejecting_any_finish_owner(
    fixture: FinishFixture,
    state: BootstrapState,
) -> list[str]:
    owner_calls: list[str] = []

    def forbidden_owner(*_args: object) -> PrefixInspection:
        owner_calls.append("validate-prefix")
        raise BootstrapError("Finish owner invoked before plan rejection")

    operations = replace(
        fixture.context.operations,
        validate_prefix=forbidden_owner,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(
            fixture.context,
            state=state,
            operations=operations,
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(BootstrapError, match="Finish recovery plan is invalid"):
        resume(context)
    return owner_calls


def test_resume_rejects_licensed_before_path_mismatch_before_any_owner(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    durable = _crashed_finish_for_plan_validation(fixture)
    transaction = fixture.state_root / durable.transaction_id
    document = json.loads((transaction / "plan.json").read_text())
    licensed = json.loads((transaction / "licensed-plan.json").read_text())
    licensed["before"]["logical_path"] = str(
        fixture.system32 / "foreign-threading-target"
    )
    (transaction / "licensed-plan.json").write_bytes(canonical_json(licensed))
    (transaction / "licensed-plan.json").chmod(0o600)
    document["licensed"]["private_action_sha256"] = hashlib.sha256(
        canonical_json(licensed)
    ).hexdigest()
    durable = _republish_validly_digested_finish_plan(
        fixture, durable, document
    )

    owner_calls = _resume_rejecting_any_finish_owner(fixture, durable)

    assert owner_calls == []


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-state",
        "extra-state",
        "replaced-path",
        "duplicate-path",
        "wrong-after-hash",
    ),
)
def test_resume_rejects_noncanonical_patch_coverage_before_any_owner(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = FinishFixture(tmp_path)
    durable = _crashed_finish_for_plan_validation(fixture)
    transaction = fixture.state_root / durable.transaction_id
    document = json.loads((transaction / "plan.json").read_text())
    if mutation == "missing-state":
        document["patch"]["states"].pop()
        document["patch_after_sha256"].pop()
    elif mutation == "extra-state":
        document["patch"]["states"].append("original")
        document["patch_after_sha256"].append(fixture.patch_hash)
    elif mutation == "wrong-after-hash":
        document["patch_after_sha256"][0] = "7" * 64
    else:
        replacement = (
            "prefix/controller"
            if mutation == "duplicate-path"
            else "compat/foreign-controller"
        )
        rule = next(
            item
            for item in document["snapshot_rules"]
            if item["logical_path"] == "compat/controller"
        )
        rule["logical_path"] = replacement
    durable = _republish_validly_digested_finish_plan(
        fixture, durable, document
    )

    owner_calls = _resume_rejecting_any_finish_owner(fixture, durable)

    assert owner_calls == []


def test_resume_rejects_patch_hashes_permuted_between_targets_before_any_owner(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    durable = _crashed_finish_for_plan_validation(fixture)
    transaction = fixture.state_root / durable.transaction_id
    document = json.loads((transaction / "plan.json").read_text())
    distinct_hashes = [
        hashlib.sha256(path.encode("ascii")).hexdigest()
        for path in PATCH_LOGICAL_PATHS
    ]
    rules = {item["logical_path"]: item for item in document["snapshot_rules"]}
    for logical_path, digest in zip(
        PATCH_LOGICAL_PATHS, distinct_hashes, strict=True
    ):
        rules[logical_path]["after_sha256"] = digest
    document["patch_after_sha256"] = distinct_hashes.copy()
    document["patch_after_sha256"][0], document["patch_after_sha256"][2] = (
        document["patch_after_sha256"][2],
        document["patch_after_sha256"][0],
    )
    durable = _republish_validly_digested_finish_plan(fixture, durable, document)

    owner_calls = _resume_rejecting_any_finish_owner(fixture, durable)

    assert owner_calls == []


@pytest.mark.parametrize(
    "mutation",
    ("missing", "duplicate", "extra", "threading-hash", "threading-mode"),
)
def test_resume_rejects_noncanonical_snapshot_rule_coverage_before_any_owner(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = FinishFixture(tmp_path)
    durable = _crashed_finish_for_plan_validation(fixture)
    transaction = fixture.state_root / durable.transaction_id
    document = json.loads((transaction / "plan.json").read_text())
    marker = next(
        item
        for item in document["snapshot_rules"]
        if item["logical_path"] == "compat/tool-marker"
    )
    if mutation == "missing":
        document["snapshot_rules"].remove(marker)
    elif mutation == "duplicate":
        document["snapshot_rules"].append(dict(marker))
    elif mutation == "extra":
        document["snapshot_rules"].append(
            {
                "logical_path": "user/.local/bin/unmanaged-extra",
                "policy": "unchanged",
                "after_sha256": None,
                "after_mode": None,
            }
        )
    else:
        threading = next(
            item
            for item in document["snapshot_rules"]
            if item["logical_path"] == "prefix/threading"
        )
        if mutation == "threading-hash":
            threading["after_sha256"] = "7" * 64
        else:
            threading["after_mode"] = 0o600
    durable = _republish_validly_digested_finish_plan(
        fixture, durable, document
    )

    owner_calls = _resume_rejecting_any_finish_owner(fixture, durable)

    assert owner_calls == []


def test_resume_revalidates_recorded_prefix_identity_before_owner_mutation(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "runtime-status" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    owner_calls: list[str] = []

    def plan_threading(_context: object):
        owner_calls.append("licensed")
        return fixture.licensed_action

    operations = replace(
        fixture.context.operations,
        validate_prefix=lambda _context: PrefixInspection("9:90", "9:91"),
        plan_threading=plan_threading,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(
            fixture.context,
            state=durable,
            operations=operations,
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    with pytest.raises(BootstrapError, match="prefix identity changed"):
        resume(context)

    assert owner_calls == []


def test_licensed_resume_reconstructs_original_action_without_replanning_destination(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "install-threading" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    resumed_actions: list[dict[str, object]] = []

    def refuse_replan(_context: object):
        raise BootstrapError("replanned against mutated licensed destination")

    def resume_licensed(_context: object, action: object, _journal: Path) -> None:
        resumed_actions.append(action.private_json())  # type: ignore[union-attr]

    operations = replace(
        fixture.context.operations,
        plan_threading=refuse_replan,
        install_threading=resume_licensed,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(
            fixture.context,
            state=durable,
            operations=operations,
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert resumed_actions == [fixture.licensed_action.private_json()]


def test_licensed_resume_uses_journal_owner_to_verify_durable_keep_action(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    private_record = next(
        item
        for item in fixture.after.records
        if item.logical_path == "prefix/threading"
    )
    fixture.pre_finish = replace(
        fixture.pre_finish,
        records=tuple(
            private_record if item.logical_path == "prefix/threading" else item
            for item in fixture.pre_finish.records
        ),
    )
    fixture.licensed_action = replace(
        fixture.licensed_action,
        before=replace(
            private_record,
            logical_path=fixture.licensed_action.destination_logical,
        ),
        disposition="keep",
    )
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "install-threading" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    verified: list[dict[str, object]] = []

    def verify_licensed(_context: object, action: object, _journal: Path) -> None:
        verified.append(action.private_json())  # type: ignore[union-attr]

    resumed_operations = replace(
        fixture.context.operations,
        install_threading=verify_licensed,
        boundary=lambda _name: None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(
            fixture.context,
            state=durable,
            operations=resumed_operations,
        ),
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert verified == [fixture.licensed_action.private_json()]


def test_default_resume_rechecks_completed_patch_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "patch-apply" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    resumed_operations = replace(
        fixture.context.operations, boundary=lambda _name: None
    )
    finish_context = replace(
        fixture.context,
        state=durable,
        operations=None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=finish_context,
    )
    context = replace(context, operations=default_recovery_operations(context))
    monkeypatch.setattr(
        coordinator_module,
        "_finish_operations",
        lambda _context: resumed_operations,
    )
    monkeypatch.setattr(
        coordinator_module,
        "_default_patch_recovery_status",
        lambda *_args: (_ for _ in ()).throw(
            BootstrapError("patch target changed after publication")
        ),
    )

    with pytest.raises(BootstrapError, match="patch target changed after publication"):
        resume(context)

    recovered = load_unfinished_transaction(fixture.state_root)
    assert recovered is not None
    assert recovered.phase is BootstrapPhase.RECOVERY_REQUIRED


def test_default_patch_recovery_accepts_exact_adopted_patches_without_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    patched_records = {
        item.logical_path: item
        for item in fixture.after.records
        if item.logical_path in PATCH_LOGICAL_PATHS
    }
    fixture.pre_finish = replace(
        fixture.pre_finish,
        records=tuple(
            patched_records.get(item.logical_path, item)
            for item in fixture.pre_finish.records
        ),
    )
    fixture.patch_plan = PatchInspection(
        states=("patched",) * len(PATCH_LOGICAL_PATHS),
        targets=fixture.patch_plan.targets,
        disposition="keep",
    )
    state = fixture.run()
    monkeypatch.setattr(
        coordinator_module,
        "_run_finish_child",
        lambda _context, argv, _label, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "patched\n", ""
        ),
    )
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(fixture.context, state=state),
    )
    operations = default_recovery_operations(context)

    assert operations.inspect_patches(context, state) == "not_applicable"


def test_resume_binds_published_patch_backup_without_applying_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False

    def interrupt(boundary: str) -> None:
        nonlocal crashed
        if boundary == "runtime-status" and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(fixture.operations, boundary=interrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None and durable.patch_backup_manifest is None
    fixture.patch_backup.parent.mkdir(parents=True, exist_ok=True)
    fixture.patch_backup.parent.chmod(0o700)
    fixture.patch_backup.write_text("{}", encoding="ascii")
    fixture.patch_backup.chmod(0o600)
    patch_publications = 0

    def apply_patches(*_args: object) -> Path:
        nonlocal patch_publications
        patch_publications += 1
        return fixture.patch_backup

    resumed_operations = replace(
        fixture.context.operations,
        apply_patches=apply_patches,
        boundary=lambda _name: None,
    )
    finish_context = replace(
        fixture.context,
        state=durable,
        operations=None,
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=finish_context,
    )
    context = replace(context, operations=default_recovery_operations(context))
    monkeypatch.setattr(
        coordinator_module,
        "_finish_operations",
        lambda _context: resumed_operations,
    )
    monkeypatch.setattr(
        coordinator_module,
        "_run_finish_child",
        lambda _context, argv, _label, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "patched\n", ""
        ),
    )

    result = resume(context)

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert result.patch_backup_manifest == str(fixture.patch_backup)
    assert patch_publications == 0


@pytest.mark.parametrize(
    "boundary",
    (
        "install-threading",
        "runtime-install",
        "runtime-status",
        "patch-apply",
        "doctor",
        "write-after-snapshot",
        "compare-snapshots",
        "steam-options",
    ),
)
def test_default_resume_after_every_finish_boundary_reaches_ready_once(
    tmp_path: Path, boundary: str
) -> None:
    fixture = FinishFixture(tmp_path)
    crashed = False
    runtime_publications = 0
    original_install_runtime = fixture.operations.install_runtime

    def install_runtime(*args: object):
        nonlocal runtime_publications
        runtime_publications += 1
        return original_install_runtime(*args)  # type: ignore[arg-type]

    def interrupt(current: str) -> None:
        nonlocal crashed
        if current == boundary and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.context = replace(
        fixture.context,
        operations=replace(
            fixture.operations,
            install_runtime=install_runtime,
            boundary=interrupt,
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        fixture.run()
    durable = load_unfinished_transaction(fixture.state_root)
    assert durable is not None
    fixture.context = replace(
        fixture.context,
        state=durable,
        operations=replace(fixture.context.operations, boundary=lambda _name: None),
    )
    context = RecoveryContext(
        durable,
        fixture.state_root,
        fixture.stdout,
        finish_context=fixture.context,
    )
    context = replace(context, operations=default_recovery_operations(context))

    result = resume(context)

    assert result.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert runtime_publications == 1


def test_default_rollback_inspects_every_durable_owner_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    state = fixture.run()
    transaction = fixture.state_root / state.transaction_id
    licensed_journal = transaction / "recovery/licensed/licensed.json"
    licensed_journal.parent.chmod(0o700)
    licensed_journal.write_text(
        json.dumps(
            {
                "version": 1,
                "destination_logical": str(
                    fixture.system32 / "xgameruntime.dll.threading"
                ),
                "source_size": fixture.licensed_source.stat().st_size,
                "source_sha256": fixture.licensed_hash,
                "before": {
                    "logical_path": "prefix/threading",
                    "state": "absent",
                    "mode": None,
                    "size": None,
                    "sha256": None,
                    "private": True,
                },
                "disposition": "install",
                "stage_name": ".xgameruntime.dll.threading.111111111111111111111111.licensed-stage",
                "backup_name": ".xgameruntime.dll.threading.111111111111111111111111.licensed-backup",
                "rollback_name": ".xgameruntime.dll.threading.111111111111111111111111.rollback-held",
                "recovery_name": ".xgameruntime.dll.threading.111111111111111111111111.recovery",
                "status": "installed",
            }
        ),
        encoding="ascii",
    )
    licensed_journal.chmod(0o600)
    runtime_root = (
        fixture.user
        / ".local/state/forza-motorsport-linux/runtime-transactions"
        / ("b" * 24)
    )
    runtime_root.mkdir(parents=True)
    runtime_root.chmod(0o700)
    runtime_journal = runtime_root / "journal.json"
    runtime_journal.write_text(
        json.dumps(
            {
                "transaction_id": "b" * 24,
                "plan_sha256": fixture.runtime_plan.sha256,
                "state": "installed",
            }
        ),
        encoding="ascii",
    )
    runtime_journal.chmod(0o600)
    tool_info = (fixture.compatibility / "GE-Proton11-3-FM").stat()
    tool_record = {
        "schema": 1,
        "destination": {
            "root": "steam",
            "relative": "compatibilitytools.d/GE-Proton11-3-FM",
        },
        "disposition": "created",
        "identity": {
            "name": "GE-Proton11-3-FM",
            "base_release": "GE-Proton11-3",
            "vdf_sha256": "1" * 64,
            "managed_tree_sha256": "2" * 64,
            "marker_sha256": "3" * 64,
        },
        "root_device": tool_info.st_dev,
        "root_inode": tool_info.st_ino,
        "published_tree_sha256": "1" * 64,
    }
    tool_path = transaction / "compatibility-tool.json"
    tool_path.write_text(json.dumps(tool_record), encoding="ascii")
    tool_path.chmod(0o600)
    monkeypatch.setattr(
        coordinator_module,
        "_default_patch_recovery_status",
        lambda *_args: "pending",
        raising=False,
    )
    monkeypatch.setattr(
        coordinator_module,
        "inspect_fm",
        lambda *_args: type(
            "Inspection",
            (),
            {
                "disposition": __import__(
                    "forza_bootstrap.proton", fromlist=["ToolDisposition"]
                ).ToolDisposition.ADOPTED,
                "identity": fixture.manifest
                and coordinator_module.ToolIdentity(
                    "GE-Proton11-3-FM",
                    "GE-Proton11-3",
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
                "tree_sha256": "1" * 64,
            },
        )(),
    )
    prepare_context = type(
        "PrepareContextFixture",
        (),
        {
            "host": fixture.host,
            "manifest": fixture.manifest,
            "repository_root": tmp_path,
        },
    )()
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        prepare_context=prepare_context,  # type: ignore[arg-type]
        finish_context=replace(fixture.context, state=state),
    )
    operations = default_recovery_operations(context)

    assert operations.inspect_patches(context, state) == "pending"
    assert operations.inspect_runtime(context, state) == "pending"
    assert operations.inspect_threading(context, state) == "pending"
    assert operations.inspect_tool(context, state) == "pending"


def test_default_runtime_rollback_discovers_child_after_install_return_crash(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    state = fixture.run()
    transaction = fixture.state_root / state.transaction_id
    state_value = json.loads((transaction / "state.json").read_text())
    state_value["child_runtime_transaction"] = None
    (transaction / "state.json").write_text(json.dumps(state_value), encoding="ascii")
    (transaction / "state.json").chmod(0o600)
    state = replace(state, child_runtime_transaction=None)
    child_root = (
        fixture.user
        / ".local/state/forza-motorsport-linux/runtime-transactions"
        / ("b" * 24)
    )
    child_root.mkdir(parents=True)
    child_root.chmod(0o700)
    journal = child_root / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "transaction_id": "b" * 24,
                "plan_sha256": fixture.runtime_plan.sha256,
                "state": "installed",
            }
        ),
        encoding="ascii",
    )
    journal.chmod(0o600)
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(fixture.context, state=state),
    )
    operations = default_recovery_operations(context)

    assert operations.inspect_runtime(context, state) == "pending"


def test_runtime_gap_discovery_ignores_prior_matching_terminal_child(
    tmp_path: Path,
) -> None:
    fixture = FinishFixture(tmp_path)
    state = fixture.run()
    transaction = fixture.state_root / state.transaction_id
    state_value = json.loads((transaction / "state.json").read_text())
    state_value["child_runtime_transaction"] = None
    (transaction / "state.json").write_text(json.dumps(state_value), encoding="ascii")
    (transaction / "state.json").chmod(0o600)
    state = replace(state, child_runtime_transaction=None)
    runtime_root = (
        fixture.user / ".local/state/forza-motorsport-linux/runtime-transactions"
    )
    for child, status in (("a" * 24, "rolled_back"), ("c" * 24, "installed")):
        child_root = runtime_root / child
        child_root.mkdir(parents=True)
        child_root.chmod(0o700)
        journal = child_root / "journal.json"
        journal.write_text(
            json.dumps(
                {
                    "transaction_id": child,
                    "plan_sha256": fixture.runtime_plan.sha256,
                    "state": status,
                }
            ),
            encoding="ascii",
        )
        journal.chmod(0o600)
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(fixture.context, state=state),
    )
    operations = default_recovery_operations(context)

    assert operations.inspect_runtime(context, state) == "pending"


def test_default_patch_rollback_refuses_patched_targets_without_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FinishFixture(tmp_path)
    state = fixture.run()
    transaction = fixture.state_root / state.transaction_id
    Path(state.patch_backup_manifest).unlink()  # type: ignore[arg-type]
    state_value = json.loads((transaction / "state.json").read_text())
    state_value["patch_backup_manifest"] = None
    (transaction / "state.json").write_text(json.dumps(state_value), encoding="ascii")
    (transaction / "state.json").chmod(0o600)
    state = replace(state, patch_backup_manifest=None)
    monkeypatch.setattr(
        coordinator_module,
        "_run_finish_child",
        lambda argv_context, argv, label, **kwargs: subprocess.CompletedProcess(
            argv, 0, "patched\n", ""
        ),
    )
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        finish_context=replace(fixture.context, state=state),
    )
    operations = default_recovery_operations(context)

    with pytest.raises(BootstrapError, match="patch backup is missing"):
        operations.inspect_patches(context, state)


def test_rollback_holds_launcher_lease_across_every_owner(tmp_path: Path) -> None:
    fixture = FinishFixture(tmp_path)
    state = fixture.run()
    checked: list[str] = []
    statuses = {name: "pending" for name in ("patch", "runtime", "threading", "tool")}

    def inspector(name: str):
        def inspect(_context: RecoveryContext, _state: BootstrapState) -> str:
            return statuses[name]

        return inspect

    def owner(name: str):
        def mutate(_context: RecoveryContext, _state: BootstrapState) -> None:
            contender = (fixture.runtime / "forza-linux.lock").open("r+b")
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                contender.close()
            checked.append(name)
            statuses[name] = "rolled_back"

        return mutate

    operations = RecoveryOperations(
        resume_prepare=lambda _context, current: current,
        resume_finish=lambda _context, current: current,
        inspect_patches=inspector("patch"),
        restore_patches=owner("patch"),
        inspect_runtime=inspector("runtime"),
        rollback_runtime=owner("runtime"),
        inspect_threading=inspector("threading"),
        rollback_threading=owner("threading"),
        inspect_tool=inspector("tool"),
        rollback_tool=owner("tool"),
    )
    context = RecoveryContext(
        state,
        fixture.state_root,
        fixture.stdout,
        operations,
        finish_context=replace(fixture.context, state=state),
    )

    result = rollback(context, confirm="ROLLBACK")

    assert result.phase is BootstrapPhase.ROLLED_BACK
    assert checked == ["patch", "runtime", "threading", "tool"]


def test_rollback_is_reverse_dependency_order_and_second_run_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    fixture.state = replace(fixture.state, compatibility_tool_disposition="created")
    transaction = fixture.root / fixture.state.transaction_id
    state_path = transaction / "state.json"
    value = json.loads(state_path.read_text())
    value["compatibility_tool_disposition"] = "created"
    state_path.write_text(json.dumps(value), encoding="ascii")
    state_path.chmod(0o600)
    fixture.context = replace(fixture.context, state=fixture.state)

    result = rollback(fixture.context, confirm="ROLLBACK")
    again = rollback(replace(fixture.context, state=result), confirm="ROLLBACK")

    assert fixture.actions[:4] == [
        "patch-restore",
        "runtime-rollback",
        "threading-rollback",
        "compat-tool-rollback",
    ]
    assert result.phase is BootstrapPhase.ROLLED_BACK
    assert again == result
    assert fixture.actions.count("patch-restore") == 1


def test_rollback_preflights_accepted_runtime_before_any_mutation(
    tmp_path: Path,
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    state_path = fixture.root / fixture.state.transaction_id / "state.json"
    before = state_path.read_bytes()

    def accepted_runtime(_context: RecoveryContext, _state: BootstrapState) -> str:
        return "accepted"

    fixture.operations = replace(
        fixture.operations,
        inspect_runtime=accepted_runtime,
    )
    fixture.context = replace(fixture.context, operations=fixture.operations)

    with pytest.raises(BootstrapError, match="accepted runtime child"):
        rollback(fixture.context, confirm="ROLLBACK")

    assert fixture.patch == "applied"
    assert "patch-restore" not in fixture.actions
    assert state_path.read_bytes() == before
    assert load_unfinished_transaction(fixture.root) == fixture.state


def test_declined_confirmation_is_an_exact_noop(tmp_path: Path) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    before = (fixture.root / fixture.state.transaction_id / "state.json").read_bytes()

    result = rollback(fixture.context, confirm="rollback")

    assert result == fixture.state
    assert fixture.actions == []
    assert (
        fixture.root / fixture.state.transaction_id / "state.json"
    ).read_bytes() == before


def test_accepted_transaction_refuses_ordinary_rollback(tmp_path: Path) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.ACCEPTED)

    with pytest.raises(BootstrapError, match="accepted transaction"):
        rollback(fixture.context, confirm="ROLLBACK")

    assert fixture.actions == []


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    (
        ("patch", "changed", "patch target changed"),
        ("runtime", "recovery_required", "runtime child requires recovery"),
        ("threading", "changed", "licensed destination changed"),
        ("tool", "changed", "compatibility tool changed"),
    ),
)
def test_rollback_ambiguity_enters_recovery_required_and_prints_private_journal(
    tmp_path: Path, attribute: str, value: str, message: str
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    fixture.state = replace(fixture.state, compatibility_tool_disposition="created")
    state_path = fixture.root / fixture.state.transaction_id / "state.json"
    document = json.loads(state_path.read_text())
    document["compatibility_tool_disposition"] = "created"
    state_path.write_text(json.dumps(document), encoding="ascii")
    state_path.chmod(0o600)
    fixture.context = replace(fixture.context, state=fixture.state)
    setattr(fixture, attribute, value)

    with pytest.raises(BootstrapError, match=message):
        rollback(fixture.context, confirm="ROLLBACK")

    durable = load_unfinished_transaction(fixture.root)
    assert durable is not None
    assert durable.phase is BootstrapPhase.RECOVERY_REQUIRED
    assert str(fixture.root / durable.transaction_id) in fixture.output.getvalue()


def test_adopted_tool_is_inspected_but_never_removed(tmp_path: Path) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    fixture.state = replace(fixture.state, compatibility_tool_disposition="adopted")
    state_path = fixture.root / fixture.state.transaction_id / "state.json"
    value = json.loads(state_path.read_text())
    value["compatibility_tool_disposition"] = "adopted"
    state_path.write_text(json.dumps(value), encoding="ascii")
    state_path.chmod(0o600)
    fixture.context = replace(fixture.context, state=fixture.state)

    result = rollback(fixture.context, confirm="ROLLBACK")

    assert result.phase is BootstrapPhase.ROLLED_BACK
    assert fixture.tool == "created"
    assert "compat-tool-rollback" not in fixture.actions


ROLLBACK_BOUNDARIES = (
    "rollback-patches",
    "rollback-runtime",
    "rollback-threading",
    "rollback-compat-tool",
)


@pytest.mark.parametrize("boundary", ROLLBACK_BOUNDARIES)
def test_crash_at_every_rollback_boundary_resumes_without_duplicate_action(
    tmp_path: Path, boundary: str
) -> None:
    fixture = RecoveryFixture(tmp_path, BootstrapPhase.READY_TO_ATTEMPT)
    crashed = False

    def interrupt(current: str) -> None:
        nonlocal crashed
        if current == boundary and not crashed:
            crashed = True
            raise KeyboardInterrupt

    fixture.operations = replace(fixture.operations, boundary=interrupt)
    fixture.context = replace(fixture.context, operations=fixture.operations)
    with pytest.raises(KeyboardInterrupt):
        rollback(fixture.context, confirm="ROLLBACK")

    durable = load_unfinished_transaction(fixture.root)
    assert durable is not None and durable.phase is BootstrapPhase.ROLLING_BACK
    resumed = rollback(replace(fixture.context, state=durable), confirm="ROLLBACK")

    assert resumed.phase is BootstrapPhase.ROLLED_BACK
    expected = {
        "rollback-patches": "patch-restore",
        "rollback-runtime": "runtime-rollback",
        "rollback-threading": "threading-rollback",
        "rollback-compat-tool": "compat-tool-rollback",
    }[boundary]
    assert fixture.actions.count(expected) == 1


def test_only_one_unfinished_coordinator_can_be_recovered(tmp_path: Path) -> None:
    first = state_at(tmp_path / "state", BootstrapPhase.PREPARING)
    with pytest.raises(BootstrapError, match="unfinished bootstrap transaction"):
        create_transaction(tmp_path / "state", first.manifest_sha256)

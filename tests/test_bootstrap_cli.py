# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import forza_bootstrap.cli as cli_module
from forza_bootstrap.cli import main, parse_args
from forza_bootstrap.state import BootstrapPhase


def test_cli_parses_bootstrap_modes_as_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as failure:
        parse_args(["bootstrap", "--bundle", "/tmp/b.tar.zst", "--build-from-source"])

    assert failure.value.code == 2


def test_cli_rejects_combined_check_and_rollback() -> None:
    with pytest.raises(SystemExit) as failure:
        parse_args(["bootstrap", "--check", "--rollback"])

    assert failure.value.code == 2


@pytest.mark.parametrize("command", ("bootstrap", "snapshot", "compare"))
def test_cli_exposes_each_bootstrap_command(command: str) -> None:
    assert parse_args([command]).command == command


@pytest.mark.parametrize("mode", ("--check", "--rollback"))
def test_cli_accepts_one_bootstrap_mode(mode: str) -> None:
    parsed = parse_args(["bootstrap", mode])

    assert getattr(parsed, mode.removeprefix("--").replace("-", "_")) is True


def test_cli_does_not_report_an_unimplemented_command_as_success(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["snapshot"]) == 1

    assert "command is unavailable in this build" in capsys.readouterr().err


def test_cli_routes_bootstrap_prepare_and_preserves_full_digest_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = "a" * 64
    context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))

    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: None
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: expected)

    def fake_prepare(actual: object, confirm: object) -> object:
        assert actual is context
        assert confirm(expected) == expected  # type: ignore[operator]
        print("manual Steam handoff")
        return object()

    monkeypatch.setattr(cli_module, "prepare", fake_prepare)

    assert main(["bootstrap"]) == 0
    assert "manual Steam handoff" in capsys.readouterr().out


def test_cli_resumes_awaiting_prefix_through_finish_with_full_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "b" * 64
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(phase=BootstrapPhase.AWAITING_STEAM_PREFIX)
    finish_context = object()
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )
    monkeypatch.setattr(
        cli_module,
        "_finish_context",
        lambda actual, arguments, durable: (
            finish_context
            if (
                actual is prepare_context
                and arguments.threading_dll == "/licensed/runtime.dll"
                and durable is state
            )
            else None
        ),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: expected)

    def fake_finish(actual: object, confirm: object) -> object:
        assert actual is finish_context
        assert confirm(expected) == expected  # type: ignore[operator]
        calls.append("finish")
        return object()

    monkeypatch.setattr(cli_module, "finish", fake_finish)
    monkeypatch.setattr(
        cli_module,
        "prepare",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must resume Finish")),
    )

    assert main(
        ["bootstrap", "--threading-dll", "/licensed/runtime.dll"]
    ) == 0
    assert calls == ["finish"]


def test_cli_routes_interrupted_prepare_through_state_derived_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(phase=BootstrapPhase.PREPARING)
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )

    def fake_resume(context: object) -> object:
        assert context.state is state  # type: ignore[attr-defined]
        assert context.state_root == Path("/state")  # type: ignore[attr-defined]
        assert context.prepare_context is prepare_context  # type: ignore[attr-defined]
        assert context.finish_context is None  # type: ignore[attr-defined]
        calls.append("resume-prepare")
        return state

    monkeypatch.setattr(cli_module, "resume", fake_resume)
    monkeypatch.setattr(
        cli_module,
        "prepare",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must resume Prepare")),
    )

    assert main(["bootstrap"]) == 0
    assert calls == ["resume-prepare"]


def test_cli_routes_interrupted_finish_through_state_derived_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(phase=BootstrapPhase.INSTALLING_FINISH)
    finish_context = object()
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )
    monkeypatch.setattr(
        cli_module,
        "_finish_context_for_recovery",
        lambda actual, arguments, durable: (
            finish_context
            if (
                actual is prepare_context
                and arguments.threading_dll == "/licensed/runtime.dll"
                and durable is state
            )
            else None
        ),
    )

    def fake_resume(context: object) -> object:
        assert context.state is state  # type: ignore[attr-defined]
        assert context.prepare_context is prepare_context  # type: ignore[attr-defined]
        assert context.finish_context is finish_context  # type: ignore[attr-defined]
        calls.append("resume-finish")
        return state

    monkeypatch.setattr(cli_module, "resume", fake_resume)
    monkeypatch.setattr(
        cli_module,
        "finish",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must recover Finish")),
    )

    assert main(
        ["bootstrap", "--threading-dll", "/licensed/runtime.dll"]
    ) == 0
    assert calls == ["resume-finish"]


def test_cli_ready_rerun_is_noop_without_requiring_licensed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(
        phase=BootstrapPhase.READY_TO_ATTEMPT,
        finish_plan_sha256="c" * 64,
    )
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )
    monkeypatch.setattr(
        cli_module,
        "_finish_context_for_recovery",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("READY no-op must not reconstruct licensed input")
        ),
    )

    def fake_resume(context: object) -> object:
        assert context.state is state  # type: ignore[attr-defined]
        assert context.finish_context is None  # type: ignore[attr-defined]
        calls.append("ready-noop")
        return state

    monkeypatch.setattr(cli_module, "resume", fake_resume)

    assert main(["bootstrap"]) == 0
    assert calls == ["ready-noop"]


def test_cli_routes_composed_rollback_with_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(phase=BootstrapPhase.AWAITING_STEAM_PREFIX)
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "ROLLBACK")

    def fake_rollback(context: object, confirmation: str) -> object:
        assert context.state is state  # type: ignore[attr-defined]
        assert context.prepare_context is prepare_context  # type: ignore[attr-defined]
        assert context.finish_context is None  # type: ignore[attr-defined]
        assert confirmation == "ROLLBACK"
        calls.append("rollback")
        return state

    monkeypatch.setattr(cli_module, "rollback", fake_rollback)

    assert main(["bootstrap", "--rollback"]) == 0
    assert calls == ["rollback"]


def test_cli_ready_rollback_reconstructs_finish_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_context = SimpleNamespace(host=SimpleNamespace(state_root=Path("/state")))
    state = SimpleNamespace(
        phase=BootstrapPhase.READY_TO_ATTEMPT,
        finish_plan_sha256="d" * 64,
    )
    finish_context = object()
    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: prepare_context)
    monkeypatch.setattr(
        cli_module, "load_unfinished_transaction", lambda _state_root: state
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "ROLLBACK")
    monkeypatch.setattr(
        cli_module,
        "_finish_context_for_recovery",
        lambda actual, _arguments, durable: (
            finish_context
            if actual is prepare_context and durable is state
            else None
        ),
    )

    def fake_rollback(context: object, confirmation: str) -> object:
        assert context.finish_context is finish_context  # type: ignore[attr-defined]
        assert confirmation == "ROLLBACK"
        return state

    monkeypatch.setattr(cli_module, "rollback", fake_rollback)

    assert main(["bootstrap", "--rollback"]) == 0

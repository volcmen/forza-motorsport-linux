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

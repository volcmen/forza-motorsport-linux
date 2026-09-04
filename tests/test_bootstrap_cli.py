# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest

import forza_bootstrap.cli as cli_module
from forza_bootstrap.cli import main, parse_args


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
    context = object()

    monkeypatch.setattr(cli_module, "_prepare_context", lambda _arguments: context)
    monkeypatch.setattr("builtins.input", lambda _prompt: expected)

    def fake_prepare(actual: object, confirm: object) -> object:
        assert actual is context
        assert confirm(expected) == expected  # type: ignore[operator]
        print("manual Steam handoff")
        return object()

    monkeypatch.setattr(cli_module, "prepare", fake_prepare)

    assert main(["bootstrap"]) == 0
    assert "manual Steam handoff" in capsys.readouterr().out

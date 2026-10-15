from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forza_bootstrap import platform_check as check
from forza_bootstrap.model import BootstrapError


def vdf_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home with spaces; $(not-a-command)"
    steam = home / ".local/share/Steam"
    (steam / "steamapps").mkdir(parents=True)
    return {"HOME": str(home)}, steam


def install(library: Path, *, name: str = "Forza Motorsport") -> Path:
    game = library / "steamapps/common" / name
    game.mkdir(parents=True)
    (library / "steamapps/appmanifest_2440510.acf").write_text(
        f'"AppState" {{ "appid" "2440510" "installdir" {vdf_quote(name)} }}'
    )
    return game


def libraries(steam: Path, paths: list[Path]) -> None:
    entries = " ".join(
        f'"{index}" {{ "path" {vdf_quote(str(path))} }}'
        for index, path in enumerate(paths)
    )
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ {entries} }}'
    )


def test_primary_library_and_prefix_are_discovered_without_writes(layout):
    env, steam = layout
    game = install(steam)
    prefix = steam / "steamapps/compatdata/2440510/pfx"
    prefix.mkdir(parents=True)
    before = {
        str(p): (p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
        for p in steam.rglob("*")
    }
    result = check.discover_steam(env)
    assert result == check.SteamLayout(steam, steam, game, (prefix,))
    after = {
        str(p): (p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
        for p in steam.rglob("*")
    }
    assert before == after


def test_additional_library_handles_spaces_escaping_and_detached_cards(
    layout, tmp_path
):
    env, steam = layout
    card = tmp_path / 'SD Card "games" \\ folder'
    game = install(card)
    libraries(steam, [steam, tmp_path / "unmounted", card, card])
    prefix = card / "steamapps/compatdata/2440510/pfx"
    prefix.mkdir(parents=True)
    result = check.discover_steam(env)
    assert result.library_root == card
    assert result.game_root == game
    assert result.prefix_candidates == (prefix,)
    assert not (Path.cwd() / "not-a-command").exists()


def test_multiple_installed_copies_are_rejected(layout, tmp_path):
    env, steam = layout
    card = tmp_path / "card"
    install(card)
    install(steam)
    libraries(steam, [steam, card])
    with pytest.raises(BootstrapError, match="multiple Forza installations"):
        check.discover_steam(env)


def test_prefixes_in_both_libraries_are_reported_as_ambiguous(layout, tmp_path):
    env, steam = layout
    card = tmp_path / "card"
    install(card)
    libraries(steam, [steam, card])
    for root in (steam, card):
        (root / "steamapps/compatdata/2440510/pfx").mkdir(parents=True)
    assert len(check.discover_steam(env).prefix_candidates) == 2


@pytest.mark.parametrize(
    "payload",
    [
        '"AppState" { "appid" "2440510" "installdir" "../outside" }',
        '"AppState" { "appid" "123" "installdir" "Forza Motorsport" }',
        '"AppState" { "appid" "2440510" "appid" "2440510" "installdir" "Forza Motorsport" }',
        '"AppState" { "appid" "2440510" "installdir" "bad\x00name" }',
        '"AppState" { "appid" "2440510"',
    ],
)
def test_malformed_manifest_is_rejected(layout, payload):
    env, steam = layout
    install(steam)
    (steam / "steamapps/appmanifest_2440510.acf").write_text(payload)
    with pytest.raises(BootstrapError):
        check.discover_steam(env)


def test_symlink_and_fifo_metadata_are_not_followed_or_blocking(layout, tmp_path):
    env, steam = layout
    install(steam)
    manifest = steam / "steamapps/appmanifest_2440510.acf"
    target = tmp_path / "saved"
    manifest.rename(target)
    manifest.symlink_to(target)
    with pytest.raises(OSError):
        check.discover_steam(env)
    manifest.unlink()
    os.mkfifo(manifest)
    with pytest.raises(BootstrapError, match="regular file"):
        check.discover_steam(env)


def test_game_directory_cannot_escape_library(layout, tmp_path):
    env, steam = layout
    game = install(steam)
    game.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    game.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BootstrapError, match="escapes"):
        check.discover_steam(env)


def test_case_colliding_dlls_are_detected_without_opening_or_removing_them(layout):
    _env, steam = layout
    game = install(steam)
    (game / "xgameruntime.dll").write_bytes(b"first")
    (game / "XGameRuntime.dll").write_bytes(b"second")
    (game / "unrelated.txt").write_text("irrelevant")
    assert check.duplicate_dll_groups(game) == 1
    assert (game / "xgameruntime.dll").read_bytes() == b"first"
    assert (game / "XGameRuntime.dll").read_bytes() == b"second"


def test_report_is_redacted_and_never_claims_deck_playability(layout, monkeypatch):
    env, steam = layout
    install(steam)
    calls = []
    monkeypatch.setattr(
        check.platform, "freedesktop_os_release", lambda: {"ID": "steamos"}
    )
    monkeypatch.setattr(check.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(check.shutil, "which", lambda command: "/usr/bin/" + command)
    monkeypatch.setattr(check, "_system_library_present", lambda name: True)
    monkeypatch.setattr(check, "_query", lambda argv: calls.append(argv) or True)
    result = check.inspect_platform(env)
    assert result["gameplay"] == "unverified"
    assert result["installer_support"] == "blocked-pending-custom-proton"
    assert env["HOME"] not in json.dumps(result)
    assert not any(check["state"] == "FAIL" for check in result["checks"])
    assert calls == [
        ["systemctl", "--user", "show", "--property=Version"],
        ["busctl", "--user", "--quiet", "status", "org.freedesktop.secrets"],
    ]


def test_missing_steamos_gui_dependencies_are_reported(layout, monkeypatch):
    env, steam = layout
    install(steam)
    missing = {"libwebkit2gtk-4.1.so.0", "libadwaita-1.so.0"}
    monkeypatch.setattr(
        check, "_system_library_present", lambda name: name not in missing
    )
    result = check.inspect_platform(env)
    native = {
        row["check"]: row["state"]
        for row in result["checks"]
        if row["check"] in check.NATIVE_LIBRARIES
    }
    assert {name for name, state in native.items() if state == "FAIL"} == missing


def test_setup_platform_check_works_without_uv_or_writes_to_checkout(tmp_path):
    root = Path(__file__).parents[1]
    checkout = tmp_path / "checkout"
    home = tmp_path / "empty-home"
    home.mkdir()
    shutil.copytree(
        root / "forza_bootstrap",
        checkout / "forza_bootstrap",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (checkout / "scripts").mkdir()
    shutil.copy2(root / "setup", checkout / "setup")
    shutil.copy2(root / "scripts/forza-bootstrap", checkout / "scripts/forza-bootstrap")
    completed = subprocess.run(
        [str(checkout / "setup"), "platform-check", "--json"],
        env={
            **os.environ,
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["scope"] == "read-only platform diagnostics"
    assert list(home.iterdir()) == []
    assert list(checkout.rglob("__pycache__")) == []

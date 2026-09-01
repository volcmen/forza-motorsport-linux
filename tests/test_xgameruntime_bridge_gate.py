from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_bridge_gate_uses_an_isolated_exact_artifact_overlay():
    gate = (ROOT / "scripts/test-xgameruntime-bridge").read_text()

    required = [
        'mktemp -d "$build_dir/.ge-overlay.',
        "WINEDLLOVERRIDES=xgameruntime=b",
        'unlink "$overlay_dir/lib/wine/x86_64-windows/xgameruntime.dll"',
        'unlink "$overlay_dir/lib/wine/x86_64-unix/xgameruntime.so"',
        "cmp -s",
        'XGAMERUNTIME_TEST_XODUS="$mode"',
        "run_test failure",
        "run_test success",
    ]
    assert all(marker in gate for marker in required)


def test_fixture_is_local_and_never_handles_credentials():
    fixture = (ROOT / "tools/xodus-main-fixture.py").read_text().lower()

    assert "af_unix" in fixture
    assert "0o600" in fixture
    assert "http" not in fixture
    assert "token" not in fixture
    assert "xuid" not in fixture

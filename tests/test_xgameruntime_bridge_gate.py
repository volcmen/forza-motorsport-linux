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
        "xgameruntime-transport-test.py",
        'LD_LIBRARY_PATH="$build_dir/dlls/ntdll',
    ]
    assert all(marker in gate for marker in required)


def test_fixture_is_local_and_never_handles_credentials():
    fixture = (ROOT / "tools/xodus-main-fixture.py").read_text().lower()

    assert "af_unix" in fixture
    assert "0o600" in fixture
    assert "http" not in fixture
    assert "token" not in fixture
    assert "xuid" not in fixture


def test_transport_fixture_covers_private_socket_and_social_protocols():
    fixture = (ROOT / "tools/xgameruntime-transport-test.py").read_text().lower()

    required = [
        "0o600",
        "0o666",
        "xdsi",
        "xdui",
        "inviteaccept?",
        "status_access_denied",
        "status_cancelled",
        "invalid_network_response",
    ]
    assert all(marker in fixture for marker in required)


def test_legacy_transport_fixture_is_synthetic_and_uses_fixed_call_indexes():
    fixture = (
        ROOT / "tools/xgameruntime-legacy-transport-test.py"
    ).read_text().lower()

    required = [
        "invite_connect = 3",
        "invite_poll = 4",
        "invite_close = 5",
        "social_ui = 6",
        "0o600",
        "xdsi",
        "xdui",
        "inviteaccept",
        "max_invite_uri = 4096",
        "fragment",
        "connection refusal",
    ]
    forbidden = ["http://", "https://", "token", "xuid", "secretservice"]

    assert all(marker in fixture for marker in required)
    assert all(marker not in fixture for marker in forbidden)


def test_legacy_source_policy_covers_invite_privacy_queue_and_interface_contracts():
    policy = (ROOT / "tools/check-xgameruntime-legacy-source.py").read_text()

    required = [
        "activation URI appears in a log call",
        "invite/session data appears in a log call",
        "requestingUser appears in a social log call",
        "x_game_ui_XGameUiShowSendGameInviteAsync",
        "x_game_ui_XGameUiShowMultiplayerActivityGameInviteAsync",
        "interface IXGameUiImpl2 : IXGameUiImpl",
        "interface methods must not be standalone exports",
        "XTaskQueueDispatchMode_Manual",
        "XTaskQueueDuplicateHandle",
        "invite_received_delayed",
        "invite_received_self_unregister",
        "undispatched XGameInviteRegisterForEvent",
        "undispatched callback was not suppressed",
        "invite teardown retained",
        "XGameUi QueryInterface(NULL) returned",
        "wait=TRUE returned while a callback was in flight",
        "slot reuse returned an invalid or repeated token",
    ]

    assert all(marker in policy for marker in required)

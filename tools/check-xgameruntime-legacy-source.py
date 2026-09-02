#!/usr/bin/env python3
"""Fail closed on legacy XGameRuntime invite ABI and privacy regressions."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def log_calls(source: str) -> list[str]:
    return re.findall(r"(?:TRACE|WARN|FIXME)\s*\((.*?)\);", source, re.DOTALL)


def function_block(source: str, name: str) -> str:
    marker = f"static HRESULT WINAPI {name}("
    try:
        start = source.index(marker)
        end = source.index("\n}", start) + 2
    except ValueError as error:
        raise SystemExit(f"xgameui.c: missing {name}") from error
    return source[start:end]


def require_all(source: str, markers: list[str], label: str) -> None:
    missing = [marker for marker in markers if marker not in source]
    if missing:
        raise SystemExit(f"{label}: missing required markers: {missing}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="clean Wine source root")
    args = parser.parse_args()

    module = args.source / "dlls/xgameruntime"
    invite = (module / "xgameinvite.c").read_text()
    ui = (module / "xgameui.c").read_text()
    idl = (module / "xgameui.idl").read_text()
    tests = (module / "tests/xgameruntime.c").read_text()
    exports = (module / "xgameruntime.spec").read_text()

    sensitive_invite = {"poll.uri", "post->uri", "invite_uri"}
    sensitive_ui = {
        "sessionConfigurationId",
        "sessionTemplateName",
        "sessionId",
        "invitationText",
        "customActivationContext",
    }
    if any(name in call for call in log_calls(invite) for name in sensitive_invite):
        raise SystemExit("xgameinvite.c: activation URI appears in a log call")
    if any(name in call for call in log_calls(ui) for name in sensitive_ui):
        raise SystemExit("xgameui.c: invite/session data appears in a log call")
    social_user_functions = (
        "x_game_ui_XGameUiShowSendGameInviteAsync",
        "x_game_ui_XGameUiShowMultiplayerActivityGameInviteAsync",
    )
    if any(
        "requestingUser" in call
        for name in social_user_functions
        for call in log_calls(function_block(ui, name))
    ):
        raise SystemExit("xgameui.c: requestingUser appears in a social log call")

    require_all(
        invite,
        [
            "static struct list invite_posts",
            "invite_post_claim",
            "invite_next_post_token",
        ],
        "xgameinvite.c",
    )
    require_all(ui, ["if (!out) return E_POINTER;"], "xgameui.c")

    require_all(
        idl,
        [
            "interface IXGameUiImpl2 : IXGameUiImpl",
            "uuid(36a03122-9ea3-4a3a-a8a4-899cfd85d7db)",
            "XGameUiShowMultiplayerActivityGameInviteAsync( [in, out] XAsyncBlock *async, [in] XUserHandle requestingUser )",
            "XGameUiShowMultiplayerActivityGameInviteResult( [in, out] XAsyncBlock *async )",
        ],
        "xgameui.idl",
    )
    forbidden_exports = {
        "XGameUiShowMultiplayerActivityGameInviteAsync",
        "XGameUiShowMultiplayerActivityGameInviteResult",
    }
    if any(name in exports for name in forbidden_exports):
        raise SystemExit("xgameruntime.spec: interface methods must not be standalone exports")

    require_all(
        tests,
        [
            "IID_IXGameUiImpl2",
            "QueryApiImpl(XGameUi, IXGameUiImpl2)",
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
        ],
        "xgameruntime tests",
    )

    print("legacy XGameRuntime source policy: PASS")


if __name__ == "__main__":
    main()

# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only native Steam/SteamOS diagnostics; never an installation authority."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .model import BootstrapError
from .proton import _parse_vdf, _VdfToken

APP_ID = "2440510"
MAX_VDF_BYTES = 4 * 1024 * 1024
# Selected dependencies of the native service and legacy GUI. Presence is a
# prerequisite, not proof that every transitive dependency or symbol resolves.
SERVICE_LIBRARIES = (
    "libdbus-1.so.3",
    "libssl.so.3",
    "libcrypto.so.3",
)
GUI_LIBRARIES = (
    "libgtk-4.so.1",
    "libadwaita-1.so.0",
    "libSDL3.so.0",
    "libwebkit2gtk-4.1.so.0",
)
NATIVE_LIBRARIES = SERVICE_LIBRARIES + GUI_LIBRARIES


@dataclass(frozen=True)
class SteamLayout:
    """Observed paths only. Mutating tools must still validate their own roots."""

    steam_root: Path
    library_root: Path
    game_root: Path
    prefix_candidates: tuple[Path, ...]


def _read_vdf(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise BootstrapError("Steam metadata is not an owned regular file")
        if before.st_size > MAX_VDF_BYTES:
            raise BootstrapError("Steam metadata is too large")
        data = bytearray()
        while block := os.read(fd, min(65536, MAX_VDF_BYTES + 1 - len(data))):
            data.extend(block)
            if len(data) > MAX_VDF_BYTES:
                raise BootstrapError("Steam metadata is too large")
        after = os.fstat(fd)
        named = path.lstat()

        def signature(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                value.st_mode,
                value.st_uid,
            )

        if signature(before) != signature(after) or signature(after) != signature(
            named
        ):
            raise BootstrapError("Steam metadata changed during inspection")
        if b"\0" in data:
            raise BootstrapError("Steam metadata contains a NUL byte")
        try:
            return _parse_vdf(bytes(data))
        except (BootstrapError, RecursionError):
            raise BootstrapError("Steam metadata is malformed or ambiguous") from None
    finally:
        os.close(fd)


def _object(fields: dict, name: str) -> dict:
    entry = fields.get(name)
    if entry is None or not isinstance(entry.value, dict):
        raise BootstrapError("Steam metadata is missing a required object")
    return entry.value


def _string(fields: dict, name: str) -> str:
    entry = fields.get(name)
    if entry is None or not isinstance(entry.value, _VdfToken):
        raise BootstrapError("Steam metadata is missing a required string")
    value = entry.value.value
    if not value or any(ord(character) < 32 for character in value):
        raise BootstrapError("Steam metadata contains an invalid string")
    return value


def _directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BootstrapError("Steam directory is not owned by the current user")
    return resolved


def discover_steam(env: Mapping[str, str]) -> SteamLayout:
    """Inspect Valve metadata, including mounted additional libraries, without writes."""
    home = Path(env.get("HOME", ""))
    data = Path(env.get("XDG_DATA_HOME", str(home / ".local/share")))
    if not home.is_absolute() or not data.is_absolute():
        raise BootstrapError("HOME and XDG_DATA_HOME must be absolute paths")
    primary = data / "Steam"
    if not primary.exists():
        if (home / ".var/app/com.valvesoftware.Steam").exists():
            raise BootstrapError("Flatpak Steam is outside this native Steam workflow")
        raise BootstrapError(
            "native Steam is missing; start Steam in Desktop Mode first"
        )
    steam = _directory(primary)
    libraries = [steam]
    try:
        folders = _object(
            _read_vdf(steam / "steamapps/libraryfolders.vdf"), "libraryfolders"
        )
    except FileNotFoundError:
        folders = {}
    for key, entry in folders.items():
        if not key.isdecimal():
            continue
        if not isinstance(entry.value, dict):
            raise BootstrapError("legacy Steam library metadata needs a Steam refresh")
        candidate = Path(_string(entry.value, "path"))
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise BootstrapError(
                "Steam library path must be absolute without traversal"
            )
        try:
            library = _directory(candidate)
        except FileNotFoundError:
            continue  # A detached microSD entry is not an installed game.
        if library not in libraries:
            libraries.append(library)
    installations: list[tuple[Path, Path]] = []
    for library in libraries:
        try:
            app = _object(
                _read_vdf(library / f"steamapps/appmanifest_{APP_ID}.acf"), "AppState"
            )
        except FileNotFoundError:
            continue
        if _string(app, "appid") != APP_ID:
            raise BootstrapError("Forza Steam manifest has the wrong AppID")
        name = _string(app, "installdir")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise BootstrapError("Forza installation directory is not a basename")
        common = _directory(library / "steamapps/common")
        game = _directory(common / name)
        if game.parent != common:
            raise BootstrapError("Forza installation escapes its library")
        installations.append((library, game))
    if len(installations) != 1:
        raise BootstrapError(
            "multiple Forza installations are ambiguous"
            if installations
            else "Forza is not installed in a mounted native Steam library"
        )
    library, game = installations[0]
    prefixes: list[Path] = []
    for root in dict.fromkeys((library, steam)):
        try:
            prefix = _directory(root / f"steamapps/compatdata/{APP_ID}/pfx")
        except FileNotFoundError:
            continue
        if prefix not in prefixes:
            prefixes.append(prefix)
    return SteamLayout(steam, library, game, tuple(prefixes))


def duplicate_dll_groups(directory: Path) -> int:
    """Check the game's top-level DLL names without reading or modifying their contents."""
    names: dict[str, int] = {}
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 100000:
                raise BootstrapError("game directory is too large for this diagnostic")
            folded = entry.name.casefold()
            if folded.endswith(".dll"):
                names[folded] = names.get(folded, 0) + 1
    return sum(count > 1 for count in names.values())


def _query(argv: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _system_library_present(name: str) -> bool:
    # The validation targets use /usr/lib. Inspect names without dlopen, ldd,
    # running compiler probes, or executing constructors from shared objects.
    return (Path("/usr/lib") / name).is_file()


def inspect_platform(env: Mapping[str, str], *, profile: str = "composed") -> dict:
    if profile not in {"composed", "legacy"}:
        raise BootstrapError("unknown diagnostic profile")
    checks: list[dict[str, str]] = []

    def report(state: str, name: str, detail: str) -> None:
        checks.append({"state": state, "check": name, "detail": detail})

    try:
        os_id = platform.freedesktop_os_release().get("ID", "unknown")
    except OSError:
        os_id = "unknown"
    machine = platform.machine()
    report(
        "PASS" if os_id in {"arch", "steamos"} and machine == "x86_64" else "FAIL",
        "platform",
        "SteamOS x86_64"
        if os_id == "steamos" and machine == "x86_64"
        else "Arch x86_64"
        if os_id == "arch" and machine == "x86_64"
        else "outside the Arch/SteamOS x86_64 validation target",
    )
    report(
        "PASS" if sys.version_info >= (3, 11) else "FAIL",
        "Python",
        "3.11 or newer required",
    )
    commands = ("bash", "systemctl", "busctl", "flock")
    if profile == "composed":
        commands += ("flatpak",)
    for command in commands:
        available = shutil.which(command) is not None
        report(
            "PASS" if available else "FAIL",
            command,
            "available" if available else "missing",
        )
    libraries = NATIVE_LIBRARIES if profile == "legacy" else SERVICE_LIBRARIES
    for library in libraries:
        present = _system_library_present(library)
        report(
            "PASS" if present else "FAIL",
            library,
            "present; full application ABI not checked"
            if present
            else "missing native Xodus dependency; see the Steam Deck wiki page",
        )
    report(
        "PASS"
        if _query(["systemctl", "--user", "show", "--property=Version"])
        else "FAIL",
        "user systemd",
        "Desktop Mode user session required",
    )
    secret_owner = _query(
        ["busctl", "--user", "--quiet", "status", "org.freedesktop.secrets"]
    )
    report(
        "PASS" if secret_owner else "WARN",
        "Secret Service",
        "owner available"
        if secret_owner
        else "no current owner; unlock KDE Wallet and run forza-doctor",
    )
    try:
        layout = discover_steam(env)
        report(
            "PASS",
            "native Steam library",
            "primary"
            if layout.library_root == layout.steam_root
            else "additional (including microSD)",
        )
        accessible = os.access(layout.game_root, os.R_OK | os.W_OK | os.X_OK)
        report(
            "PASS" if accessible else "FAIL",
            "game directory access",
            "read/write/search allowed for current user; no write probe performed"
            if accessible
            else "Steam needs read/write/search access; review directory ownership and mount options",
        )
        count = len(layout.prefix_candidates)
        report(
            "PASS" if count == 1 else "WARN" if count == 0 else "FAIL",
            "Forza prefix",
            "one existing prefix"
            if count == 1
            else "not created yet"
            if count == 0
            else "multiple prefixes; identify the one Steam actually uses before making changes",
        )
        duplicates = duplicate_dll_groups(layout.game_root)
        report(
            "FAIL" if duplicates else "PASS",
            "game DLL filename collisions",
            f"{duplicates} case-insensitive duplicate groups; preserve files and review manually"
            if duplicates
            else "none in the game directory",
        )
        for name, path in (
            ("game filesystem free space", layout.game_root),
            ("Steam tools free space", layout.steam_root),
        ):
            free_gib = shutil.disk_usage(path).free // (1024**3)
            report(
                "WARN" if free_gib < 30 else "PASS", name, f"{free_gib} GiB available"
            )
    except (OSError, ValueError, BootstrapError) as error:
        # Never include raw OS exceptions: they can disclose private mount paths.
        detail = (
            str(error)
            if isinstance(error, BootstrapError)
            else "Steam metadata or paths are unavailable"
        )
        report("FAIL", "native Steam layout", detail)
    if profile == "legacy":
        report(
            "WARN",
            "fresh bootstrap",
            "v0.2 stock GE acquisition is not a complete FM build; see issues #7 and #8",
        )
    else:
        report(
            "WARN",
            "composed preview",
            "complete custom Proton and preview Flatpak required; composed Finish verifies the app/runtime pins; see Install",
        )
    report(
        "WARN",
        "Steam Deck gameplay",
        "requires physical-device validation; containers and VMs cannot establish playability",
    )
    return {
        "schema": 1,
        "scope": "read-only platform diagnostics",
        "profile": profile,
        "installer_support": (
            "experimental-composed-preview"
            if profile == "composed"
            else "blocked-pending-custom-proton"
        ),
        "gameplay": "unverified",
        "checks": checks,
    }


def main(*, json_output: bool = False, profile: str = "composed") -> int:
    result = inspect_platform(os.environ, profile=profile)
    if json_output:
        print(json.dumps(result, sort_keys=True))
    else:
        for check in result["checks"]:
            print(f"{check['state']} {check['check']}: {check['detail']}")
    return int(any(check["state"] == "FAIL" for check in result["checks"]))

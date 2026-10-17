<!--
SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Physical host Steam validation — 2026-10-17

This record covers the existing **legacy-v0.1** installation on the maintainer's
physical Arch Linux desktop, observed through its native Steam client and game
UI between 13:25 and 13:43 UTC. It is separate from the
[SteamOS VM qualification](preview-2-validation-2026-10-17.md).
The complete preview-2 composed runtime was **not installed or tested on this
host**. Its gameplay status does not inherit these results.

## Environment and runtime identity

| Item | Observed value |
| --- | --- |
| Desktop | Arch Linux, Hyprland; native Steam |
| Kernel | `7.2.8-zen1-2-zen` |
| CPU | Intel Core i9-11900K |
| GPU | NVIDIA GeForce RTX 4080 SUPER |
| Linux GPU driver | `615.71.09` |
| Game | Steam AppID `2440510`, build `18920732`, version `1.859.7102.0` |
| Selected compatibility tool | Existing custom `GE-Proton11-3-FM` |
| Installed runtime profile | `legacy-v0.1` |
| Repository used for separate source checks | `b83271b2fd3e7ca95a397241a10ce5d6ff18c08a` (`v0.3.0-preview.2`) |

The installed Xodus service, CLI and overlay, and the XGameRuntime PE and Unix
modules matched the five clean-build hashes in the
[v0.1 publication manifest](../manifests/publication-v0.1.toml) and
[September live-validation record](v0.1-live-validation.md). Those artifacts bind
Xodus `7b236772297b3475ea4f3cb830feb5b224f3064a` and XGameRuntime
`a1548b1cf57371715d10b608bc81a77a188e40d4`.

The actual game process mapped the NVIDIA driver and the existing native
Microsoft threading DLL. No Venus library was mapped. Steam's selected tool,
launch options and installed integration/runtime files were retained; no
runtime upgrade or DLL-free comparison was performed in this host test.

## Game observations

| Check | Result |
| --- | --- |
| First launch through host Steam | Passed; shader optimization completed, profile and main menu loaded |
| Keyboard menu navigation | Passed |
| Built-in Hakone benchmark | Completed; game reported `BENCHMARK RUN IS VALID` |
| Second launch without reboot | Passed; profile and main menu loaded again |
| Normal exit from both game sessions | Passed; Xodus inactive, socket and both ownership tokens absent |
| Kernel GPU-reset or out-of-memory events during the test interval | None found in the accessible kernel journal |

The benchmark retained the existing custom/auto graphics settings, including
NVIDIA DLSS Auto, at **3440 × 1440**. Its result screen reported **60 FPS
achieved**, **zero stutters**, and **17.8 ms average latency**. The separate GPU
timing row showed 93.5 average, 70.7 minimum and 108.4 maximum FPS; those figures
are not the achieved presentation rate. This is one completed built-in
benchmark, not a long-session or multiplayer stability result.

The host initially displayed the fully assisted braking, throttle and steering
used temporarily in the VM test. Account synchronization is a plausible source
of that carryover, but was not isolated. Cleanup restored **ABS On** braking,
**Unassisted** throttle and **Normal** steering through the game's Save
confirmation. Reopening Settings and inspecting the saved profile confirmed
the restored values. No career race or car purchase was needed for this test.

## Current source checks on the host

These checks used the repository at the commit above. The two Steam game
sessions used the already installed legacy launcher and runtime.

- `./setup status` passed all eight Doctor checks; the existing accepted
  transaction and recovery records remained intact.
- Read-only platform diagnostics found the native game/prefix, no game DLL
  filename collisions and approximately 114 GiB free on the game filesystem.
- The current source launcher rejected a concurrent launch while the Steam
  game held the session lock.
- After the game exited, the current source launcher ran a small child that
  checked the real Xodus service and socket. The child exited successfully;
  the launcher then stopped its service and removed the socket and ownership
  tokens.
- Final comparisons confirmed unchanged hashes and modes for the checked
  installed files, and an unchanged Steam launch-options string.

Before the first launch, the installed September Doctor reported no Secret
Service provider. The current source Doctor recognized KDE's
`org.kde.secretservicecompat` activator and passed. The actual Steam launch
successfully activated the wallet; it did not require replacing the provider.

## Limits and retained evidence

No new controller/hotplug, explicit Invite/Join, long race, physical Steam Deck,
suspend/resume, fresh installation, or preview-2 composed-runtime qualification
is claimed. Automatic incoming Xbox invite notifications remain unsupported;
Xodus logged an HTTP 400 rejection for that endpoint during the session.

Screenshots, raw logs, saved-profile backup and private machine paths remain
local. This public record contains the observed outcomes and open-source
artifact references only; it publishes no account identifiers, credentials,
game files or proprietary binaries.
